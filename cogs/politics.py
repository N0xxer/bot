import json
import os
import re
import time
import aiosqlite
import disnake
from disnake.ext import commands
from typing import Optional, Tuple

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
    def parse_candidate_line(line: str, guild: Optional[disnake.Guild] = None) -> Tuple[Optional[str], str]:
        """
        Извлекает эмодзи (кастомный эмодзи сервера, стандартный флаг/юникод) из начала строки.
        Поддерживает форматы:
        • <:name:id> Название
        • <a:name:id> Название
        • :name: Название (если эмодзи с таким именем есть на сервере)
        • 🚩 Название / 🇷🇺 Название
        • Эмодзи | Название
        """
        line = line.strip()
        if not line:
            return None, ""

        emoji_pattern = re.compile(
            r'^('
            r'<a?:[a-zA-Z0-9_]+:[0-9]{17,20}>'
            r'|[\U0001F1E6-\U0001F1FF]{2}'
            r'|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B50-\u2B55\u200D\uFE0F]+'
            r'|:[a-zA-Z0-9_]+:'
            r')\s*\|?\s*(.*)',
            re.UNICODE
        )

        m = emoji_pattern.match(line)
        if not m:
            return None, line

        token = m.group(1).strip()
        rest = m.group(2).strip()

        # Если задан шорткод :name:
        if token.startswith(":") and token.endswith(":") and not token.startswith("<"):
            if guild:
                clean_name = token[1:-1]
                found_emoji = disnake.utils.get(guild.emojis, name=clean_name)
                if found_emoji:
                    return str(found_emoji), rest
            # Если не найден среди эмодзи сервера — возвращаем как обычный текст
            return None, line

        return token, rest

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
            title="🗳️ Избирательная комиссия Резендии",
            description=(
                "Добро пожаловать в панель управления выборами.\n\n"
                "Выберите уровень выборов для настройки и проведения:\n"
                "• **🏛️ Федеральные** (Президентские / Парламентские)\n"
                "• **📍 Земельные** (Выборы глав регионов)"
            ),
            color=disnake.Color.blue()
        )
        view = disnake.ui.View(timeout=180)
        view.add_item(disnake.ui.Button(
            label="Федеральные",
            style=disnake.ButtonStyle.primary,
            emoji="🏛️",
            custom_id="el_nav:level:federal"
        ))
        view.add_item(disnake.ui.Button(
            label="Земельные",
            style=disnake.ButtonStyle.secondary,
            emoji="📍",
            custom_id="el_nav:level:regional"
        ))
        await inter.edit_original_message(embed=embed, view=view)

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
                return await inter.response.send_message("⛔ Доступ разрешен только администраторам.", ephemeral=True)

            parts = custom_id.split(":")
            step = parts[1]

            # ШАГ 1 -> ШАГ 2: Выбор уровня (Федеральные / Земельные)
            if step == "level":
                level = parts[2]
                if level == "federal":
                    embed = create_embed(
                        title="🏛️ Федеральные выборы Резендии",
                        description="Выберите тип федеральных выборов:\n\n• **👑 Президентские** (Кандидат в Президенты + Вице-президент)\n• **🏛️ Парламентские** (Политические партии)",
                        color=disnake.Color.blue()
                    )
                    view = disnake.ui.View(timeout=180)
                    view.add_item(disnake.ui.Button(
                        label="Президентские",
                        style=disnake.ButtonStyle.primary,
                        emoji="👑",
                        custom_id="el_nav:type:president"
                    ))
                    view.add_item(disnake.ui.Button(
                        label="Парламентские",
                        style=disnake.ButtonStyle.primary,
                        emoji="🏛️",
                        custom_id="el_nav:type:parliament"
                    ))
                    view.add_item(disnake.ui.Button(
                        label="Назад",
                        style=disnake.ButtonStyle.secondary,
                        emoji="⬅️",
                        custom_id="el_nav:back:root"
                    ))
                    return await inter.response.edit_message(embed=embed, view=view)

                elif level == "regional":
                    if not REGIONS_LIST:
                        return await inter.response.edit_message(content="❌ Список регионов в конфигурации пуст.", embed=None, view=None)

                    options = [
                        disnake.SelectOption(label=reg, value=reg, emoji="📍")
                        for reg in REGIONS_LIST[:25]
                    ]
                    embed = create_embed(
                        title="📍 Земельные выборы (Выборы главы региона)",
                        description="Выберите регион государства, в котором проводятся выборы главы:",
                        color=disnake.Color.blue()
                    )
                    view = disnake.ui.View(timeout=180)
                    select = disnake.ui.StringSelect(
                        custom_id="el_nav_select:region",
                        placeholder="Выберите регион для выборов",
                        options=options
                    )
                    view.add_item(select)
                    view.add_item(disnake.ui.Button(
                        label="Назад",
                        style=disnake.ButtonStyle.secondary,
                        emoji="⬅️",
                        custom_id="el_nav:back:root"
                    ))
                    return await inter.response.edit_message(embed=embed, view=view)

            elif step == "back" and parts[2] == "root":
                embed = create_embed(
                    title="🗳️ Избирательная комиссия Резендии",
                    description=(
                        "Добро пожаловать в панель управления выборами.\n\n"
                        "Выберите уровень выборов для настройки и проведения:\n"
                        "• **🏛️ Федеральные** (Президентские / Парламентские)\n"
                        "• **📍 Земельные** (Выборы глав регионов)"
                    ),
                    color=disnake.Color.blue()
                )
                view = disnake.ui.View(timeout=180)
                view.add_item(disnake.ui.Button(
                    label="Федеральные",
                    style=disnake.ButtonStyle.primary,
                    emoji="🏛️",
                    custom_id="el_nav:level:federal"
                ))
                view.add_item(disnake.ui.Button(
                    label="Земельные",
                    style=disnake.ButtonStyle.secondary,
                    emoji="📍",
                    custom_id="el_nav:level:regional"
                ))
                return await inter.response.edit_message(embed=embed, view=view)

            # ШАГ 2 -> ШАГ 3: Выбран тип выборов (Президентские / Парламентские)
            elif step == "type":
                el_type = parts[2]
                return await self.show_election_action_menu(inter, el_type, target_region=None)

        # -----------------------------------------------------------
        # 2. ДЕЙСТВИЯ: РЕДАКТИРОВАТЬ / НАЧАТЬ / ЗАКОНЧИТЬ
        # -----------------------------------------------------------
        elif custom_id.startswith("el_act:"):
            if not inter.author.guild_permissions.administrator:
                return await inter.response.send_message("⛔ Доступ разрешен только администраторам.", ephemeral=True)

            parts = custom_id.split(":")
            action = parts[1]
            el_type = parts[2]
            target_region = parts[3] if len(parts) > 3 and parts[3] != "none" else None

            election = await get_latest_election(el_type, target_region)
            if not election:
                eid = await create_election(el_type, target_region)
                election = await get_election_by_id(eid)

            election_id = election["id"]

            # Действие: Редактировать список кандидатов / партий
            if action == "edit":
                try:
                    candidates_data = json.loads(election["candidates"]) if election["candidates"] else []
                except Exception:
                    candidates_data = []

                if el_type == "president":
                    # Президентские: Кандидат в Президенты - Вице-президент
                    cur_lines = []
                    for c in candidates_data:
                        if isinstance(c, dict):
                            em_prefix = f"{c['emoji']} " if c.get("emoji") else ""
                            cur_lines.append(f"{em_prefix}{c['candidate']} - {c.get('deputy', '')}")
                        else:
                            cur_lines.append(str(c))
                    cur_text = "\n".join(cur_lines)
                    modal = disnake.ui.Modal(
                        title="Кандидаты: Президентские",
                        custom_id=f"el_modal:edit_candidates:{election_id}",
                        components=[
                            disnake.ui.TextInput(
                                label="Список: Кандидат - Вице-президент",
                                placeholder="🚩 Иван Иванов - Петр Петров\n<:flag:123> Алексей Смирнов - Сергей Сидоров",
                                value=cur_text[:4000],
                                custom_id="candidates_raw",
                                style=disnake.TextInputStyle.paragraph,
                                required=True,
                                max_length=4000
                            )
                        ]
                    )
                    return await inter.response.send_modal(modal=modal)

                elif el_type == "parliament":
                    # Парламентские: Политические партии
                    cur_lines = []
                    for c in candidates_data:
                        if isinstance(c, dict):
                            em_prefix = f"{c['emoji']} " if c.get("emoji") else ""
                            cur_lines.append(f"{em_prefix}{c.get('candidate', '')}")
                        else:
                            cur_lines.append(str(c))
                    if not cur_lines:
                        parties = await get_all_parties()
                        if parties:
                            cur_lines = [f"{p.get('emoji', '')} {p['name']}".strip() for p in parties]
                    cur_text = "\n".join(cur_lines)
                    modal = disnake.ui.Modal(
                        title="Партии: Парламентские выборы",
                        custom_id=f"el_modal:edit_candidates:{election_id}",
                        components=[
                            disnake.ui.TextInput(
                                label="Список партий (по одной в строке)",
                                placeholder="🚩 Либеральная партия\n<:flag:123> Консервативная партия Резендии",
                                value=cur_text[:4000],
                                custom_id="candidates_raw",
                                style=disnake.TextInputStyle.paragraph,
                                required=True,
                                max_length=4000
                            )
                        ]
                    )
                    return await inter.response.send_modal(modal=modal)

                elif el_type == "regional":
                    # Земельные: Кандидаты в главы региона (без зама)
                    cur_lines = []
                    for c in candidates_data:
                        if isinstance(c, dict):
                            em_prefix = f"{c['emoji']} " if c.get("emoji") else ""
                            cur_lines.append(f"{em_prefix}{c.get('candidate', '')}")
                        else:
                            cur_lines.append(str(c))
                    cur_text = "\n".join(cur_lines)
                    modal = disnake.ui.Modal(
                        title=f"Кандидаты: {target_region}"[:45],
                        custom_id=f"el_modal:edit_candidates:{election_id}",
                        components=[
                            disnake.ui.TextInput(
                                label=f"Кандидаты в главы {target_region}"[:45],
                                placeholder="⭐ Иван Иванов\n<:flag:123> Петр Петров",
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
                    return await inter.response.send_message("ℹ️ Голосование уже активно!", ephemeral=True)

                candidates = json.loads(election["candidates"]) if election["candidates"] else []
                if not candidates:
                    return await inter.response.send_message("❌ Сначала заполните список кандидатов/партий через кнопку «Редактировать»!", ephemeral=True)

                # Запрашиваем ID канала для отправки объявления (модалка)
                modal = disnake.ui.Modal(
                    title="Запуск выборов",
                    custom_id=f"el_modal:start_channel:{election_id}",
                    components=[
                        disnake.ui.TextInput(
                            label="ID канала для объявления и голосования",
                            placeholder="Например: 123456789012345678 (пусто = этот канал)",
                            custom_id="channel_id_raw",
                            style=disnake.TextInputStyle.short,
                            required=False,
                            max_length=30
                        )
                    ]
                )
                return await inter.response.send_modal(modal=modal)

            # Действие: Закончить выборы
            elif action == "finish":
                if election["status"] != "active":
                    return await inter.response.send_message("❌ Выборы не находятся в активной фазе голосования.", ephemeral=True)

                await inter.response.defer(ephemeral=True)

                # 1. Подсчет голосов
                votes_summary = await get_election_votes_summary(election_id)
                total_votes = sum(votes_summary.values())

                candidates_data = json.loads(election["candidates"]) if election["candidates"] else []
                cand_emoji_map = {}
                default_icon = "👑" if el_type == "president" else ("🏛️" if el_type == "parliament" else "📍")
                for c in candidates_data:
                    if isinstance(c, dict):
                        c_name = c.get("candidate", "")
                        em = c.get("emoji") or default_icon
                        if el_type == "president":
                            dep = c.get("deputy", "")
                            lbl = f"{c_name} (Вице: {dep})" if dep else c_name
                        else:
                            lbl = c_name
                        cand_emoji_map[lbl] = em
                        cand_emoji_map[c_name] = em

                title_map = {
                    "president": "👑 Официальные итоги Президентских выборов",
                    "parliament": "🏛️ Официальные итоги Парламентских выборов",
                    "regional": f"📍 Официальные итоги выборов главы региона «{target_region}»"
                }

                lines = []
                lines.append(f"Всего проголосовало: **{total_votes}** избирателей.\n")

                winner = None
                winner_votes = -1

                if not votes_summary:
                    lines.append("*В голосовании не было отдано ни одного голоса.*")
                else:
                    lines.append("### Результаты голосования:")
                    for candidate_name, count in votes_summary.items():
                        percent = (count / total_votes * 100) if total_votes > 0 else 0.0
                        c_icon = cand_emoji_map.get(candidate_name, default_icon)
                        lines.append(f"• {c_icon} **{candidate_name}**: `{count}` голосов ({percent:.1f}%)")
                        if count > winner_votes:
                            winner_votes = count
                            winner = candidate_name

                    if winner:
                        win_icon = cand_emoji_map.get(winner, "🏆")
                        lines.append(f"\n🏆 **Победитель:** {win_icon} **{winner}** с результатом `{winner_votes}` голосов!")

                title_text = title_map.get(el_type, "🗳️ Официальные итоги выборов")
                result_content = f"### {title_text}\n\n" + "\n".join(lines)
                result_components = [
                    disnake.ui.Container(
                        disnake.ui.TextDisplay(content=result_content)
                    )
                ]

                # 2. Обновляем сообщение в канале выборов (публикуем итоги в контейнере)
                if election["announcement_channel_id"] and election["announcement_message_id"]:
                    ch = self.bot.get_channel(election["announcement_channel_id"])
                    if ch:
                        try:
                            msg = await ch.fetch_message(election["announcement_message_id"])
                            await msg.edit(content=None, embed=None, view=None, components=result_components)
                        except Exception:
                            pass

                # 3. Переводим статус в finished
                await update_election_status(election_id, "finished")

                # 4. Обновляем панель управления
                await self.show_election_action_menu(inter, el_type, target_region)
                await inter.followup.send(content="✅ Выборы успешно завершены, итоги опубликованы!", ephemeral=True)

        # -----------------------------------------------------------
        # 3. КНОПКА ГОЛОСОВАНИЯ ДЛЯ ИЗБИРАТЕЛЕЙ
        # -----------------------------------------------------------
        elif custom_id.startswith("election_vote_btn:"):
            election_id = int(custom_id.split(":")[1])
            election = await get_election_by_id(election_id)
            if not election:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ Данные выборов не найдены."))],
                    ephemeral=True
                )

            if election["status"] != "active":
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="⛔ Голосование на данных выборах уже завершено или не началось."))],
                    ephemeral=True
                )

            if not await ensure_user_registered(inter, inter.author.id):
                return

            el_type = election["election_type"]
            target_region = election["target_region"]

            # Проверка прописки для Земельных выборов
            if el_type == "regional":
                user_reg = await get_user_region(inter.author.id)
                if not user_reg or user_reg.strip().lower() != target_region.strip().lower():
                    return await inter.response.send_message(
                        components=[
                            disnake.ui.Container(
                                disnake.ui.TextDisplay(
                                    content=f"⛔ **Отказано в доступе к голосованию!**\n\nВыборы проводятся исключительно для жителей региона **«{target_region}»**.\nВаша прописка: `{user_reg or 'Не указана'}`."
                                )
                            )
                        ],
                        ephemeral=True
                    )

            # Проверка: голосовал ли уже гражданин
            if await has_user_voted_election(election_id, inter.author.id):
                return await inter.response.send_message(
                    components=[
                        disnake.ui.Container(
                            disnake.ui.TextDisplay(
                                content="⚠️ **Вы уже приняли участие в данном голосовании!**\n\nПовторный голос строго запрещен законом."
                            )
                        )
                    ],
                    ephemeral=True
                )

            candidates_data = json.loads(election["candidates"]) if election["candidates"] else []
            if not candidates_data:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ Список кандидатов пуст."))],
                    ephemeral=True
                )

            options = []
            default_icon = "👑" if el_type == "president" else ("🏛️" if el_type == "parliament" else "📍")

            for item in candidates_data[:25]:
                if isinstance(item, dict):
                    cand = item.get("candidate", "")
                    item_emoji = item.get("emoji") or default_icon
                    if el_type == "president":
                        dep = item.get("deputy", "")
                        label = f"{cand} (Вице: {dep})" if dep else cand
                    else:
                        label = cand

                    try:
                        opt = disnake.SelectOption(label=label[:100], value=label[:100], emoji=item_emoji)
                    except Exception:
                        opt = disnake.SelectOption(label=label[:100], value=label[:100], emoji=default_icon)
                    options.append(opt)
                else:
                    options.append(disnake.SelectOption(label=str(item)[:100], value=str(item)[:100], emoji=default_icon))

            modal = disnake.ui.Modal(
                title="Бюллетень для голосования",
                custom_id=f"el_modal:submit_vote:{election_id}",
                components=[
                    disnake.ui.Label(
                        text="Ваш выбор в избирательном бюллетене",
                        component=disnake.ui.StringSelect(
                            custom_id="vote_choice",
                            placeholder="Выберите кандидата / партию",
                            options=options,
                            min_values=1,
                            max_values=1
                        )
                    )
                ]
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
                return await inter.response.send_message("⛔ Доступ разрешен только администраторам.", ephemeral=True)

            selected_region = inter.values[0]
            return await self.show_election_action_menu(inter, "regional", selected_region)


    async def show_election_action_menu(self, inter: disnake.MessageInteraction, el_type: str, target_region: Optional[str] = None):
        """Отображает Шаг 3: Меню с кнопками Редактировать и Начать/Закончить."""
        election = await get_latest_election(el_type, target_region)
        if not election:
            eid = await create_election(el_type, target_region)
            election = await get_election_by_id(eid)

        election_id = election["id"]
        status = election["status"]

        candidates = json.loads(election["candidates"]) if election["candidates"] else []

        type_names = {
            "president": "👑 Президентские выборы",
            "parliament": "🏛️ Парламентские выборы",
            "regional": f"📍 Выборы главы региона «{target_region}»"
        }
        title_str = type_names.get(el_type, "Выборы")

        status_text = {
            "draft": "📝 Черновик (настройка)",
            "active": "🟢 Идёт голосование",
            "finished": "🏁 Завершены"
        }.get(status, status)

        cand_lines = []
        default_icon = "👑" if el_type == "president" else ("🏛️" if el_type == "parliament" else "📍")
        if not candidates:
            cand_lines.append("*Список кандидатов/партий пуст.*")
        else:
            for idx, c in enumerate(candidates, 1):
                if el_type == "president" and isinstance(c, dict):
                    em = c.get("emoji") or default_icon
                    cand_lines.append(f"{idx}. {em} **{c['candidate']}** (Вице: *{c.get('deputy', '—')}*)")
                elif isinstance(c, dict):
                    em = c.get("emoji") or default_icon
                    cand_lines.append(f"{idx}. {em} **{c.get('candidate', '')}**")
                else:
                    cand_lines.append(f"{idx}. {default_icon} **{c}**")

        desc = (
            f"• **Статус:** {status_text}\n"
            f"• **ID кампании:** `#{election_id}`\n\n"
            f"**Зарегистрированные участники:**\n"
            + "\n".join(cand_lines) + "\n\n"
            f"Используйте кнопки ниже для редактирования или изменения статуса выборов."
        )

        embed = create_embed(title=title_str, description=desc, color=disnake.Color.blue())
        view = disnake.ui.View(timeout=180)

        reg_arg = target_region if target_region else "none"

        view.add_item(disnake.ui.Button(
            label="Редактировать",
            style=disnake.ButtonStyle.primary,
            emoji="✏️",
            custom_id=f"el_act:edit:{el_type}:{reg_arg}"
        ))

        if status == "active":
            view.add_item(disnake.ui.Button(
                label="Закончить",
                style=disnake.ButtonStyle.danger,
                emoji="⏹️",
                custom_id=f"el_act:finish:{el_type}:{reg_arg}"
            ))
        else:
            view.add_item(disnake.ui.Button(
                label="Начать",
                style=disnake.ButtonStyle.success,
                emoji="▶️",
                custom_id=f"el_act:start:{el_type}:{reg_arg}"
            ))

        back_custom_id = "el_nav:level:federal" if el_type in ["president", "parliament"] else "el_nav:level:regional"
        view.add_item(disnake.ui.Button(
            label="Назад",
            style=disnake.ButtonStyle.secondary,
            emoji="⬅️",
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

        # 1. Сохранение списка кандидатов/партий
        if custom_id.startswith("el_modal:edit_candidates:"):
            election_id = int(custom_id.split(":")[2])
            election = await get_election_by_id(election_id)
            if not election:
                return await inter.response.send_message("❌ Кампания выборов не найдена.", ephemeral=True)

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
                else:
                    parsed_candidates.append({"candidate": rest.strip(), "emoji": em_val})

            await update_election_candidates(election_id, parsed_candidates)
            await inter.response.send_message(f"✅ Список успешно обновлен! Всего внесено: **{len(parsed_candidates)}** поз.", ephemeral=True, delete_after=10)

            # Обновляем меню действий
            return await self.show_election_action_menu(inter, election["election_type"], election["target_region"])

        # 2. Запуск выборов в канале
        elif custom_id.startswith("el_modal:start_channel:"):
            election_id = int(custom_id.split(":")[2])
            election = await get_election_by_id(election_id)
            if not election:
                return await inter.response.send_message("❌ Кампания выборов не найдена.", ephemeral=True)

            raw_ch = inter.text_values.get("channel_id_raw", "").strip()
            target_ch = inter.channel
            if raw_ch:
                ch_id = int(raw_ch.replace("<#", "").replace(">", ""))
                found_ch = self.bot.get_channel(ch_id)
                if found_ch:
                    target_ch = found_ch

            el_type = election["election_type"]
            target_region = election["target_region"]
            candidates = json.loads(election["candidates"]) if election["candidates"] else []

            title_map = {
                "president": "👑 Выборы Президента Резендии",
                "parliament": "🏛️ Парламентские выборы Резендии",
                "regional": f"📍 Выборы главы региона «{target_region}»"
            }
            title_text = title_map.get(el_type, "Выборы")

            desc_lines = [
                f"### {title_text}\n",
                "Граждане Резендии! Официально объявлен старт всеобщего голосования.\n\n",
                "**Зарегистрированные кандидаты / партии:**\n"
            ]

            default_icon = "👑" if el_type == "president" else ("🏛️" if el_type == "parliament" else "📍")
            for idx, c in enumerate(candidates, 1):
                if el_type == "president" and isinstance(c, dict):
                    em = c.get("emoji") or default_icon
                    desc_lines.append(f"{idx}. {em} **{c['candidate']}** (Вице-президент: *{c.get('deputy', '—')}*)\n")
                elif isinstance(c, dict):
                    em = c.get("emoji") or default_icon
                    desc_lines.append(f"{idx}. {em} **{c.get('candidate', '')}**\n")
                else:
                    desc_lines.append(f"{idx}. {default_icon} **{c}**\n")

            region_note = f"\n⚠️ *В голосовании могут участвовать только граждане с официальной пропиской в регионе «{target_region}».*\n" if el_type == "regional" else ""
            desc_lines.append(f"\nНажмите кнопку ниже, чтобы открыть бюллетень и сделать свой выбор!{region_note}")

            container_content = "".join(desc_lines)
            components = [
                disnake.ui.Container(
                    disnake.ui.TextDisplay(content=container_content)
                ),
                disnake.ui.ActionRow(
                    disnake.ui.Button(
                        label="Проголосовать",
                        style=disnake.ButtonStyle.success,
                        emoji="🗳️",
                        custom_id=f"election_vote_btn:{election_id}"
                    )
                )
            ]

            announcement_msg = await target_ch.send(components=components)
            await update_election_status(election_id, "active", target_ch.id, announcement_msg.id)

            await inter.response.send_message(f"✅ Выборы успешно запущены в канале {target_ch.mention}!", ephemeral=True, delete_after=10)
            return await self.show_election_action_menu(inter, el_type, target_region)

        # 3. Фиксация голоса гражданина
        elif custom_id.startswith("el_modal:submit_vote:"):
            election_id = int(custom_id.split(":")[2])
            election = await get_election_by_id(election_id)
            if not election or election["status"] != "active":
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="⛔ Голосование завершено или недоступно."))],
                    ephemeral=True
                )

            # Проверка повторного голосования
            if await has_user_voted_election(election_id, inter.author.id):
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="⚠️ Вы уже приняли участие в данном голосовании!"))],
                    ephemeral=True
                )

            selected_choices = inter.values.get("vote_choice", [])
            if not selected_choices:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ Вы не выбрали кандидата."))],
                    ephemeral=True
                )

            choice = selected_choices[0]

            success = await add_election_vote(election_id, inter.author.id, choice)
            if not success:
                return await inter.response.send_message(
                    components=[disnake.ui.Container(disnake.ui.TextDisplay(content="❌ Не удалось зафиксировать голос. Возможно, вы уже голосовали."))],
                    ephemeral=True
                )

            # Ищем эмодзи выбранного кандидата / партии
            choice_emoji = None
            try:
                cands_list = json.loads(election["candidates"]) if election["candidates"] else []
                for c in cands_list:
                    if isinstance(c, dict):
                        cand_name = c.get("candidate", "")
                        if election["election_type"] == "president":
                            dep = c.get("deputy", "")
                            lbl = f"{cand_name} (Вице: {dep})" if dep else cand_name
                        else:
                            lbl = cand_name
                        if lbl == choice or cand_name == choice:
                            choice_emoji = c.get("emoji")
                            break
            except Exception:
                pass

            em_display = f"{choice_emoji} " if choice_emoji else ""

            # Персональный ответ избирателю в контейнере
            vote_resp_components = [
                disnake.ui.Container(
                    disnake.ui.TextDisplay(
                        content=f"✅ **Ваш голос учтен!**\n\nВы сделали выбор в пользу: {em_display}**{choice}**.\nСпасибо за исполнение гражданского долга!"
                    )
                )
            ]
            await inter.response.send_message(
                components=vote_resp_components,
                ephemeral=True
            )

            # Анонимное оповещение в канал выборов в контейнере
            if election["announcement_channel_id"]:
                ch = self.bot.get_channel(election["announcement_channel_id"])
                if ch:
                    try:
                        alert_components = [
                            disnake.ui.Container(
                                disnake.ui.TextDisplay(
                                    content=f"🗳️ Был отдан новый голос за: {em_display}**{choice}**!"
                                )
                            )
                        ]
                        await ch.send(
                            components=alert_components,
                            delete_after=120
                        )
                    except Exception:
                        pass





def setup(bot: commands.Bot):
    bot.add_cog(PoliticsCog(bot))