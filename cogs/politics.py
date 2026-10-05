import json
import logging
import os
import re
import time
import aiosqlite
import disnake
from disnake.ext import commands
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


from functions.db_helpers import (
    create_law_proposal,
    get_user_info,
    update_user_info,
    get_law_proposal,
    update_law_proposal_status,
    DB_PATH,
    create_election,
    get_election_by_id,
    get_latest_election,
    get_all_active_elections,
    update_election_candidates,
    update_election_status,
    has_user_voted_election,
    add_election_vote,
    get_election_votes_summary,
    get_user_region,
    create_party,
    get_all_parties,
    get_party_by_id,
    get_party_by_name,
    delete_party,
    get_party_members_count
)
from functions.utils import create_embed, UniversalModal, format_number, ensure_user_registered

# Загрузка списка регионов
REGIONS_CONFIG_PATH = "configs/regions_config.json"
if os.path.exists(REGIONS_CONFIG_PATH):
    with open(REGIONS_CONFIG_PATH, "r", encoding="utf-8") as f:
        REGIONS_LIST = json.load(f).get("regions", [])
else:
    REGIONS_LIST = []

# === ИЗБИРАТЕЛЬНЫЕ ОКРУГА (ФКЗ "О выборах и референдумах", Приложение 1) ===
DISTRICTS = [
    {
        "id": "alvaria",
        "name": "Альварийский избирательный округ",
        "short_name": "Альварийский округ",
        "leader_title": "Президент Альварии",
        "gov_election_title": "Выборы Президента Альварии",
        "dep_election_title": "Выборы народного депутата от Альварии",
        "region_match": ["альвари", "республика альвария", "альварийский"],
        "display_name": "Альварийский округ (Респ. Альвария)"
    },
    {
        "id": "ledohod",
        "name": "Ледоходный избирательный округ",
        "short_name": "Ледоходный округ",
        "leader_title": "Мэр Ледохода",
        "gov_election_title": "Выборы Мэра Ледохода",
        "dep_election_title": "Выборы народного депутата от Ледохода",
        "region_match": ["ледоход", "город ледоход", "фсо", "столичный", "ледоходный"],
        "display_name": "Ледоходный округ (Федеральный Столичный Округ)"
    },
    {
        "id": "martinia",
        "name": "Мартинийский избирательный округ",
        "short_name": "Мартинийский округ",
        "leader_title": "Верховный Лидер Мартинии",
        "gov_election_title": "Выборы Верховного Лидера Мартинии",
        "dep_election_title": "Выборы народного депутата от Мартинии",
        "region_match": ["мартини", "республика мартиниа", "мартинийский"],
        "display_name": "Мартинийский округ (Респ. Мартиниа)"
    },
    {
        "id": "porelian",
        "name": "Порелианский избирательный округ",
        "short_name": "Порелианский округ",
        "leader_title": "Гос. Президент Порелиании",
        "gov_election_title": "Выборы Гос. Президента Порелиании",
        "dep_election_title": "Выборы народного депутата от Порелиании",
        "region_match": ["порелиан", "порелиания", "порелианская", "порелианский"],
        "display_name": "Порелианский округ (Порелианская Респ.)"
    },
    {
        "id": "sancho",
        "name": "Санчойский избирательный округ",
        "short_name": "Санчойский округ",
        "leader_title": "Канцлер Санчгоу",
        "gov_election_title": "Выборы Канцлера Санчгоу",
        "dep_election_title": "Выборы народного депутата от Санчгоу",
        "region_match": ["санч", "санчгоу", "санчойский", "республика санчгоу"],
        "display_name": "Санчойский округ (Респ. Санчгоу)"
    }
]

# Каналы форума для анонимных уведомлений о голосах
ELECTION_CHANNELS = {
    # Выборы Президента
    "president": 1553440948422189238,

    # Выборы народного депутата по округам
    "deputy": {
        "ledohod": 1553440847406563440,   # Выборы народного депутата от Ледохода
        "alvaria": 1553440759963848866,   # Выборы народного депутата от Альварии
        "martinia": 1553440673795936306,  # Выборы народного депутата от Мартинии
        "porelian": 1553440482556911726,  # Выборы народного депутата от Порелиании
        "sancho": 1553440377602842747     # Выборы народного депутата от Санчгоу
    },

    # Выборы глав округов (губернаторские)
    "governor": {
        "ledohod": 1553440163579953162,   # Выборы Мэра Ледохода
        "alvaria": 1553440064225280000,   # Выборы Президента Альварии
        "martinia": 1553439911632306298,  # Выборы Верховного Лидера Мартинии
        "porelian": 1553439820997591090,  # Выборы Гос. Президента Порелиании
        "sancho": 1553439208172159117     # Выборы Канцлера Санчгоу
    }
}

def find_district_by_region(region_str: Optional[str]) -> Optional[dict]:
    """Находит округ по названию региона прописки гражданина."""
    if not region_str:
        return None
    r_lower = region_str.strip().lower()
    for d in DISTRICTS:
        for m in d["region_match"]:
            if m in r_lower or r_lower in m:
                return d
    return None

def get_district_by_id(dist_id: str) -> Optional[dict]:
    """Возвращает данные округа по его идентификатору."""
    for d in DISTRICTS:
        if d["id"] == dist_id:
            return d
    return None


# === КОНФИГУРАЦИЯ И СПИСКИ РОЛЕЙ ===
# ID канала для логирования выдачи/снятия ролей (укажи свой)
LOG_CHANNEL_ID = 1520135463699087521

# Загружаем конфигурацию ролей для автокомплита и отображения
CONFIG_PATH = "configs/function_roles_config.json"
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        ROLES_CONFIG = json.load(f)
else:
    ROLES_CONFIG = {}

# Права на голосования
ALLOWED_TO_CREATE = ["speaker_of_seim", "vice_president_of_rezendia"]
ALLOWED_TO_VOTE = ["deputy_of_seim"]
ALLOWED_TO_END = ["speaker_of_seim", "vice_president_of_rezendia"]


def parse_user_roles(raw_roles) -> list:
    """Парсит роли пользователя из БД в список строк."""
    if not raw_roles:
        return []
    try:
        if isinstance(raw_roles, list):
            return raw_roles
        raw_str = str(raw_roles).strip()
        if raw_str.startswith("["):
            return json.loads(raw_str)
        return [r.strip() for r in raw_str.split(",") if r.strip()]
    except Exception:
        return [str(raw_roles)]


def has_any_role(user_roles: list, allowed_roles: list) -> bool:
    """Проверяет наличие хотя бы одной разрешенной роли."""
    return any(role in allowed_roles for role in user_roles)


async def role_autocomplete(inter: disnake.ApplicationCommandInteraction, user_input: str):
    """Автокомплит по списку ролей из конфига."""
    choices = {}
    for key, data in ROLES_CONFIG.items():
        role_name = data.get("name", key)
        # Ищем совпадение по ключу или читаемому названию
        if user_input.lower() in key.lower() or user_input.lower() in role_name.lower():
            choices[f"{role_name} ({key})"] = key
    
    # Discord поддерживает максимум 25 элементов автокомплита
    return [
        disnake.OptionChoice(name=name, value=value)
        for name, value in list(choices.items())[:25]
    ]


class PoliticsCog(commands.Cog):
    """Модуль политики и управления должностями."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.PROPOSALS_FORUM_ID = 1520142456782327908  # Форум входящих заявок
        self.VOTING_FORUM_ID = 1520143914546495699
        self.LAW_REVIEW_ROLE_ID = 1540367846658412596

    # ==========================================
    #           УПРАВЛЕНИЕ РОЛЯМИ (/function)
    # ==========================================

    @commands.slash_command(
        name="function",
        description="Управление государственными должностями пользователей",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def function_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @function_group.sub_command(
        name="give",
        description="Выдать функциональную роль пользователю"
    )
    async def function_give(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пользователь: disnake.Member = commands.Param(description="Пользователь, которому выдается роль"),
        должность: str = commands.Param(description="Выберите должность из списка", autocomplete=role_autocomplete),
        причина: str = commands.Param(default="Не указана", description="Причина назначения")
    ):

        if not await ensure_user_registered(inter, пользователь.id):
            return
        
        raw_roles = await get_user_info(пользователь.id, "functions")
        user_roles = parse_user_roles(raw_roles)

        if должность in user_roles:
            embed = create_embed(
                title="⚠️ Роль уже присутствует",
                description=f"У пользователя {пользователь.mention} уже есть должность `{должность}`.",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        user_roles.append(должность)
        await update_user_info(пользователь.id, "functions", json.dumps(user_roles, ensure_ascii=False))

        role_display_name = ROLES_CONFIG.get(должность, {}).get("name", должность)

        # 1. Ответ администратору
        admin_embed = create_embed(
            title="✅ Должность выдана",
            description=(
                f"**Сотрудник:** {пользователь.mention} (`{пользователь.id}`)\n"
                f"**Назначенная должность:** {role_display_name} (`{должность}`)\n"
                f"**Причина:** {причина}"
            ),
            color=disnake.Color.green()
        )
        await inter.response.send_message(embed=admin_embed, ephemeral=True)

        # 2. Уведомление пользователю в ЛС
        user_embed = create_embed(
            title="📜 Новое государственное назначение",
            description=(
                f"Вам была назначена должность: **{role_display_name}**.\n\n"
                f"**Приказ издал:** {inter.author.mention}\n"
                f"**Причина:** {причина}"
            ),
            color=disnake.Color.blue()
        )
        try:
            await пользователь.send(embed=user_embed)
        except disnake.Forbidden:
            pass

        # 3. Логирование
        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = create_embed(
                title="📋 Лог: Выдача должности",
                description=(
                    f"**Администратор:** {inter.author.mention} (`{inter.author.id}`)\n"
                    f"**Пользователь:** {пользователь.mention} (`{пользователь.id}`)\n"
                    f"**Должность:** {role_display_name} (`{должность}`)\n"
                    f"**Причина:** {причина}"
                ),
                color=disnake.Color.green(),
                footer_text=f"ID цели: {пользователь.id}"
            )
            await log_channel.send(embed=log_embed)

    @function_group.sub_command(
        name="revoke",
        description="Снять функциональную роль с пользователя"
    )
    async def function_revoke(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пользователь: disnake.Member = commands.Param(description="Пользователь, у которого снимается роль"),
        должность: str = commands.Param(description="Выберите должность из списка", autocomplete=role_autocomplete),
        причина: str = commands.Param(default="Не указана", description="Причина снятия")
    ):

        if not await ensure_user_registered(inter, пользователь.id):
            return
        
        raw_roles = await get_user_info(пользователь.id, "functions")
        user_roles = parse_user_roles(raw_roles)

        if должность not in user_roles:
            embed = create_embed(
                title="⚠️ Должность отсутствует",
                description=f"У пользователя {пользователь.mention} нет должности `{должность}`.",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        user_roles.remove(должность)
        await update_user_info(пользователь.id, "functions", json.dumps(user_roles, ensure_ascii=False))

        role_display_name = ROLES_CONFIG.get(должность, {}).get("name", должность)

        # 1. Ответ администратору
        admin_embed = create_embed(
            title="🗑️ Должность снята",
            description=(
                f"**Сотрудник:** {пользователь.mention} (`{пользователь.id}`)\n"
                f"**Снятая должность:** {role_display_name} (`{должность}`)\n"
                f"**Причина:** {причина}"
            ),
            color=disnake.Color.red()
        )
        await inter.response.send_message(embed=admin_embed, ephemeral=True)

        # 2. Уведомление пользователю в ЛС
        user_embed = create_embed(
            title="📜 Отзыв государственной должности",
            description=(
                f"С вас была снята должность: **{role_display_name}**.\n\n"
                f"**Решение принял:** {inter.author.mention}\n"
                f"**Причина:** {причина}"
            ),
            color=disnake.Color.dark_red()
        )
        try:
            await пользователь.send(embed=user_embed)
        except disnake.Forbidden:
            pass

        # 3. Логирование
        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            log_embed = create_embed(
                title="📋 Лог: Снятие должности",
                description=(
                    f"**Администратор:** {inter.author.mention} (`{inter.author.id}`)\n"
                    f"**Пользователь:** {пользователь.mention} (`{пользователь.id}`)\n"
                    f"**Должность:** {role_display_name} (`{должность}`)\n"
                    f"**Причина:** {причина}"
                ),
                color=disnake.Color.red(),
                footer_text=f"ID цели: {пользователь.id}"
            )
            await log_channel.send(embed=log_embed)



    @commands.slash_command(name="mandate", description="Управление мандатами пользователя")
    async def mandate_manage(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @mandate_manage.sub_command(
        name="set",
        description="Установить количество мандатных мест пользователю"
    )
    async def mandate_set(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пользователь: disnake.Member = commands.Param(description="Пользователь, которому устанавливается мандат"),
        количество: int = commands.Param(description="Количество мандатных мест", ge=0)
    ):
        
        if not await ensure_user_registered(inter, пользователь.id):
            return
        
        await update_user_info(пользователь.id, "mandates", количество)

        embed = create_embed(
            title="✅ Мандаты обновлены",
            description=(
                f"**Пользователь:** {пользователь.mention} (`{пользователь.id}`)\n"
                f"**Новое количество мандатов:** `{количество}`"
            ),
            color=disnake.Color.green()
        )
        await inter.response.send_message(embed=embed, ephemeral=True)



    # ==========================================
    #         ГОЛОСОВАНИЯ И ОПРОСЫ (/vote)
    # ==========================================



    @commands.slash_command(
        name="vote",
        description="Голосование за политические решения"
    )
    async def start_voting(self, inter: disnake.ApplicationCommandInteraction):
        raw_roles = await get_user_info(inter.author.id, "functions")
        user_roles = parse_user_roles(raw_roles)

        if not has_any_role(user_roles, ALLOWED_TO_CREATE):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description="У вас нет полномочий для создания голосований.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        components = [
            disnake.ui.TextInput(
                label="Вопрос голосования",
                placeholder="Введите вопрос голосования",
                custom_id="voting_question",
                style=disnake.TextInputStyle.paragraph,
                min_length=5,
                max_length=512,
                required=True
            )
        ]

        async def on_modal_submit(modal_inter: disnake.ModalInteraction, text_values: dict):
            question = text_values["voting_question"]
            start_time = str(int(time.time()))
            end_time = str(int(time.time()) + 86400)
            initial_votes = json.dumps({})

            os.makedirs("data", exist_ok=True)

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """
                    INSERT INTO votings (question, options, votes, start_time, end_time)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (question, json.dumps(["За", "Против", "Воздержаться"]), initial_votes, start_time, end_time)
                )
                poll_id = cursor.lastrowid
                await db.commit()

            json_path = "data/votings_temp.json"
            temp_data = {}
            if os.path.exists(json_path):
                with open(json_path, "r", encoding="utf-8") as f:
                    try:
                        temp_data = json.load(f)
                    except json.JSONDecodeError:
                        pass

            temp_data[str(poll_id)] = {
                "author_id": modal_inter.author.id,
                "status": "active"
            }

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(temp_data, f, indent=4, ensure_ascii=False)

            desc = (
                f"**Суть вопроса:**\n{question}\n\n"
                f"📊 **Текущие результаты (мандаты):**\n"
                f"🟢 За: `0` (0.0%)\n"
                f"🔴 Против: `0` (0.0%)\n"
                f"⚪ Воздержались: `0` (0.0%)\n\n"
                f"👥 **Список проголосовавших:**\n"
                f"🟢 **За:** —\n"
                f"🔴 **Против:** —\n"
                f"⚪ **Воздержались:** —\n\n"
                f"Всего голосов: `0` | Проголосовало: `0` чел."
            )
            embed = create_embed(
                title="🗳️ Политическое голосование",
                description=desc,
                color=disnake.Color.blurple(),
                footer_text=f"Инициатор: {modal_inter.author.display_name} | ID: {poll_id}"
            )

            view = disnake.ui.View(timeout=None)
            view.add_item(disnake.ui.Button(label="За", style=disnake.ButtonStyle.success, custom_id=f"vote:for:{poll_id}"))
            view.add_item(disnake.ui.Button(label="Против", style=disnake.ButtonStyle.danger, custom_id=f"vote:against:{poll_id}"))
            view.add_item(disnake.ui.Button(label="Воздержаться", style=disnake.ButtonStyle.secondary, custom_id=f"vote:abstain:{poll_id}"))
            view.add_item(disnake.ui.Button(label="Завершить", style=disnake.ButtonStyle.primary, custom_id=f"vote:end:{poll_id}"))

            await modal_inter.response.send_message(embed=embed, view=view)

        modal = UniversalModal(
            title="Создание голосования",
            components=components,
            callback_func=on_modal_submit,
            custom_id="vote_create_modal"
        )
        await inter.response.send_modal(modal)

    @commands.Cog.listener("on_button_click")
    async def handle_voting_buttons(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id
        if not custom_id.startswith("vote:"):
            return

        parts = custom_id.split(":")
        action = parts[1]
        poll_id = int(parts[2])
        user_id = str(inter.author.id)

        raw_roles = await get_user_info(inter.author.id, "functions")
        user_roles = parse_user_roles(raw_roles)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT question, votes FROM votings WHERE id = ?", (poll_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    embed = create_embed(
                        title="Ошибка",
                        description="Голосование не найдено в базе данных.",
                        color=disnake.Color.red()
                    )
                    return await inter.response.send_message(embed=embed, ephemeral=True)

                question, raw_votes = row
                votes_dict = json.loads(raw_votes) if raw_votes else {}

        def parse_vote(entry):
            if isinstance(entry, dict):
                return entry.get("action"), int(entry.get("weight", 1))
            return str(entry), 1

        def build_voter_lines(target_action):
            entries = []
            for uid, val in votes_dict.items():
                act, weight = parse_vote(val)
                if act == target_action:
                    entries.append(f"<@{uid}> (`{weight}` м.)")
            return ", ".join(entries) if entries else "—"

        # --- ЗАВЕРШЕНИЕ ГОЛОСОВАНИЯ ---
        if action == "end":
            if not has_any_role(user_roles, ALLOWED_TO_END):
                embed = create_embed(
                    title="⛔ Доступ запрещен",
                    description="У вас нет прав для завершения этого голосования.",
                    color=disnake.Color.red()
                )
                return await inter.response.send_message(embed=embed, ephemeral=True)

            count_for = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "for")
            count_against = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "against")
            count_abstain = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "abstain")
            total_votes = count_for + count_against + count_abstain
            total_users = len(votes_dict)

            pct_for = (count_for / total_votes * 100) if total_votes > 0 else 0.0
            pct_against = (count_against / total_votes * 100) if total_votes > 0 else 0.0
            pct_abstain = (count_abstain / total_votes * 100) if total_votes > 0 else 0.0

            if total_votes == 0:
                verdict = "⚠️ Голосов не поступило."
            elif count_for > count_against:
                verdict = f"✅ **Решение принято большинством!** (За: {pct_for:.1f}%)"
            elif count_against > count_for:
                verdict = f"❌ **Решение отклонено!** (Против: {pct_against:.1f}%)"
            else:
                verdict = "⚖️ **Ничья.** Решение не принято."

            voters_for = build_voter_lines("for")
            voters_against = build_voter_lines("against")
            voters_abstain = build_voter_lines("abstain")

            final_desc = (
                f"**Суть вопроса:**\n{question}\n\n"
                f"📌 **Итоги:**\n{verdict}\n\n"
                f"📊 **Распределение голосов (мандатов):**\n"
                f"🟢 За: `{count_for}` ({pct_for:.1f}%)\n"
                f"🔴 Против: `{count_against}` ({pct_against:.1f}%)\n"
                f"⚪ Воздержались: `{count_abstain}` ({pct_abstain:.1f}%)\n\n"
                f"👥 **Итоговые голоса:**\n"
                f"🟢 **За:** {voters_for}\n"
                f"🔴 **Против:** {voters_against}\n"
                f"⚪ **Воздержались:** {voters_abstain}\n\n"
                f"Всего голосов: `{total_votes}` | Проголосовало: `{total_users}` чел."
            )

            final_embed = create_embed(
                title="🗳️ Голосование завершено",
                description=final_desc,
                color=disnake.Color.dark_gray(),
                footer_text=f"Завершил: {inter.author.display_name} | ID: {poll_id}"
            )
            return await inter.response.edit_message(embed=final_embed, view=None)

        # --- ОБРАБОТКА ГОЛОСОВАНИЯ (За / Против / Воздержаться) ---
        if not has_any_role(user_roles, ALLOWED_TO_VOTE):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description="У вашей должности нет права голоса.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        if user_id in votes_dict:
            embed = create_embed(
                title="⚠️ Повторное голосование",
                description="Вы уже отдали свой голос в этом опросе.",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        raw_mandates = await get_user_info(inter.author.id, "mandates")
        vote_weight = int(raw_mandates) if raw_mandates and int(raw_mandates) > 0 else 1

        votes_dict[user_id] = {
            "action": action,
            "weight": vote_weight
        }

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE votings SET votes = ? WHERE id = ?",
                (json.dumps(votes_dict), poll_id)
            )
            await db.commit()

        count_for = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "for")
        count_against = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "against")
        count_abstain = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "abstain")
        total_votes = count_for + count_against + count_abstain
        total_users = len(votes_dict)

        pct_for = (count_for / total_votes * 100) if total_votes > 0 else 0.0
        pct_against = (count_against / total_votes * 100) if total_votes > 0 else 0.0
        pct_abstain = (count_abstain / total_votes * 100) if total_votes > 0 else 0.0

        voters_for = build_voter_lines("for")
        voters_against = build_voter_lines("against")
        voters_abstain = build_voter_lines("abstain")

        updated_desc = (
            f"**Суть вопроса:**\n{question}\n\n"
            f"📊 **Текущие результаты (мандаты):**\n"
            f"🟢 За: `{count_for}` ({pct_for:.1f}%)\n"
            f"🔴 Против: `{count_against}` ({pct_against:.1f}%)\n"
            f"⚪ Воздержались: `{count_abstain}` ({pct_abstain:.1f}%)\n\n"
            f"👥 **Список проголосовавших:**\n"
            f"🟢 **За:** {voters_for}\n"
            f"🔴 **Против:** {voters_against}\n"
            f"⚪ **Воздержались:** {voters_abstain}\n\n"
            f"Всего голосов: `{total_votes}` | Проголосовало: `{total_users}` чел."
        )

        updated_embed = create_embed(
            title="🗳️ Политическое голосование",
            description=updated_desc,
            color=disnake.Color.blurple(),
            footer_text=inter.message.embeds[0].footer.text if inter.message.embeds else None
        )

        await inter.response.edit_message(embed=updated_embed)



    # ==========================================
    #         ЗАКОНОПРОЕКТЫ И ПРЕДЛОЖЕНИЯ
    # ==========================================



    @commands.slash_command(
        name="send_proposal_panel",
        description="Отправить панель подачи законопроектов (Админ)",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def send_proposal_panel(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)

        image_path = os.path.join("images", "zakonoproekt.png")
        if not os.path.exists(image_path):
            return await inter.edit_original_message(
                content=f"❌ Файл `{image_path}` не найден."
            )

        file = disnake.File(image_path, filename="zakonoproekt.png")

        container_body = [
            disnake.ui.TextDisplay(
                content=(
                    "### Подать законопроект\n\n"
                    "В этом канале Вы можете заполнить анкету для подачи законопроекта на рассмотрение Сеймом Резендии.\n\n"
                    "**Форма подачи законопроекта:**\n"
                    "1. Название закона\n"
                    "2. Тип закона (ФЗ, ФКЗ)\n"
                    "   * [статья Конституции о принятии законов](https://discord.com/channels/1520019406745370755/1530164877887275160/1530287316025999451)\n"
                    "3. Текст закона\n"
                    "   * [шаблон оформления законов](https://docs.google.com/document/d/1Yg6pU2oyZkzUjYAaeiCWYPNnUrSJXsWRUGcfGq1B7j8/edit?usp=sharing)\n"
                    "4. Ваше пояснение текста закона (по желанию)"
                )
            ),
            disnake.ui.MediaGallery(disnake.MediaGalleryItem(media="attachment://zakonoproekt.png"))
        ]

        components = [
            disnake.ui.Container(*container_body),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Подать законопроект",
                    emoji="📑",
                    style=disnake.ButtonStyle.secondary,
                    custom_id="btn:open_law_proposal_modal"
                )
            )
        ]

        await inter.channel.send(file=file, components=components)
        await inter.edit_original_message(content="✅ Панель подачи успешно отправлена.")

    # =========================================================
    #            ОТКРЫТИЕ МОДАЛКИ ЗАКОНОПРОЕКТА
    # =========================================================

    @commands.Cog.listener("on_button_click")
    async def handle_open_proposal_modal(self, inter: disnake.MessageInteraction):
        if inter.component.custom_id != "btn:open_law_proposal_modal":
            return

        modal_components = [
            disnake.ui.Label(
                text="Название закона",
                component=disnake.ui.TextInput(
                    custom_id="law_name",
                    placeholder="О уголовных преступлениях",
                    style=disnake.TextInputStyle.short,
                    min_length=3,
                    max_length=150,
                    required=True
                )
            ),
            disnake.ui.Label(
                text="Тип закона",
                component=disnake.ui.StringSelect(
                    custom_id="law_type",
                    placeholder="Выберите тип закона",
                    options=[
                        disnake.SelectOption(label="Федеральный Закон (ФЗ)", value="ФЗ", emoji="📘"),
                        disnake.SelectOption(label="Федеральный Конституционный Закон (ФКЗ)", value="ФКЗ", emoji="📕")
                    ],
                    min_values=1,
                    max_values=1
                )
            ),
            disnake.ui.Label(
                text="Текст закона",
                description="Желательно выложить ссылку на Google Документ с текстом вашего закона",
                component=disnake.ui.TextInput(
                    custom_id="law_text",
                    placeholder="Текст закона",
                    style=disnake.TextInputStyle.paragraph,
                    min_length=10,
                    max_length=4000,
                    required=True
                )
            ),
            disnake.ui.Label(
                text="Пояснения к закону",
                component=disnake.ui.TextInput(
                    custom_id="law_comment",
                    placeholder="В чём смысл данного закона?",
                    style=disnake.TextInputStyle.paragraph,
                    max_length=2000,
                    required=False
                )
            )
        ]

        await inter.response.send_modal(
            title="Подача законопроекта",
            custom_id="modal:submit_law_proposal",
            components=modal_components
        )

    # =========================================================
    #            ОБРАБОТКА ПОДАЧИ ЗАКОНОПРОЕКТА
    # =========================================================

    @commands.Cog.listener("on_modal_submit")
    async def handle_law_proposal_submit(self, inter: disnake.ModalInteraction):
        if inter.custom_id != "modal:submit_law_proposal":
            return

        await inter.response.defer(ephemeral=True)

        law_name = inter.text_values.get("law_name", "").strip()
        raw_type = inter.values.get("law_type")
        law_type_val = raw_type[0] if isinstance(raw_type, list) and raw_type else "ФЗ"
        law_text = inter.text_values.get("law_text", "").strip()
        law_comment = inter.text_values.get("law_comment", "").strip()

        # 1. Запись в БД
        proposal_id = await create_law_proposal(
            author_id=inter.author.id,
            law_name=law_name,
            law_type=law_type_val,
            law_text=law_text,
            law_comment=law_comment if law_comment else None
        )

        forum = inter.guild.get_channel(self.PROPOSALS_FORUM_ID)
        if not isinstance(forum, disnake.ForumChannel):
            embed = create_embed(
                title="❌ Ошибка",
                description="Форум-канал для рассмотрения законопроектов не найден.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        prop_role_id = self.LAW_REVIEW_ROLE_ID
        role_mention = f" • <@&{prop_role_id}>" if prop_role_id else ""
        header_text = f"👤 {inter.author.mention}{role_mention}"

        type_badge = "📕 Федеральный Конституционный Закон" if law_type_val == "ФКЗ" else "📘 Федеральный Закон"

        content_parts = [
            f"**Название закона**\n{law_name}\n\n",
            f"**Тип закона**\n{type_badge}\n\n"
        ]
        if law_comment:
            content_parts.append(f"**Пояснения к закону**\n{law_comment}\n\n")
        content_parts.append(f"**Текст закона**\n{law_text}")

        # 2. Карточка в форум (Контейнер без цвета)
        components_v2 = [
            disnake.ui.TextDisplay(content=header_text),
            disnake.ui.Container(
                disnake.ui.TextDisplay(content="".join(content_parts))
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Accept",
                    style=disnake.ButtonStyle.success,
                    custom_id=f"law_action:accept:{proposal_id}"
                ),
                disnake.ui.Button(
                    label="Deny",
                    style=disnake.ButtonStyle.danger,
                    custom_id=f"law_action:deny:{proposal_id}"
                )
            )
        ]

        await forum.create_thread(
            name=f"{law_name[:95]}",
            components=components_v2
        )

        success_embed = create_embed(
            title="📨 Законопроект подан",
            description="Ваш законопроект успешно передан на рассмотрение.",
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=success_embed)

    # =========================================================
    #            ОБРАБОТКА ВЕРДИКТОВ (ACCEPT / DENY)
    # =========================================================

    @commands.Cog.listener("on_button_click")
    async def handle_law_verdict_buttons(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id
        if not custom_id.startswith("law_action:"):
            return

        # Проверка прав администратора
        if not inter.author.guild_permissions.administrator:
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description="Рассматривать законопроекты может только администрация.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        parts = custom_id.split(":")
        action = parts[1]
        proposal_id = int(parts[2])

        proposal = await get_law_proposal(proposal_id)
        if not proposal:
            embed = create_embed(
                title="❌ Ошибка",
                description="Законопроект не найден в базе данных.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        if proposal["status"] != "pending":
            embed = create_embed(
                title="⚠️ Внимание",
                description=f"Этот законопроект уже был рассмотрен (Статус: `{proposal['status']}`).",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        # ==========================================
        #                   ACCEPT
        # ==========================================
        if action == "accept":
            await inter.response.defer()

            # 1. Создаем запись голосования в БД
            question_text = f"Законопроект: «{proposal['law_name']}» ({proposal['law_type']})\n\n{proposal['law_text']}"
            start_time = str(int(time.time()))
            end_time = str(int(time.time()) + 86400)
            initial_votes = json.dumps({})

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """
                    INSERT INTO votings (question, options, votes, start_time, end_time)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (question_text, json.dumps(["За", "Против", "Воздержаться"]), initial_votes, start_time, end_time)
                )
                poll_id = cursor.lastrowid
                await db.commit()

            # 2. Обновляем статус в БД
            await update_law_proposal_status(proposal_id, "accepted", inter.author.id, voting_poll_id=poll_id)

            # 3. Публикуем голосование в форум голосований
            target_channel = inter.guild.get_channel(self.VOTING_FORUM_ID)
            vote_desc = (
                f"**Суть вопроса:**\n{question_text}\n\n"
                f"📊 **Текущие результаты:**\n"
                f"🟢 За: `0` (0.0%)\n"
                f"🔴 Против: `0` (0.0%)\n"
                f"⚪ Воздержались: `0` (0.0%)\n\n"
                f"Всего проголосовало: `0` чел."
            )
            vote_embed = create_embed(
                title="🗳️ Политическое голосование",
                description=vote_desc,
                color=disnake.Color.blurple(),
                footer_text=f"Инициатор: {proposal['law_name']} | ID: {poll_id}"
            )
            vote_view = disnake.ui.View(timeout=None)
            vote_view.add_item(disnake.ui.Button(label="За", style=disnake.ButtonStyle.success, custom_id=f"vote:for:{poll_id}"))
            vote_view.add_item(disnake.ui.Button(label="Против", style=disnake.ButtonStyle.danger, custom_id=f"vote:against:{poll_id}"))
            vote_view.add_item(disnake.ui.Button(label="Воздержаться", style=disnake.ButtonStyle.secondary, custom_id=f"vote:abstain:{poll_id}"))
            vote_view.add_item(disnake.ui.Button(label="Завершить", style=disnake.ButtonStyle.primary, custom_id=f"vote:end:{poll_id}"))

            if isinstance(target_channel, disnake.ForumChannel):
                # Ищем тег «Голосование» или берем первый доступный тег форума
                tag = disnake.utils.get(target_channel.available_tags, name="Активные")
                tags = [tag] if tag else (target_channel.available_tags[:1] if target_channel.available_tags else [])

                await target_channel.create_thread(
                    name=f"Голосование: {proposal['law_name'][:80]}",
                    embed=vote_embed,
                    view=vote_view,
                    applied_tags=tags
                )
            elif target_channel:
                await target_channel.send(embed=vote_embed, view=vote_view)

            # 4. Уведомление автору в ЛС
            author = inter.guild.get_member(proposal["author_id"])
            if author:
                dm_embed = create_embed(
                    title="📜 Законопроект принят к рассмотрению",
                    description=(
                        f"Ваш законопроект **«{proposal['law_name']}»** был успешно одобрен к голосованию в Сейме!\n"
                        f"Одобрил: {inter.author.mention}"
                    ),
                    color=disnake.Color.green()
                )
                try:
                    await author.send(embed=dm_embed)
                except disnake.Forbidden:
                    pass

            # 5. Пересобираем карточку с сохранением контейнера
            prop_role_id = self.LAW_REVIEW_ROLE_ID
            role_mention = f" • <@&{prop_role_id}>" if prop_role_id else ""
            header_text = f"👤 <@{proposal['author_id']}>{role_mention}"

            type_badge = "📕 Федеральный Конституционный Закон" if proposal["law_type"] == "ФКЗ" else "📘 Федеральный Закон"
            content_parts = [
                f"**Название закона**\n{proposal['law_name']}\n\n",
                f"**Тип закона**\n{type_badge}\n\n"
            ]
            if proposal.get("law_comment"):
                content_parts.append(f"**Пояснения к закону**\n{proposal['law_comment']}\n\n")
            content_parts.append(f"**Текст закона**\n{proposal['law_text']}")

            updated_components = [
                disnake.ui.TextDisplay(content=header_text),
                disnake.ui.Container(
                    disnake.ui.TextDisplay(content="".join(content_parts))
                ),
                disnake.ui.ActionRow(
                    disnake.ui.Button(
                        label=f"Accepted by {inter.author.display_name}",
                        style=disnake.ButtonStyle.success,
                        disabled=True
                    )
                )
            ]

            await inter.edit_original_message(components=updated_components)

        # ==========================================
        #                    DENY
        # ==========================================
        elif action == "deny":
            components = [
                disnake.ui.Label(
                    text="Причина отказа",
                    description="Укажите причину отказа (необязательно)",
                    component=disnake.ui.TextInput(
                        custom_id="deny_reason",
                        placeholder="Например: Противоречит Конституции / некорректное оформление",
                        style=disnake.TextInputStyle.paragraph,
                        max_length=500,
                        required=False
                    )
                )
            ]
            await inter.response.send_modal(
                title="Отказ законопроекта",
                custom_id=f"modal:law_deny:{proposal_id}:{inter.message.id}",
                components=components
            )

    # =========================================================
    #         МОДАЛКА ПРИЧИНЫ ОТКАЗА ЗАКОНОПРОЕКТА
    # =========================================================

    @commands.Cog.listener("on_modal_submit")
    async def handle_law_deny_modal(self, inter: disnake.ModalInteraction):
        if not inter.custom_id.startswith("modal:law_deny:"):
            return

        await inter.response.defer()

        parts = inter.custom_id.split(":")
        proposal_id = int(parts[2])
        message_id = int(parts[3])
        reason = inter.text_values.get("deny_reason", "").strip()

        proposal = await get_law_proposal(proposal_id)
        if not proposal:
            return await inter.delete_original_message()

        # 1. Обновляем статус в БД
        await update_law_proposal_status(
            proposal_id, 
            "denied", 
            inter.author.id, 
            reject_reason=reason if reason else "Причина не указана"
        )

        # 2. Уведомление автору в ЛС
        author = inter.guild.get_member(proposal["author_id"])
        if author:
            reason_text = f"\n\n**Причина:** {reason}" if reason else ""
            deny_embed = create_embed(
                title="❌ Законопроект отклонен",
                description=f"Ваш законопроект **«{proposal['law_name']}»** был отклонен администратором {inter.author.mention}.{reason_text}",
                color=disnake.Color.red()
            )
            try:
                await author.send(embed=deny_embed)
            except disnake.Forbidden:
                pass

        # 3. Пересобираем карточку с сохранением контейнера в исходном сообщении
        try:
            msg = await inter.channel.fetch_message(message_id)

            prop_role_id = self.LAW_REVIEW_ROLE_ID
            role_mention = f" • <@&{prop_role_id}>" if prop_role_id else ""
            header_text = f"👤 <@{proposal['author_id']}>{role_mention}"

            type_badge = "📕 Федеральный Конституционный Закон" if proposal["law_type"] == "ФКЗ" else "📘 Федеральный Закон"
            content_parts = [
                f"**Название закона**\n{proposal['law_name']}\n\n",
                f"**Тип закона**\n{type_badge}\n\n"
            ]
            if proposal.get("law_comment"):
                content_parts.append(f"**Пояснения к закону**\n{proposal['law_comment']}\n\n")
            content_parts.append(f"**Текст закона**\n{proposal['law_text']}")

            updated_components = [
                disnake.ui.TextDisplay(content=header_text),
                disnake.ui.Container(
                    disnake.ui.TextDisplay(content="".join(content_parts))
                ),
                disnake.ui.ActionRow(
                    disnake.ui.Button(
                        label=f"Denied by {inter.author.display_name}",
                        style=disnake.ButtonStyle.danger,
                        disabled=True
                    )
                )
            ]

            await msg.edit(components=updated_components)
        except Exception:
            pass

        await inter.delete_original_message()



    # ==========================================
    #             ВЫБОРНАЯ СИСТЕМА
    # ==========================================

    @staticmethod
    def safe_select_emoji(em_str: Optional[str]) -> Optional[Union[str, disnake.PartialEmoji]]:
        """Преобразует строку эмодзи в PartialEmoji или юникод-строку для disnake.SelectOption."""
        if not em_str or not isinstance(em_str, str):
            return None
        em_str = em_str.strip()
        if not em_str:
            return None
        if (em_str.startswith("<:") or em_str.startswith("<a:")) and em_str.endswith(">"):
            try:
                return disnake.PartialEmoji.from_str(em_str)
            except Exception:
                return None
        if em_str.startswith(":") and em_str.endswith(":"):
            return None
        if len(em_str) <= 8 and not any(c.isalnum() for c in em_str):
            return em_str
        return None

    @staticmethod
    def parse_candidate_line(line: str, guild: Optional[disnake.Guild] = None) -> Tuple[Optional[str], str]:
        """
        Извлекает эмодзи (кастомный эмодзи сервера, стандартный флаг/юникод) из строки кандидата.
        Поддерживает:
        • <:name:id> в начале, в середине или в конце строки
        • <a:name:id> (анимированные)
        • :name: (шорткод эмодзи с сервера)
        • 🚩 / 🇷🇺 и любые юникод-флаги/эмодзи
        """
        line = line.strip()
        if not line:
            return None, ""

        # 1. Кастомные эмодзи Discord: <:name:id> или <a:name:id>
        custom_m = re.search(r'<a?:[a-zA-Z0-9_]+:[0-9]{17,20}>', line)
        if custom_m:
            token = custom_m.group(0)
            rest = re.sub(r'\s+', ' ', line.replace(token, "")).strip()
            return token, rest

        # 2. Шорткод эмодзи :name: (проверяем на сервере guild)
        short_m = re.search(r':([a-zA-Z0-9_]+):', line)
        if short_m:
            clean_name = short_m.group(1)
            token = short_m.group(0)
            if guild:
                found_emoji = disnake.utils.get(guild.emojis, name=clean_name)
                if found_emoji:
                    rest = re.sub(r'\s+', ' ', line.replace(token, "")).strip()
                    return str(found_emoji), rest

        # 3. Юникод-эмодзи / флаг в начале строки
        emoji_pattern = re.compile(
            r'^('
            r'[\U0001F1E6-\U0001F1FF]{2}'
            r'|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B50-\u2B55\u200D\uFE0F]+'
            r')\s*\|?\s*(.*)',
            re.UNICODE
        )
        m = emoji_pattern.match(line)
        if m:
            token = m.group(1).strip()
            rest = m.group(2).strip()
            return token, rest

        return None, line

    def parse_cand_and_party(self, text: str) -> Tuple[str, Optional[str]]:
        """Разбирает строку кандидата, выделяя имя и партию (например 'Иван Иванов - Партия' или 'Иван Иванов (Партия)')."""
        text = text.strip()
        if " - " in text:
            parts = text.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        elif text.endswith(")") and "(" in text:
            parts = text[:-1].rsplit("(", 1)
            return parts[0].strip(), parts[1].strip()
        return text, None

    def get_channel_for_election(self, el_type: str, dist_id: Optional[str] = None) -> Optional[int]:
        """Возвращает ID канала форума для уведомлений о выборах."""
        if el_type == "president":
            return ELECTION_CHANNELS.get("president")
        elif el_type == "deputy":
            if dist_id:
                return ELECTION_CHANNELS.get("deputy", {}).get(dist_id)
        elif el_type == "governor":
            if dist_id:
                return ELECTION_CHANNELS.get("governor", {}).get(dist_id)
        return None

    async def send_anonymous_vote_alert(self, channel_id: int, content: str):
        """Отправляет анонимное оповещение о голосовании в канал форума или текстовый канал."""
        try:
            ch = self.bot.get_channel(channel_id)
            if not ch:
                try:
                    ch = await self.bot.fetch_channel(channel_id)
                except Exception:
                    ch = None
            if not ch:
                logger.warning(f"Could not find or fetch election channel {channel_id}")
                return

            components = [
                disnake.ui.Container(
                    disnake.ui.TextDisplay(content=content)
                )
            ]

            if hasattr(ch, "send"):
                await ch.send(components=components)
            elif isinstance(ch, disnake.ForumChannel):
                if ch.threads:
                    await ch.threads[0].send(components=components)
                else:
                    await ch.create_thread(name="Голосование", components=components)
        except Exception as e:
            logger.error(f"Failed to send anonymous vote alert to channel {channel_id}: {e}")

    @commands.slash_command(
        name="election",
        description="Управление избирательной системой и выборами",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def election_cmd_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @election_cmd_group.sub_command(
        name="panel",
        description="Открыть панель настройки и проведения выборов (Администрация)"
    )
    async def election_panel(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)

        embed = create_embed(
            title="Избирательная комиссия Резендии",
            description=(
                "Добро пожаловать в панель управления избирательной системой Резендии.\n\n"
                "Выберите категорию выборов для настройки и проведения:\n"
                "• **Федеральные** (Президентские выборы)\n"
                "• **Окружные** (Депутатские / Губернаторские выборы)"
            ),
            color=disnake.Color.blue()
        )
        view = disnake.ui.View(timeout=180)
        view.add_item(disnake.ui.Button(
            label="Федеральные",
            style=disnake.ButtonStyle.primary,
            custom_id="el_nav:level:federal"
        ))
        view.add_item(disnake.ui.Button(
            label="Окружные",
            style=disnake.ButtonStyle.primary,
            custom_id="el_nav:level:district"
        ))
        await inter.edit_original_message(embed=embed, view=view)

    @election_cmd_group.sub_command(
        name="send_panel",
        description="Отправить постоянное сообщение для голосования на выборах (Администрация)"
    )
    async def election_send_panel(self, inter: disnake.ApplicationCommandInteraction):
        await self.send_election_panel(inter)

    @commands.slash_command(
        name="send_election_panel",
        description="Отправить постоянное сообщение для голосования на выборах (Администрация)",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def send_election_panel(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)

        components = [
            disnake.ui.Container(
                disnake.ui.TextDisplay(
                    content=(
                        "### Выборы в Резендской Республике\n\n"
                        "Для участия в голосовании нажмите кнопку ниже. Избирательный бюллетень будет сформирован автоматически в соответствии с вашей пропиской и текущими активными кампаниями."
                    )
                )
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Проголосовать",
                    style=disnake.ButtonStyle.primary,
                    custom_id="election_global_vote_btn"
                )
            )
        ]

        await inter.channel.send(components=components)
        await inter.edit_original_message(content="Постоянная панель голосования успешно отправлена в канал.")

    # ==========================================
    #            УПРАВЛЕНИЕ ПАРТИЯМИ (/party)
    # ==========================================

    async def build_party_panel_components(self) -> list:
        parties = await get_all_parties()

        cards = []
        if not parties:
            cards.append("*(В государственном реестре пока нет зарегистрированных партий)*")
        else:
            for p in parties:
                pid = p["id"]
                name = p["name"]
                emoji = p.get("emoji") or "🏛️"
                desc = p.get("description") or "Без описания"
                leader_id = p.get("leader_id")
                leader_str = f"<@{leader_id}>" if leader_id else "`Не назначен`"
                count = await get_party_members_count(name)

                cards.append(
                    f"{emoji} **{name}** (ID: `{pid}`)\n"
                    f"👑 **Лидер:** {leader_str} • 👥 **Членов:** `{count}` чел.\n"
                    f"📝 *{desc}*"
                )

        container_text = (
            "### 🏛️ Реестр политических партий Резендии\n\n"
            "Панель управления официальным списком политических партий республики.\n\n"
            + "\n\n---\n\n".join(cards)
        )

        container = disnake.ui.Container(
            disnake.ui.TextDisplay(content=container_text)
        )

        buttons = [
            disnake.ui.Button(
                label="Создать партию",
                emoji="➕",
                style=disnake.ButtonStyle.success,
                custom_id="party_admin:create"
            ),
            disnake.ui.Button(
                label="Удалить партию",
                emoji="🗑️",
                style=disnake.ButtonStyle.danger,
                custom_id="party_admin:delete"
            ),
            disnake.ui.Button(
                label="Обновить",
                emoji="🔄",
                style=disnake.ButtonStyle.secondary,
                custom_id="party_admin:refresh"
            )
        ]
        action_row = disnake.ui.ActionRow(*buttons)

        return [container, action_row]

    @commands.slash_command(
        name="party",
        description="Управление политическими партиями Резендии",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def party_cmd_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @party_cmd_group.sub_command(
        name="panel",
        description="Панель настройки и управления партиями (Администрация)"
    )
    async def party_panel(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)
        components = await self.build_party_panel_components()
        await inter.edit_original_message(components=components)

    @commands.Cog.listener("on_button_click")
    async def handle_election_buttons(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id

        # -----------------------------------------------------------
        # ПАНЕЛЬ УПРАВЛЕНИЯ ПАРТИЯМИ (АДМИНИСТРАЦИЯ)
        # -----------------------------------------------------------
        if custom_id.startswith("party_admin:"):
            if not inter.author.guild_permissions.administrator:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="⛔ Доступ разрешен только администраторам."))],
                    ephemeral=True
                )

            action = custom_id.split(":")[1]

            if action in ("refresh", "cancel_delete"):
                components = await self.build_party_panel_components()
                return await inter.response.edit_message(components=components)

            elif action == "create":
                modal = disnake.ui.Modal(
                    title="Создание политической партии",
                    custom_id="party_modal:create",
                    components=[
                        disnake.ui.TextInput(
                            label="Название партии",
                            placeholder="Либерально-демократическая партия",
                            custom_id="party_name",
                            style=disnake.TextInputStyle.short,
                            required=True,
                            max_length=60
                        ),
                        disnake.ui.TextInput(
                            label="Эмодзи или флаг партии",
                            placeholder="🚩 или <:flag:1234567890>",
                            custom_id="party_emoji",
                            style=disnake.TextInputStyle.short,
                            required=False,
                            max_length=60
                        ),
                        disnake.ui.TextInput(
                            label="Discord ID лидера (необязательно)",
                            placeholder="123456789012345678",
                            custom_id="party_leader",
                            style=disnake.TextInputStyle.short,
                            required=False,
                            max_length=30
                        ),
                        disnake.ui.TextInput(
                            label="Краткое описание / программа",
                            placeholder="Краткая программа или идеология партии...",
                            custom_id="party_desc",
                            style=disnake.TextInputStyle.paragraph,
                            required=False,
                            max_length=300
                        ),
                    ]
                )
                return await inter.response.send_modal(modal=modal)

            elif action == "delete":
                parties = await get_all_parties()
                if not parties:
                    return await inter.response.send_message(
                        components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ В реестре нет зарегистрированных партий для удаления."))],
                        ephemeral=True
                    )

                options = []
                for p in parties[:25]:
                    opt_emoji = None
                    if p.get("emoji"):
                        em_str = p["emoji"].strip()
                        if em_str.startswith("<") and em_str.endswith(">"):
                            try:
                                opt_emoji = disnake.PartialEmoji.from_str(em_str)
                            except Exception:
                                opt_emoji = None
                        else:
                            opt_emoji = em_str

                    desc_str = f"ID: {p['id']}"
                    if p.get("leader_id"):
                        desc_str += f" | Лидер: {p['leader_id']}"

                    options.append(
                        disnake.SelectOption(
                            label=p["name"][:100],
                            value=str(p["id"]),
                            description=desc_str[:100],
                            emoji=opt_emoji
                        )
                    )

                del_components = [
                    disnake.ui.Container(
                        disnake.ui.TextDisplay(
                            content=(
                                "### 🗑️ Удаление политической партии\n\n"
                                "Выберите партию, которую необходимо удалить из реестра Резендии.\n\n"
                                "⚠️ **Внимание:** Все участники удалённой партии автоматически получат статус `Беспартийный`!"
                            )
                        )
                    ),
                    disnake.ui.ActionRow(
                        disnake.ui.StringSelect(
                            custom_id="party_select:delete",
                            placeholder="Выберите партию для удаления...",
                            options=options
                        )
                    ),
                    disnake.ui.ActionRow(
                        disnake.ui.Button(
                            label="Отмена",
                            emoji="⬅️",
                            style=disnake.ButtonStyle.secondary,
                            custom_id="party_admin:cancel_delete"
                        )
                    )
                ]
                return await inter.response.edit_message(components=del_components)

        # -----------------------------------------------------------
        # 1. НАВИГАЦИЯ В АДМИН-ПАНЕЛИ ВЫБОРОВ
        # -----------------------------------------------------------
        if custom_id.startswith("el_nav:"):
            if not inter.author.guild_permissions.administrator:
                return await inter.response.send_message("Доступ разрешен только администраторам.", ephemeral=True)

            parts = custom_id.split(":")
            step = parts[1]

            # ШАГ 1 -> ШАГ 2: Выбор уровня (Федеральные / Окружные)
            if step == "level":
                level = parts[2]
                if level == "federal":
                    embed = create_embed(
                        title="Федеральные выборы Резендии",
                        description=(
                            "Категория федеральных общенациональных выборов:\n\n"
                            "• **Президентские выборы**\n"
                            "Выборы главы государства (Президента) и Вице-Президента Резендии."
                        ),
                        color=disnake.Color.blue()
                    )
                    view = disnake.ui.View(timeout=180)
                    view.add_item(disnake.ui.Button(
                        label="Президентские",
                        style=disnake.ButtonStyle.primary,
                        custom_id="el_nav:type:president"
                    ))
                    view.add_item(disnake.ui.Button(
                        label="Назад",
                        style=disnake.ButtonStyle.secondary,
                        custom_id="el_nav:back:root"
                    ))
                    return await inter.response.edit_message(embed=embed, view=view)

                elif level == "district":
                    embed = create_embed(
                        title="Окружные выборы Резендии",
                        description=(
                            "Категория окружных выборов (по 5 избирательным округам Резендии):\n\n"
                            "• **Депутатские выборы**\n"
                            "Выборы депутатов Сейма Республики по 5 избирательным округам.\n\n"
                            "• **Губернаторские выборы**\n"
                            "Выборы глав (губернаторов) 5 избирательных округов."
                        ),
                        color=disnake.Color.blue()
                    )
                    view = disnake.ui.View(timeout=180)
                    view.add_item(disnake.ui.Button(
                        label="Депутатские",
                        style=disnake.ButtonStyle.primary,
                        custom_id="el_nav:type:deputy"
                    ))
                    view.add_item(disnake.ui.Button(
                        label="Губернаторские",
                        style=disnake.ButtonStyle.primary,
                        custom_id="el_nav:gov_menu"
                    ))
                    view.add_item(disnake.ui.Button(
                        label="Назад",
                        style=disnake.ButtonStyle.secondary,
                        custom_id="el_nav:back:root"
                    ))
                    return await inter.response.edit_message(embed=embed, view=view)

            # Меню выбора округа для Губернаторских выборов
            elif step == "gov_menu":
                embed = create_embed(
                    title="Губернаторские выборы (Выборы глав округов)",
                    description=(
                        "Выберите избирательный округ для настройки и проведения выборов главы:\n\n"
                        "• **Альварийский округ** (Республика Альвария)\n"
                        "• **Ледоходный округ** (Федеральный Столичный Округ)\n"
                        "• **Мартинийский округ** (Республика Мартиниа)\n"
                        "• **Порелианский округ** (Порелианская Республика)\n"
                        "• **Санчойский округ** (Республика Санчгоу)"
                    ),
                    color=disnake.Color.blue()
                )
                view = disnake.ui.View(timeout=180)
                for d in DISTRICTS:
                    view.add_item(disnake.ui.Button(
                        label=d["short_name"],
                        style=disnake.ButtonStyle.primary,
                        custom_id=f"el_nav:gov_select:{d['id']}"
                    ))
                view.add_item(disnake.ui.Button(
                    label="Назад",
                    style=disnake.ButtonStyle.secondary,
                    custom_id="el_nav:level:district"
                ))
                return await inter.response.edit_message(embed=embed, view=view)

            elif step == "gov_select":
                dist_id = parts[2]
                return await self.show_election_action_menu(inter, "governor", target_region=dist_id)

            elif step == "back" and parts[2] == "root":
                embed = create_embed(
                    title="Избирательная комиссия Резендии",
                    description=(
                        "Добро пожаловать в панель управления избирательной системой Резендии.\n\n"
                        "Выберите категорию выборов для настройки и проведения:\n"
                        "• **Федеральные** (Президентские выборы)\n"
                        "• **Окружные** (Депутатские / Губернаторские выборы)"
                    ),
                    color=disnake.Color.blue()
                )
                view = disnake.ui.View(timeout=180)
                view.add_item(disnake.ui.Button(
                    label="Федеральные",
                    style=disnake.ButtonStyle.primary,
                    custom_id="el_nav:level:federal"
                ))
                view.add_item(disnake.ui.Button(
                    label="Окружные",
                    style=disnake.ButtonStyle.primary,
                    custom_id="el_nav:level:district"
                ))
                return await inter.response.edit_message(embed=embed, view=view)

            # ШАГ 2 -> ШАГ 3: Выбран тип выборов (Президентские / Депутатские)
            elif step == "type":
                el_type = parts[2]
                return await self.show_election_action_menu(inter, el_type, target_region=None)

        # -----------------------------------------------------------
        # 2. ДЕЙСТВИЯ: РЕДАКТИРОВАТЬ / НАЧАТЬ / ЗАКОНЧИТЬ
        # -----------------------------------------------------------
        elif custom_id.startswith("el_act:"):
            if not inter.author.guild_permissions.administrator:
                return await inter.response.send_message("Доступ разрешен только администраторам.", ephemeral=True)

            parts = custom_id.split(":")
            action = parts[1]
            el_type = parts[2]
            target_region = parts[3] if len(parts) > 3 and parts[3] != "none" else None

            election = await get_latest_election(el_type, target_region)
            if not election:
                eid = await create_election(el_type, target_region)
                election = await get_election_by_id(eid)

            election_id = election["id"]

            # Действие: Редактировать список кандидатов
            if action == "edit":
                if election["status"] == "finished":
                    new_eid = await create_election(el_type, target_region)
                    if election["candidates"]:
                        await update_election_candidates(new_eid, json.loads(election["candidates"]))
                    election = await get_election_by_id(new_eid)
                    election_id = election["id"]

                try:
                    candidates_data = json.loads(election["candidates"]) if election["candidates"] else None
                except Exception:
                    candidates_data = None

                if el_type == "deputy":
                    # Депутатские: 5 параметров в модалке (по одному на каждый округ)
                    cands_dict = candidates_data if isinstance(candidates_data, dict) else {}
                    components = []
                    for d in DISTRICTS:
                        did = d["id"]
                        d_cands = cands_dict.get(did, [])
                        cur_lines = []
                        for c in d_cands:
                            if isinstance(c, dict):
                                em = f"{c['emoji']} " if c.get("emoji") else ""
                                pty = f" - {c['party']}" if c.get("party") else ""
                                cur_lines.append(f"{em}{c.get('candidate', '')}{pty}")
                            else:
                                cur_lines.append(str(c))
                        cur_text = "\n".join(cur_lines)
                        label_text = f"{d['short_name']}"
                        components.append(
                            disnake.ui.TextInput(
                                label=label_text[:45],
                                placeholder="Иван Иванов - Партия\nПетр Петров",
                                value=cur_text[:4000],
                                custom_id=f"dep_{did}",
                                style=disnake.TextInputStyle.paragraph,
                                required=False,
                                max_length=4000
                            )
                        )
                    modal = disnake.ui.Modal(
                        title="Кандидаты: Выборы в Сейм"[:45],
                        custom_id=f"el_modal:edit_deputy:{election_id}",
                        components=components
                    )
                    return await inter.response.send_modal(modal=modal)

                elif el_type == "governor":
                    # Губернаторские: Кандидаты в главы данного округа
                    dist = get_district_by_id(target_region)
                    cands_list = candidates_data if isinstance(candidates_data, list) else []
                    cur_lines = []
                    for c in cands_list:
                        if isinstance(c, dict):
                            em = f"{c['emoji']} " if c.get("emoji") else ""
                            pty = f" - {c['party']}" if c.get("party") else ""
                            cur_lines.append(f"{em}{c.get('candidate', '')}{pty}")
                        else:
                            cur_lines.append(str(c))
                    cur_text = "\n".join(cur_lines)
                    label_str = f"Кандидаты: {dist['short_name'] if dist else target_region}"[:45]
                    modal = disnake.ui.Modal(
                        title="Кандидаты в Главы округа"[:45],
                        custom_id=f"el_modal:edit_candidates:{election_id}",
                        components=[
                            disnake.ui.TextInput(
                                label=label_str,
                                placeholder="Иван Иванов - Партия\nПетр Петров",
                                value=cur_text[:4000],
                                custom_id="candidates_raw",
                                style=disnake.TextInputStyle.paragraph,
                                required=False,
                                max_length=4000
                            )
                        ]
                    )
                    return await inter.response.send_modal(modal=modal)

                elif el_type == "president":
                    # Президентские: Кандидат в Президенты - Вице-президент
                    cands_list = candidates_data if isinstance(candidates_data, list) else []
                    cur_lines = []
                    for c in cands_list:
                        if isinstance(c, dict):
                            em = f"{c['emoji']} " if c.get("emoji") else ""
                            dep = f" - {c['deputy']}" if c.get("deputy") else ""
                            cur_lines.append(f"{em}{c['candidate']}{dep}")
                        else:
                            cur_lines.append(str(c))
                    cur_text = "\n".join(cur_lines)
                    modal = disnake.ui.Modal(
                        title="Кандидаты: Президентские"[:45],
                        custom_id=f"el_modal:edit_candidates:{election_id}",
                        components=[
                            disnake.ui.TextInput(
                                label="Список: Кандидат - Вице-президент",
                                placeholder="Иван Иванов - Петр Петров\nАлексей Смирнов - Сергей Сидоров",
                                value=cur_text[:4000],
                                custom_id="candidates_raw",
                                style=disnake.TextInputStyle.paragraph,
                                required=True,
                                max_length=4000
                            )
                        ]
                    )
                    return await inter.response.send_modal(modal=modal)

            # Действие: Начать выборы
            elif action == "start":
                if election["status"] == "active":
                    return await inter.response.send_message("Голосование уже активно!", ephemeral=True)

                if election["status"] == "finished":
                    new_eid = await create_election(el_type, target_region)
                    if election["candidates"]:
                        await update_election_candidates(new_eid, json.loads(election["candidates"]))
                    election = await get_election_by_id(new_eid)
                    election_id = election["id"]

                candidates = json.loads(election["candidates"]) if election["candidates"] else None
                if not candidates:
                    return await inter.response.send_message("Сначала заполните список кандидатов через кнопку «Редактировать»!", ephemeral=True)

                if el_type == "deputy":
                    if isinstance(candidates, dict):
                        if not any(candidates.values()):
                            return await inter.response.send_message("Сначала заполните список кандидатов хотя бы в одном округе через кнопку «Редактировать»!", ephemeral=True)
                    elif isinstance(candidates, list) and len(candidates) == 0:
                        return await inter.response.send_message("Сначала заполните список кандидатов через кнопку «Редактировать»!", ephemeral=True)
                elif isinstance(candidates, list) and len(candidates) == 0:
                    return await inter.response.send_message("Сначала заполните список кандидатов через кнопку «Редактировать»!", ephemeral=True)

                # Запуск выборов немедленно (доступны через постоянную панель)
                await update_election_status(election_id, "active")
                await self.show_election_action_menu(inter, el_type, target_region)
                return await inter.followup.send("Выборы успешно запущены! Они доступны в постоянном сообщении для голосования.", ephemeral=True)

            # Действие: Закончить выборы
            elif action == "finish":
                if election["status"] != "active":
                    return await inter.response.send_message("Выборы не находятся в активной фазе голосования.", ephemeral=True)

                await inter.response.defer()

                votes_summary = await get_election_votes_summary(election_id)
                total_votes = sum(votes_summary.values())

                candidates_raw = election["candidates"]
                try:
                    candidates_data = json.loads(candidates_raw) if candidates_raw else None
                except Exception:
                    candidates_data = None

                if el_type == "deputy":
                    # Подсчет по 5 округам
                    dist_votes = {d["id"]: {} for d in DISTRICTS}
                    for choice_str, cnt in votes_summary.items():
                        if ":" in choice_str:
                            did, cname = choice_str.split(":", 1)
                            dist_votes.setdefault(did, {})[cname] = cnt

                    cands_dict = candidates_data if isinstance(candidates_data, dict) else {}
                    district_sections = []

                    for d in DISTRICTS:
                        did = d["id"]
                        d_cands = cands_dict.get(did, [])
                        d_total = sum(dist_votes.get(did, {}).values())

                        cand_results = []
                        for c in d_cands:
                            c_name = c["candidate"]
                            cnt = dist_votes.get(did, {}).get(c_name, 0)
                            cand_results.append({
                                "candidate": c_name,
                                "party": c.get("party") or "Беспартийный",
                                "votes": cnt
                            })
                        cand_results.sort(key=lambda x: x["votes"], reverse=True)

                        dist_lines = [f"**{d['name']}** (проголосовало: `{d_total}`):"]
                        if not cand_results:
                            dist_lines.append("• *Кандидаты не были зарегистрированы.*")
                        else:
                            for cr in cand_results:
                                pct = (cr["votes"] / d_total * 100) if d_total > 0 else 0.0
                                dist_lines.append(
                                    f"• **{cr['candidate']}** ({cr['party']}): `{cr['votes']}` голосов ({pct:.1f}%)"
                                )
                        district_sections.append("\n".join(dist_lines))

                    result_content = (
                        "### Официальные итоги выборов депутатов Сейма Резендии\n\n"
                        f"Всего в Республике проголосовало: **{total_votes}** избирателей.\n\n"
                        + "\n\n".join(district_sections)
                    )

                elif el_type == "governor":
                    dist = get_district_by_id(target_region)
                    dist_name = dist["name"] if dist else target_region
                    lines = [f"Всего проголосовало: **{total_votes}** избирателей.\n"]

                    winner = None
                    winner_votes = -1

                    if not votes_summary:
                        lines.append("*В голосовании не было отдано ни одного голоса.*")
                    else:
                        lines.append("### Результаты голосования:")
                        for candidate_name, count in votes_summary.items():
                            percent = (count / total_votes * 100) if total_votes > 0 else 0.0
                            lines.append(f"• **{candidate_name}**: `{count}` голосов ({percent:.1f}%)")
                            if count > winner_votes:
                                winner_votes = count
                                winner = candidate_name

                        if winner:
                            lines.append(f"\n**Победитель:** **{winner}** с результатом `{winner_votes}` голосов!")

                    result_content = f"### Официальные итоги выборов Главы: {dist_name}\n\n" + "\n".join(lines)

                elif el_type == "president":
                    lines = [f"Всего проголосовало: **{total_votes}** избирателей.\n"]
                    winner = None
                    winner_votes = -1

                    if not votes_summary:
                        lines.append("*В голосовании не было отдано ни одного голоса.*")
                    else:
                        lines.append("### Результаты голосования:")
                        for candidate_name, count in votes_summary.items():
                            percent = (count / total_votes * 100) if total_votes > 0 else 0.0
                            lines.append(f"• **{candidate_name}**: `{count}` голосов ({percent:.1f}%)")
                            if count > winner_votes:
                                winner_votes = count
                                winner = candidate_name

                        if winner:
                            lines.append(f"\n**Победитель:** **{winner}** с результатом `{winner_votes}` голосов!")

                    result_content = "### Официальные итоги Президентских выборов\n\n" + "\n".join(lines)

                else:
                    result_content = f"### Официальные итоги выборов\n\nВсего голосов: {total_votes}"

                result_components = [
                    disnake.ui.Container(
                        disnake.ui.TextDisplay(content=result_content)
                    )
                ]

                # Отправляем официальные итоги в форум-канал выборов
                if el_type == "deputy":
                    for did, d_ch_id in ELECTION_CHANNELS.get("deputy", {}).items():
                        await self.send_anonymous_vote_alert(d_ch_id, result_content)
                else:
                    forum_ch_id = self.get_channel_for_election(el_type, target_region)
                    if forum_ch_id:
                        await self.send_anonymous_vote_alert(forum_ch_id, result_content)

                # Обновляем сообщение в канале выборов (публикуем итоги в контейнере)
                if election["announcement_channel_id"] and election["announcement_message_id"]:
                    ch = self.bot.get_channel(election["announcement_channel_id"])
                    if ch:
                        try:
                            msg = await ch.fetch_message(election["announcement_message_id"])
                            await msg.edit(content=None, embed=None, view=None, components=result_components)
                        except Exception:
                            pass

                # Переводим статус в finished
                await update_election_status(election_id, "finished")

                # Обновляем панель управления
                await self.show_election_action_menu(inter, el_type, target_region)
                await inter.followup.send(content="Выборы успешно завершены, итоги опубликованы!", ephemeral=True)

        # -----------------------------------------------------------
        # 3. КНОПКА ГОЛОСОВАНИЯ ДЛЯ ИЗБИРАТЕЛЕЙ (ЕДИНАЯ ПАНЕЛЬ)
        # -----------------------------------------------------------
        elif custom_id == "election_global_vote_btn" or custom_id.startswith("election_vote_btn:"):
            if not await ensure_user_registered(inter, inter.author.id):
                return

            active_elections = await get_all_active_elections()
            if not active_elections:
                return await inter.response.send_message(
                    components=[
                        disnake.ui.Container(
                            disnake.ui.TextDisplay(
                                content="В настоящее время активных выборов не проводится."
                            )
                        )
                    ],
                    ephemeral=True
                )

            user_reg = await get_user_region(inter.author.id)
            user_dist = find_district_by_region(user_reg)

            eligible_elections = []

            for election in active_elections:
                eid = election["id"]
                el_type = election["election_type"]
                target_reg = election["target_region"]

                # Проверяем, голосовал ли уже гражданин в этих конкретных выборах
                if await has_user_voted_election(eid, inter.author.id):
                    continue

                candidates_raw = election["candidates"]
                try:
                    candidates_data = json.loads(candidates_raw) if candidates_raw else None
                except Exception:
                    candidates_data = None

                # 1. Президентские выборы (общенациональные)
                if el_type == "president":
                    cands_list = candidates_data if isinstance(candidates_data, list) else []
                    if not cands_list:
                        continue
                    options = []
                    for c in cands_list[:25]:
                        opt_emoji = None
                        if isinstance(c, dict):
                            cand = c.get("candidate", "")
                            dep = c.get("deputy", "")
                            opt_emoji = self.safe_select_emoji(c.get("emoji"))
                            label_str = f"{cand} (Вице: {dep})" if dep else cand
                        else:
                            label_str = str(c)
                        options.append(disnake.SelectOption(label=label_str[:100], value=label_str[:100], emoji=opt_emoji))
                    if options:
                        eligible_elections.append((election, "Выборы Президента Резендии", options))

                # 2. Выборы депутатов Сейма (по округам)
                elif el_type == "deputy":
                    options = []
                    cands_dict = candidates_data if isinstance(candidates_data, dict) else {}

                    if user_dist:
                        dist_cands = cands_dict.get(user_dist["id"], []) if isinstance(candidates_data, dict) else (candidates_data if isinstance(candidates_data, list) else [])
                        for c in dist_cands[:25]:
                            c_name = c.get("candidate", "") if isinstance(c, dict) else str(c)
                            party = c.get("party") if isinstance(c, dict) else None
                            opt_emoji = self.safe_select_emoji(c.get("emoji")) if isinstance(c, dict) else None
                            label_str = f"{c_name} ({party})" if party else c_name
                            val_str = f"{user_dist['id']}:{c_name}"
                            options.append(disnake.SelectOption(label=label_str[:100], value=val_str[:100], emoji=opt_emoji))
                        if options:
                            title_label = user_dist.get("dep_election_title") or f"Выборы депутата ({user_dist['short_name']})"
                            eligible_elections.append((election, title_label, options))
                    else:
                        # Прописка ещё не указана в БД: формируем список кандидатов со всех округов
                        if isinstance(candidates_data, list):
                            for c in candidates_data[:25]:
                                c_name = c.get("candidate", "") if isinstance(c, dict) else str(c)
                                party = c.get("party") if isinstance(c, dict) else None
                                opt_emoji = self.safe_select_emoji(c.get("emoji")) if isinstance(c, dict) else None
                                label_str = f"{c_name} ({party})" if party else c_name
                                options.append(disnake.SelectOption(label=label_str[:100], value=f":{c_name}"[:100], emoji=opt_emoji))
                        elif isinstance(candidates_data, dict):
                            for d in DISTRICTS:
                                did = d["id"]
                                for c in candidates_data.get(did, []):
                                    c_name = c.get("candidate", "") if isinstance(c, dict) else str(c)
                                    party = c.get("party") if isinstance(c, dict) else None
                                    opt_emoji = self.safe_select_emoji(c.get("emoji")) if isinstance(c, dict) else None
                                    pty_str = f" ({party})" if party else ""
                                    label_str = f"[{d['short_name'][:10]}] {c_name}{pty_str}"
                                    val_str = f"{did}:{c_name}"
                                    options.append(disnake.SelectOption(label=label_str[:100], value=val_str[:100], emoji=opt_emoji))
                                    if len(options) >= 25:
                                        break
                                if len(options) >= 25:
                                    break
                        if options:
                            eligible_elections.append((election, "Выборы депутатов Сейма", options))

                # 3. Губернаторские выборы (выборы глав субъектов)
                elif el_type == "governor":
                    target_dist = get_district_by_id(target_reg) if target_reg else None
                    if user_dist and target_reg and user_dist["id"] != target_reg:
                        continue
                    cands_list = candidates_data if isinstance(candidates_data, list) else []
                    if not cands_list:
                        continue
                    options = []
                    for c in cands_list[:25]:
                        c_name = c.get("candidate", "") if isinstance(c, dict) else str(c)
                        party = c.get("party") if isinstance(c, dict) else None
                        opt_emoji = self.safe_select_emoji(c.get("emoji")) if isinstance(c, dict) else None
                        label_str = f"{c_name} ({party})" if party else c_name
                        options.append(disnake.SelectOption(label=label_str[:100], value=c_name[:100], emoji=opt_emoji))
                    if options:
                        if user_dist:
                            title_label = user_dist.get("gov_election_title") or f"Выборы Главы: {user_dist['short_name']}"
                        elif target_dist:
                            title_label = target_dist.get("gov_election_title") or f"Выборы Главы: {target_dist['short_name']}"
                        else:
                            title_label = "Выборы Главы округа"
                        eligible_elections.append((election, title_label, options))

            if not eligible_elections:
                msg = "На данный момент нет доступных бюллетеней для голосования (возможно, вы уже приняли участие во всех активных выборах)."
                return await inter.response.send_message(
                    components=[
                        disnake.ui.Container(
                            disnake.ui.TextDisplay(content=msg)
                        )
                    ],
                    ephemeral=True
                )

            # Формируем компоненты модального окна (до 5 штук)
            modal_components = []
            for election, title_label, options in eligible_elections[:5]:
                eid = election["id"]
                modal_components.append(
                    disnake.ui.Label(
                        text=title_label[:45],
                        component=disnake.ui.StringSelect(
                            custom_id=f"vote_sel:{eid}",
                            placeholder="Выберите кандидата...",
                            options=options,
                            min_values=1,
                            max_values=1,
                            required=True
                        )
                    )
                )

            modal = disnake.ui.Modal(
                title="Бюллетень для голосования",
                custom_id="global_modal_vote",
                components=modal_components
            )
            return await inter.response.send_modal(modal=modal)

    @commands.Cog.listener("on_dropdown")
    async def handle_election_dropdowns(self, inter: disnake.MessageInteraction):
        if inter.component.custom_id == "party_select:delete":
            if not inter.author.guild_permissions.administrator:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="⛔ Доступ разрешен только администраторам."))],
                    ephemeral=True
                )

            try:
                party_id = int(inter.values[0])
            except (ValueError, IndexError):
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ Некорректный ID партии."))],
                    ephemeral=True
                )

            party = await get_party_by_id(party_id)
            if not party:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ Партия не найдена в базе данных."))],
                    ephemeral=True
                )

            party_name = party["name"]
            await delete_party(party_id)

            components = await self.build_party_panel_components()
            await inter.response.edit_message(components=components)
            return await inter.followup.send(
                components=[disnake.ui.Container(disnake.ui.TextDisplay(content=f"🗑️ Партия **«{party_name}»** успешно удалена из реестра."))],
                ephemeral=True
            )

        if inter.component.custom_id == "el_nav_select:region":
            if not inter.author.guild_permissions.administrator:
                return await inter.response.send_message("Доступ разрешен только администраторам.", ephemeral=True)

            selected_region = inter.values[0]
            return await self.show_election_action_menu(inter, "regional", selected_region)


    async def show_election_action_menu(self, inter: disnake.MessageInteraction, el_type: str, target_region: Optional[str] = None):
        """Отображает Меню с кнопками Редактировать, Начать/Закончить и Назад."""
        election = await get_latest_election(el_type, target_region)
        if not election:
            eid = await create_election(el_type, target_region)
            election = await get_election_by_id(eid)

        election_id = election["id"]
        status = election["status"]

        candidates_raw = election["candidates"]
        try:
            candidates_data = json.loads(candidates_raw) if candidates_raw else None
        except Exception:
            candidates_data = None

        status_text = {
            "draft": "Черновик (настройка)",
            "active": "Идёт голосование",
            "finished": "Завершены"
        }.get(status, status)

        if el_type == "deputy":
            title_str = "Выборы депутатов Сейма Резендии"
            cands_dict = candidates_data if isinstance(candidates_data, dict) else {}
            sections = []
            for d in DISTRICTS:
                did = d["id"]
                d_cands = cands_dict.get(did, [])
                lines = [f"**{d['name']}**:"]
                if not d_cands:
                    lines.append("• *Кандидаты не зарегистрированы*")
                else:
                    for idx, c in enumerate(d_cands, 1):
                        em = f"{c['emoji']} " if isinstance(c, dict) and c.get("emoji") else ""
                        pty = f" ({c['party']})" if isinstance(c, dict) and c.get("party") else ""
                        c_name = c["candidate"] if isinstance(c, dict) else str(c)
                        lines.append(f"• {em}**{c_name}**{pty}")
                sections.append("\n".join(lines))

            cands_display = "\n\n".join(sections)
            desc = (
                f"• **Статус:** {status_text}\n"
                f"• **ID кампании:** `#{election_id}`\n\n"
                f"**Кандидаты по избирательным округам:**\n\n"
                f"{cands_display}\n\n"
                f"Используйте кнопки ниже для редактирования списка или управления голосованием."
            )

        elif el_type == "governor":
            dist = get_district_by_id(target_region)
            dist_title = dist["name"] if dist else target_region
            title_str = f"Выборы Главы: {dist_title}"
            cands_list = candidates_data if isinstance(candidates_data, list) else []
            cand_lines = []
            if not cands_list:
                cand_lines.append("*Список кандидатов пуст.*")
            else:
                for idx, c in enumerate(cands_list, 1):
                    if isinstance(c, dict):
                        em = f"{c['emoji']} " if c.get("emoji") else ""
                        pty = f" ({c['party']})" if c.get("party") else ""
                        cand_lines.append(f"{idx}. {em}**{c.get('candidate', '')}**{pty}")
                    else:
                        cand_lines.append(f"{idx}. **{c}**")

            desc = (
                f"• **Статус:** {status_text}\n"
                f"• **ID кампании:** `#{election_id}`\n"
                f"• **Округ:** {dist['display_name'] if dist else target_region}\n\n"
                f"**Зарегистрированные кандидаты в Главы округа:**\n"
                + "\n".join(cand_lines) + "\n\n"
                f"Используйте кнопки ниже для редактирования списка или управления голосованием."
            )

        elif el_type == "president":
            title_str = "Президентские выборы Резендии"
            cands_list = candidates_data if isinstance(candidates_data, list) else []
            cand_lines = []
            if not cands_list:
                cand_lines.append("*Список кандидатов пуст.*")
            else:
                for idx, c in enumerate(cands_list, 1):
                    if isinstance(c, dict):
                        em = f"{c['emoji']} " if c.get("emoji") else ""
                        cand_lines.append(f"{idx}. {em}**{c['candidate']}** (Вице-президент: *{c.get('deputy', '—')}*)")
                    else:
                        cand_lines.append(f"{idx}. **{c}**")

            desc = (
                f"• **Статус:** {status_text}\n"
                f"• **ID кампании:** `#{election_id}`\n\n"
                f"**Зарегистрированные кандидаты:**\n"
                + "\n".join(cand_lines) + "\n\n"
                f"Используйте кнопки ниже для редактирования списка или управления голосованием."
            )
        else:
            title_str = "Выборы"
            desc = f"• **Статус:** {status_text}\n• **ID кампании:** `#{election_id}`"

        embed = create_embed(title=title_str, description=desc, color=disnake.Color.blue())
        view = disnake.ui.View(timeout=180)

        reg_arg = target_region if target_region else "none"

        # Кнопка 1: Редактировать
        view.add_item(disnake.ui.Button(
            label="Редактировать",
            style=disnake.ButtonStyle.primary,
            custom_id=f"el_act:edit:{el_type}:{reg_arg}"
        ))

        # Кнопка 2: Начать / Закончить
        if status == "active":
            view.add_item(disnake.ui.Button(
                label="Закончить",
                style=disnake.ButtonStyle.danger,
                custom_id=f"el_act:finish:{el_type}:{reg_arg}"
            ))
        elif status == "finished":
            view.add_item(disnake.ui.Button(
                label="Начать новые выборы",
                style=disnake.ButtonStyle.success,
                custom_id=f"el_act:start:{el_type}:{reg_arg}"
            ))
        else:
            view.add_item(disnake.ui.Button(
                label="Начать",
                style=disnake.ButtonStyle.success,
                custom_id=f"el_act:start:{el_type}:{reg_arg}"
            ))

        # Кнопка 3: Назад
        if el_type == "deputy":
            back_custom_id = "el_nav:level:district"
        elif el_type == "governor":
            back_custom_id = "el_nav:gov_menu"
        else:
            back_custom_id = "el_nav:level:federal"

        view.add_item(disnake.ui.Button(
            label="Назад",
            style=disnake.ButtonStyle.secondary,
            custom_id=back_custom_id
        ))

        if inter.response.is_done():
            await inter.edit_original_message(content=None, embed=embed, view=view)
        else:
            await inter.response.edit_message(content=None, embed=embed, view=view)



    @commands.Cog.listener("on_modal_submit")
    async def handle_election_modals(self, inter: disnake.ModalInteraction):
        custom_id = inter.custom_id

        # 0. Создание политической партии
        if custom_id == "party_modal:create":
            if not inter.author.guild_permissions.administrator:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="⛔ Доступ разрешен только администраторам."))],
                    ephemeral=True
                )

            name = inter.text_values.get("party_name", "").strip()
            emoji_raw = inter.text_values.get("party_emoji", "").strip()
            leader_raw = inter.text_values.get("party_leader", "").strip()
            desc = inter.text_values.get("party_desc", "").strip()

            if not name:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ Название партии не может быть пустым."))],
                    ephemeral=True
                )

            existing = await get_party_by_name(name)
            if existing:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content=f"❌ Партия с названием **«{name}»** уже зарегистрирована."))],
                    ephemeral=True
                )

            em_val = None
            if emoji_raw:
                parsed_em, _ = self.parse_candidate_line(emoji_raw, inter.guild)
                em_val = parsed_em or emoji_raw

            leader_id = None
            if leader_raw:
                digits = re.findall(r'\d+', leader_raw)
                if digits:
                    leader_id = int(digits[0])

            await create_party(name=name, emoji=em_val, description=desc, leader_id=leader_id)

            components = await self.build_party_panel_components()
            try:
                await inter.response.edit_message(components=components)
                return await inter.followup.send(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content=f"✅ Партия **«{name}»** успешно создана и внесена в реестр!"))],
                    ephemeral=True
                )
            except Exception:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content=f"✅ Партия **«{name}»** успешно создана и внесена в реестр!"))],
                    ephemeral=True
                )

        # 1. Сохранение списка кандидатов депутатов Сейма (5 округов)
        if custom_id.startswith("el_modal:edit_deputy:"):
            election_id = int(custom_id.split(":")[2])
            election = await get_election_by_id(election_id)
            if not election:
                return await inter.response.send_message("Кампания выборов не найдена.", ephemeral=True)


            parsed_by_district = {}
            total_cands = 0
            for d in DISTRICTS:
                did = d["id"]
                raw_input = inter.text_values.get(f"dep_{did}", "").strip()
                dist_cands = []
                if raw_input:
                    for line in raw_input.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        em_val, rest = self.parse_candidate_line(line, inter.guild)
                        cand_name, party = self.parse_cand_and_party(rest)
                        dist_cands.append({
                            "candidate": cand_name,
                            "party": party,
                            "emoji": em_val
                        })
                        total_cands += 1
                parsed_by_district[did] = dist_cands

            await update_election_candidates(election_id, parsed_by_district)
            await inter.response.send_message(f"Список кандидатов в Сейм обновлен! Всего зарегистрировано: **{total_cands}** канд.", ephemeral=True, delete_after=10)
            return await self.show_election_action_menu(inter, "deputy", None)

        # 2. Сохранение списка кандидатов (Президентские / Губернаторские)
        elif custom_id.startswith("el_modal:edit_candidates:"):
            election_id = int(custom_id.split(":")[2])
            election = await get_election_by_id(election_id)
            if not election:
                return await inter.response.send_message("Кампания выборов не найдена.", ephemeral=True)

            raw_input = inter.text_values.get("candidates_raw", "").strip()
            lines = [l.strip() for l in raw_input.split("\n") if l.strip()]

            parsed_candidates = []
            el_type = election["election_type"]

            for line in lines:
                em_val, rest = self.parse_candidate_line(line, inter.guild)
                if el_type == "president":
                    # Формат: Кандидат - Вице-президент
                    if "-" in rest:
                        parts = rest.split("-", 1)
                        cand = parts[0].strip()
                        dep = parts[1].strip()
                    else:
                        cand = rest.strip()
                        dep = ""
                    parsed_candidates.append({"candidate": cand, "deputy": dep, "emoji": em_val})
                elif el_type == "governor":
                    cand_name, party = self.parse_cand_and_party(rest)
                    parsed_candidates.append({"candidate": cand_name, "party": party, "emoji": em_val})
                else:
                    parsed_candidates.append({"candidate": rest.strip(), "emoji": em_val})

            await update_election_candidates(election_id, parsed_candidates)
            await inter.response.send_message(f"Список успешно обновлен! Всего внесено: **{len(parsed_candidates)}** поз.", ephemeral=True, delete_after=10)

            # Обновляем меню действий
            return await self.show_election_action_menu(inter, election["election_type"], election["target_region"])

        # 3. Запуск выборов в канале
        elif custom_id.startswith("el_modal:start_channel:"):
            election_id = int(custom_id.split(":")[2])
            election = await get_election_by_id(election_id)
            if not election:
                return await inter.response.send_message("Кампания выборов не найдена.", ephemeral=True)

            raw_ch = inter.text_values.get("channel_id_raw", "").strip()
            target_ch = None
            if raw_ch:
                ch_id = int(raw_ch.replace("<#", "").replace(">", ""))
                target_ch = self.bot.get_channel(ch_id)

            el_type = election["election_type"]
            target_region = election["target_region"]

            container_content = (
                "### Выборы в Резендской Республике\n\n"
                "Голосование официально открыто. Граждане могут отдать свой голос через избирательный бюллетень."
            )

            components = [
                disnake.ui.Container(
                    disnake.ui.TextDisplay(content=container_content)
                ),
                disnake.ui.ActionRow(
                    disnake.ui.Button(
                        label="Проголосовать",
                        style=disnake.ButtonStyle.primary,
                        custom_id="election_global_vote_btn"
                    )
                )
            ]

            ann_msg_id = None
            ann_ch_id = None
            if target_ch:
                announcement_msg = await target_ch.send(components=components)
                ann_msg_id = announcement_msg.id
                ann_ch_id = target_ch.id

            await update_election_status(election_id, "active", ann_ch_id, ann_msg_id)

            if target_ch:
                await inter.response.send_message(f"Выборы успешно запущены в канале {target_ch.mention}!", ephemeral=True, delete_after=10)
            else:
                await inter.response.send_message("Выборы успешно запущены! Граждане могут голосовать через постоянную панель.", ephemeral=True, delete_after=10)
            return await self.show_election_action_menu(inter, el_type, target_region)

        # 4. Единый избирательный бюллетень (мульти-голосование)
        elif custom_id == "global_modal_vote":
            recorded_votes = []
            user_reg = await get_user_region(inter.author.id)

            for cid, vals in inter.values.items():
                if not cid.startswith("vote_sel:"):
                    continue
                if not vals:
                    continue

                eid = int(cid.split(":")[1])
                choice = vals[0] if isinstance(vals, list) else vals

                election = await get_election_by_id(eid)
                if not election or election["status"] != "active":
                    continue

                if await has_user_voted_election(eid, inter.author.id):
                    continue

                success = await add_election_vote(eid, inter.author.id, choice)
                if not success:
                    continue

                el_type = election["election_type"]
                target_region = election["target_region"]
                cands_raw = election["candidates"]
                try:
                    candidates_data = json.loads(cands_raw) if cands_raw else None
                except Exception:
                    candidates_data = None

                title_election = "Выборы"
                display_choice = choice
                alert_text = f"Поступил новый голос на выборах за: **{choice}**"
                forum_ch_id = None

                if el_type == "deputy":
                    dist_id, cand_name = choice.split(":", 1) if ":" in choice else ("", choice)
                    dist = get_district_by_id(dist_id)
                    title_election = dist.get("dep_election_title") if dist else "Выборы депутата Сейма"
                    party_str = ""
                    if isinstance(candidates_data, dict):
                        for c in candidates_data.get(dist_id, []):
                            if isinstance(c, dict) and c.get("candidate") == cand_name:
                                if c.get("party"):
                                    party_str = f" ({c['party']})"
                                break
                    elif isinstance(candidates_data, list):
                        for c in candidates_data:
                            if isinstance(c, dict) and c.get("candidate") == cand_name:
                                if c.get("party"):
                                    party_str = f" ({c['party']})"
                                break
                    display_choice = f"{cand_name}{party_str}"
                    alert_text = f"Поступил новый голос на выборах депутата за: **{cand_name}**{party_str}"
                    forum_ch_id = self.get_channel_for_election("deputy", dist_id)

                    if dist and not user_reg:
                        reg_map = {
                            "alvaria": "Альвария",
                            "ledohod": "город Ледоход",
                            "martinia": "Мартиниа",
                            "porelian": "Порелиания",
                            "sancho": "Санчгоу"
                        }
                        if dist_id in reg_map:
                            await set_user_region(inter.author.id, reg_map[dist_id])
                            user_reg = reg_map[dist_id]

                elif el_type == "governor":
                    dist = get_district_by_id(target_region)
                    title_election = dist.get("gov_election_title") if dist else f"Выборы Главы ({target_region})"
                    party_str = ""
                    if isinstance(candidates_data, list):
                        for c in candidates_data:
                            if isinstance(c, dict) and c.get("candidate") == choice:
                                if c.get("party"):
                                    party_str = f" ({c['party']})"
                                break
                    display_choice = f"{choice}{party_str}"
                    leader_title = dist.get("leader_title") if dist else "Главы"
                    alert_text = f"Поступил новый голос на выборах {leader_title} за: **{choice}**{party_str}"
                    forum_ch_id = self.get_channel_for_election("governor", target_region)

                    if dist and not user_reg and target_region:
                        reg_map = {
                            "alvaria": "Альвария",
                            "ledohod": "город Ледоход",
                            "martinia": "Мартиниа",
                            "porelian": "Порелиания",
                            "sancho": "Санчгоу"
                        }
                        if target_region in reg_map:
                            await set_user_region(inter.author.id, reg_map[target_region])
                            user_reg = reg_map[target_region]

                elif el_type == "president":
                    title_election = "Выборы Президента Резендии"
                    display_choice = choice
                    alert_text = f"Поступил новый голос на выборах Президента за: **{choice}**"
                    forum_ch_id = self.get_channel_for_election("president")

                recorded_votes.append({
                    "election_title": title_election,
                    "display_choice": display_choice
                })

                # Отправляем анонимное оповещение в форум-канал
                if forum_ch_id:
                    await self.send_anonymous_vote_alert(forum_ch_id, alert_text)

            if not recorded_votes:
                return await inter.response.send_message(
                    components=[
                        disnake.ui.Container(
                            disnake.ui.TextDisplay(
                                content="Голос не был зафиксирован или вы уже приняли участие в данных выборах."
                            )
                        )
                    ],
                    ephemeral=True
                )

            confirm_lines = ["### Ваш голос успешно учтен!\n"]
            for rv in recorded_votes:
                confirm_lines.append(f"• **{rv['election_title']}**: {rv['display_choice']}")
            confirm_lines.append("\nСпасибо за исполнение гражданского долга!")

            return await inter.response.send_message(
                components=[
                    disnake.ui.Container(
                        disnake.ui.TextDisplay(content="\n".join(confirm_lines))
                    )
                ],
                ephemeral=True
            )

        # 5. Одиночный бюллетень (обратная совместимость)
        elif custom_id.startswith("el_modal:submit_vote:"):
            election_id = int(custom_id.split(":")[2])
            election = await get_election_by_id(election_id)
            if not election or election["status"] != "active":
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="Голосование завершено или недоступно."))],
                    ephemeral=True
                )

            if await has_user_voted_election(election_id, inter.author.id):
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="Вы уже приняли участие в данном голосовании!"))],
                    ephemeral=True
                )

            selected_choices = inter.values.get("vote_choice", [])
            if not selected_choices:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="Вы не выбрали кандидата."))],
                    ephemeral=True
                )

            choice = selected_choices[0]

            success = await add_election_vote(election_id, inter.author.id, choice)
            if not success:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="Не удалось зафиксировать голос. Возможно, вы уже голосовали."))],
                    ephemeral=True
                )

            el_type = election["election_type"]
            target_region = election["target_region"]
            cands_raw = election["candidates"]
            try:
                candidates_data = json.loads(cands_raw) if cands_raw else None
            except Exception:
                candidates_data = None

            forum_ch_id = None
            if el_type == "deputy":
                dist_id, cand_name = choice.split(":", 1) if ":" in choice else ("", choice)
                dist = get_district_by_id(dist_id)
                party_str = ""
                if isinstance(candidates_data, dict):
                    for c in candidates_data.get(dist_id, []):
                        if c.get("candidate") == cand_name:
                            if c.get("party"):
                                party_str = f" ({c['party']})"
                            break

                dist_display = dist["name"] if dist else "Резендия"
                user_resp = (
                    f"**Ваш голос учтен!**\n\n"
                    f"Вы отдали голос за кандидата: **{cand_name}**{party_str}\n"
                    f"Избирательный округ: **{dist_display}**.\n\n"
                    f"Спасибо за исполнение гражданского долга!"
                )
                alert_text = f"Поступил новый голос на выборах депутата за: **{cand_name}**{party_str}"
                forum_ch_id = self.get_channel_for_election("deputy", dist_id)

            elif el_type == "governor":
                dist = get_district_by_id(target_region)
                party_str = ""
                if isinstance(candidates_data, list):
                    for c in candidates_data:
                        if isinstance(c, dict) and c.get("candidate") == choice:
                            if c.get("party"):
                                party_str = f" ({c['party']})"
                            break

                dist_display = dist["name"] if dist else (target_region or "Округ")
                user_resp = (
                    f"**Ваш голос учтен!**\n\n"
                    f"Вы отдали голос за кандидата: **{choice}**{party_str}\n"
                    f"Избирательный округ: **{dist_display}**.\n\n"
                    f"Спасибо за исполнение гражданского долга!"
                )
                leader_title = dist.get("leader_title") if dist else "Главы"
                alert_text = f"Поступил новый голос на выборах {leader_title} за: **{choice}**{party_str}"
                forum_ch_id = self.get_channel_for_election("governor", target_region)

            elif el_type == "president":
                user_resp = (
                    f"**Ваш голос учтен!**\n\n"
                    f"Вы сделали выбор в пользу: **{choice}**.\n\n"
                    f"Спасибо за исполнение гражданского долга!"
                )
                alert_text = f"Поступил новый голос на выборах Президента за: **{choice}**"
                forum_ch_id = self.get_channel_for_election("president")

            else:
                user_resp = f"**Ваш голос учтен!**\n\nВыбор: **{choice}**."
                alert_text = f"Поступил новый голос на выборах за: **{choice}**"

            # Персональный ответ избирателю в контейнере
            await inter.response.send_message(
                components=[disnake.ui.Container(disnake.ui.TextDisplay(content=user_resp))],
                ephemeral=True
            )

            # Отправляем анонимное оповещение в форум-канал
            if forum_ch_id:
                await self.send_anonymous_vote_alert(forum_ch_id, alert_text)





def setup(bot: commands.Bot):
    bot.add_cog(PoliticsCog(bot))