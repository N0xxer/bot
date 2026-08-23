import json
import os
import time
import aiosqlite
import disnake
from disnake.ext import commands

from functions.db_helpers import get_user_info, update_user_info
from functions.utils import create_embed, UniversalModal

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
ALLOWED_TO_END = ["speaker_of_seim"]


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
        количество: int = commands.Param(description="Количество мандатных мест", ge=1)
    ):
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

            os.makedirs("dbs", exist_ok=True)

            async with aiosqlite.connect("dbs/main.db") as db:
                cursor = await db.execute(
                    """
                    INSERT INTO votings (question, options, votes, start_time, end_time)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (question, json.dumps(["За", "Против", "Воздержаться"]), initial_votes, start_time, end_time)
                )
                poll_id = cursor.lastrowid
                await db.commit()

            json_path = "dbs/votings_temp.json"
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
                f"📊 **Текущие результаты:**\n"
                f"🟢 За: `0` (0.0%)\n"
                f"🔴 Против: `0` (0.0%)\n"
                f"⚪ Воздержались: `0` (0.0%)\n\n"
                f"Всего проголосовало: `0` чел."
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

        async with aiosqlite.connect("dbs/main.db") as db:
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

        # Функция для извлечения выбора и веса голоса (с поддержкой старых записей)
        def parse_vote(entry):
            if isinstance(entry, dict):
                return entry.get("action"), int(entry.get("weight", 1))
            return str(entry), 1

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

            final_desc = (
                f"**Суть вопроса:**\n{question}\n\n"
                f"📌 **Итоги:**\n{verdict}\n\n"
                f"📊 **Распределение голосов (мандатов):**\n"
                f"🟢 За: `{count_for}` ({pct_for:.1f}%)\n"
                f"🔴 Против: `{count_against}` ({pct_against:.1f}%)\n"
                f"⚪ Воздержались: `{count_abstain}` ({pct_abstain:.1f}%)\n\n"
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

        # Получаем количество мандатов пользователя (вес голоса)
        raw_mandates = await get_user_info(inter.author.id, "mandates")
        vote_weight = int(raw_mandates) if raw_mandates and int(raw_mandates) > 0 else 1

        # Записываем действие и вес мандатов
        votes_dict[user_id] = {
            "action": action,
            "weight": vote_weight
        }

        async with aiosqlite.connect("dbs/main.db") as db:
            await db.execute(
                "UPDATE votings SET votes = ? WHERE id = ?",
                (json.dumps(votes_dict), poll_id)
            )
            await db.commit()

        # Подсчет суммы с учетом веса мандатов
        count_for = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "for")
        count_against = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "against")
        count_abstain = sum(parse_vote(v)[1] for v in votes_dict.values() if parse_vote(v)[0] == "abstain")
        total_votes = count_for + count_against + count_abstain
        total_users = len(votes_dict)

        pct_for = (count_for / total_votes * 100) if total_votes > 0 else 0.0
        pct_against = (count_against / total_votes * 100) if total_votes > 0 else 0.0
        pct_abstain = (count_abstain / total_votes * 100) if total_votes > 0 else 0.0

        updated_desc = (
            f"**Суть вопроса:**\n{question}\n\n"
            f"📊 **Текущие результаты (мандаты):**\n"
            f"🟢 За: `{count_for}` ({pct_for:.1f}%)\n"
            f"🔴 Против: `{count_against}` ({pct_against:.1f}%)\n"
            f"⚪ Воздержались: `{count_abstain}` ({pct_abstain:.1f}%)\n\n"
            f"Всего голосов: `{total_votes}` | Проголосовало: `{total_users}` чел."
        )

        updated_embed = create_embed(
            title="🗳️ Политическое голосование",
            description=updated_desc,
            color=disnake.Color.blurple(),
            footer_text=inter.message.embeds[0].footer.text if inter.message.embeds else None
        )

        await inter.response.edit_message(embed=updated_embed)


def setup(bot: commands.Bot):
    bot.add_cog(PoliticsCog(bot))