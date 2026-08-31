import json
import os
import time
import aiosqlite
import disnake
from disnake.ext import commands

from functions.db_helpers import create_law_proposal, get_user_info, update_user_info, get_law_proposal, update_law_proposal_status, DB_PATH
from functions.utils import create_embed, UniversalModal, format_number

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

        async with aiosqlite.connect(DB_PATH) as db:
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

        banner_url = "https://media.discordapp.net/attachments/1121508735685234798/1541046906916962364/Picsart_26-08-12_11-18-48-461.png?ex=6a960e85&is=6a94bd05&hm=34014ca7b9fa6544cfa74fc5d9bc831cb45ebdae3a53cfe5883cf64dc9da4275&format=webp&quality=lossless&width=2048&height=683&"

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
            disnake.ui.MediaGallery(disnake.MediaGalleryItem(media=banner_url))
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

        await inter.channel.send(components=components)
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





def setup(bot: commands.Bot):
    bot.add_cog(PoliticsCog(bot))