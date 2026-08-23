import json
import time
import disnake
from disnake.ext import commands
import aiosqlite

from functions.db_helpers import get_user_info, update_user_info
from functions.utils import create_embed, format_number, UniversalModal

# Загрузка конфигурации ролей
with open("configs/function_roles_config.json", "r", encoding="utf-8") as f:
    ROLES_CONFIG = json.load(f)


def parse_roles(raw_roles) -> list:
    """Безопасный парсинг списка должностей."""
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



class EconomyCog(commands.Cog):
    """Модуль экономической системы бота."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.starting_balance = 25000



    @commands.slash_command(
        name="profile",
        description="Посмотреть государственный паспорт / профиль гражданина"
    )
    async def profile(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пользователь: disnake.Member = None
    ):
        """Карточка гражданина: ФИО, баланс, список должностей и доходы."""
        target = пользователь or inter.author
        target_id = target.id

        # 1. Получение данных пользователя из БД
        balance = await get_user_info(target_id, "balance") or 0
        fio = await get_user_info(target_id, "FIO")
        roles_raw = await get_user_info(target_id, "functions")
        photo_url = await get_user_info(target_id, "photo")
        last_collection = await get_user_info(target_id, "last_collection")
        raw_mandates = await get_user_info(target_id, "mandates")

        # 2. Формирование строки мандатов
        mandates_display = ""
        if raw_mandates and int(raw_mandates) > 0:
            mandates_display = f"📜 **Количество мандатных мест:** `{raw_mandates}`\n"

        # 3. Форматирование должностей и подсчет суммарного дохода
        user_roles = parse_roles(roles_raw)
        total_income = 0
        roles_list_str = []

        for role_key in user_roles:
            if role_key in ROLES_CONFIG:
                role_data = ROLES_CONFIG[role_key]
                role_name = role_data.get("name", role_key)
                income = role_data.get("income", 0)
                total_income += income
                roles_list_str.append(f"• **{role_name}** (`+{format_number(income)}` R$ / 3 дня)")
            else:
                roles_list_str.append(f"• **{role_key}**")

        roles_display = "\n".join(roles_list_str) if roles_list_str else "Отсутствуют"
        fio_display = fio if fio else "Не заполнено"

        # 4. Сборка описания эмбеда
        desc = (
            f"👤 **ФИО:** `{fio_display}`\n"
            f"💳 **Баланс:** `{format_number(balance)}` R$\n"
            f"📈 **Суммарная зарплата:** `{format_number(total_income)}` R$ / 3 дня\n"
            f"{mandates_display}\n"
            f"🏛️ **Государственные должности:**\n"
            f"{roles_display}"
        )

        embed = create_embed(
            title=f"📋 Личное дело: {target.display_name}",
            description=desc,
            color=disnake.Color.blue(),
            footer_text=f"ID: {target.id}"
        )

        # Фото гражданина (если задан URL) или аватарка Discord
        if photo_url and photo_url.startswith("http"):
            embed.set_thumbnail(url=photo_url)
        else:
            embed.set_thumbnail(url=target.display_avatar.url)

        await inter.response.send_message(embed=embed)

        

    @commands.slash_command(
        name="balance",
        description="Посмотреть свой баланс или баланс другого пользователя"
    )
    async def balance(
        self, 
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = None
    ):
        """Слэш-команда проверки баланса."""
        target = member or inter.author
        
        # Получаем данные из функции БД
        user_balance = await get_user_info(target.id, "balance") or 0
        
        # Создаем красивый эмбед через utils
        embed = create_embed(
            title=f"Баланс: {target.display_name}",
            description=f"На счету пользователя: `{format_number(user_balance)}` R$.",
            color=disnake.Color.green()
        )
        
        await inter.response.send_message(embed=embed)



    @commands.slash_command(
        name="collect",
        description="Получить прибыль от своей деятельности"
    )
    async def collect(self, inter: disnake.ApplicationCommandInteraction):
        user_id = inter.author.id
        current_time = int(time.time())

        balance = await get_user_info(user_id, "balance") or 0
        last_collection = await get_user_info(user_id, "last_collection")
        roles = await get_user_info(user_id, "functions")
        COOLDOWN_SECONDS = 3 * 24 * 60 * 60

        # Проверка кулдауна
        if last_collection:
            time_passed = current_time - int(last_collection)
            if time_passed < COOLDOWN_SECONDS:
                ready_timestamp = int(last_collection) + COOLDOWN_SECONDS
                embed = create_embed(
                    title="⏳ Сбор недоступен",
                    description=f"Вы уже собирали прибыль.\nСледующий сбор доступен: <t:{ready_timestamp}:R> (<t:{ready_timestamp}:F>).",
                    color=disnake.Color.red()
                )
                return await inter.response.send_message(embed=embed, ephemeral=True)

        # Парсинг ролей
        user_roles = []
        if roles:
            try:
                user_roles = json.loads(roles) if roles.startswith("[") else [r.strip() for r in roles.split(",")]
            except Exception:
                user_roles = [roles]

        if not user_roles:
            embed = create_embed(
                title="❌ Нет доступной прибыли",
                description="У вас нет активных должностей для получения дохода.",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        # Расчет начислений
        total_income = 0
        collected_details = []

        for role_key in user_roles:
            if role_key in ROLES_CONFIG:
                role_data = ROLES_CONFIG[role_key]
                income = role_data.get("income", 0)
                total_income += income
                collected_details.append(f"• **{role_data.get('name', role_key)}**: `{format_number(income)}` R$")

        if total_income == 0:
            embed = create_embed(
                title="❌ Нет доступной прибыли",
                description="Ваши текущие роли не приносят дохода.",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        # Сохранение в БД
        new_balance = balance + total_income
        await update_user_info(user_id, "balance", str(new_balance))
        await update_user_info(user_id, "last_collection", str(current_time))

        # Ответ пользователю
        desc = (
            f"Вы успешно получили выплату за свои должности!\n\n"
            f"**Начисления:**\n" + "\n".join(collected_details) + "\n\n"
            f"**Итого начислено:** `+{format_number(total_income)}` R$\n"
            f"**Текущий баланс:** `{format_number(new_balance)}` R$"
        )

        embed = create_embed(
            title="💰 Государственная казна / Доход",
            description=desc,
            color=disnake.Color.green(),
            footer_text="Следующий сбор через 3 дня"
        )
        await inter.response.send_message(embed=embed)



    @commands.slash_command(name="money", description="Управление деньгами: перевод, выдача, снятие")
    async def money(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @money.sub_command(
        name="transfer",
        description="Перевести деньги другому пользователю"
    )
    async def transfer(self, inter: disnake.ApplicationCommandInteraction, пользователь: disnake.Member, количество: int):
        """Перевод средств между пользователями."""
        sender_id = inter.author.id
        receiver_id = пользователь.id

        if sender_id == receiver_id:
            embed = create_embed(
                title="❌ Ошибка перевода",
                description="Вы не можете переводить деньги самому себе.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        if количество <= 0:
            embed = create_embed(
                title="❌ Некорректная сумма",
                description="Сумма перевода должна быть положительным числом.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        sender_balance = await get_user_info(sender_id, "balance") or 0
        if sender_balance < количество:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=f"У вас недостаточно средств для перевода `{format_number(количество)}` R$.\nВаш текущий баланс: `{format_number(sender_balance)}` R$.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        # Обновление балансов в БД
        new_sender_balance = sender_balance - количество
        receiver_balance = await get_user_info(receiver_id, "balance") or 0
        new_receiver_balance = receiver_balance + количество

        await update_user_info(sender_id, "balance", str(new_sender_balance))
        await update_user_info(receiver_id, "balance", str(new_receiver_balance))

        # Ответ пользователю
        embed_sender = create_embed(
            title="✅ Перевод выполнен",
            description=(
                f"Вы успешно перевели `{format_number(количество)}` R$ пользователю {пользователь.mention}.\n"
                f"Ваш новый баланс: `{format_number(new_sender_balance)}` R$."
            ),
            color=disnake.Color.green()
        )
        embed_receiver = create_embed(
            title="💸 Вам перевели деньги",
            description=(
                f"Вам успешно перевели `{format_number(количество)}` R$ пользователем {inter.author.mention}.\n"
                f"Ваш новый баланс: `{format_number(new_receiver_balance)}` R$."
            ),
            color=disnake.Color.green()
        )
        await inter.response.send_message(embed=embed_sender)
        await пользователь.send(embed=embed_receiver)


    @money.sub_command(
        name="add",
        description="Выдать деньги пользователю (Админ)"
    )
    @commands.has_permissions(administrator=True)
    async def add(self, inter: disnake.ApplicationCommandInteraction, пользователь: disnake.Member, количество: int):
        """Выдача средств пользователю администратором."""
        if количество <= 0:
            embed = create_embed(
                title="❌ Некорректная сумма",
                description="Сумма выдачи должна быть положительным числом.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        receiver_id = пользователь.id
        receiver_balance = await get_user_info(receiver_id, "balance") or 0
        new_receiver_balance = receiver_balance + количество

        await update_user_info(receiver_id, "balance", str(new_receiver_balance))

        embed_admin = create_embed(
            title="✅ Средства выданы",
            description=(
                f"Вы успешно выдали `{format_number(количество)}` R$ пользователю {пользователь.mention}.\n"
                f"Новый баланс пользователя: `{format_number(new_receiver_balance)}` R$."
            ),
            color=disnake.Color.green()
        )
        embed_user = create_embed(
            title="💰 Вам начислены средства",
            description=(
                f"Вам было начислено `{format_number(количество)}` R$ администратором {inter.author.mention}.\n"
                f"Ваш новый баланс: `{format_number(new_receiver_balance)}` R$."
            ),
            color=disnake.Color.green()
        )
        await inter.response.send_message(embed=embed_admin)
        try:
            await пользователь.send(embed=embed_user)
        except disnake.Forbidden:
            pass


    @money.sub_command(
        name="remove",
        description="Снять деньги с пользователя (Админ)"
    )
    @commands.has_permissions(administrator=True)
    async def remove(self, inter: disnake.ApplicationCommandInteraction, пользователь: disnake.Member, количество: int):
        """Снятие средств с пользователя администратором."""
        if количество <= 0:
            embed = create_embed(
                title="❌ Некорректная сумма",
                description="Сумма снятия должна быть положительным числом.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        receiver_id = пользователь.id
        receiver_balance = await get_user_info(receiver_id, "balance") or 0

        if receiver_balance < количество:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=f"У пользователя {пользователь.mention} недостаточно средств для снятия `{format_number(количество)}` R$.\nТекущий баланс: `{format_number(receiver_balance)}` R$.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        new_receiver_balance = receiver_balance - количество
        await update_user_info(receiver_id, "balance", str(new_receiver_balance))

        embed_admin = create_embed(
            title="✅ Средства сняты",
            description=(
                f"Вы успешно сняли `{format_number(количество)}` R$ с пользователя {пользователь.mention}.\n"
                f"Новый баланс пользователя: `{format_number(new_receiver_balance)}` R$."
            ),
            color=disnake.Color.green()
        )
        embed_user = create_embed(
            title="💸 С вас сняты средства",
            description=(
                f"С вашего баланса было снято `{format_number(количество)}` R$ администратором {inter.author.mention}.\n"
                f"Ваш новый баланс: `{format_number(new_receiver_balance)}` R$."
            ),
            color=disnake.Color.red()
        )
        await inter.response.send_message(embed=embed_admin)
        try:
            await пользователь.send(embed=embed_user)
        except disnake.Forbidden:
            pass



    @commands.slash_command(
        name="work",
        description="Забрать накопленную заработную плату за рабочее время"
    )
    async def work(self, inter: disnake.ApplicationCommandInteraction):
        user_id = inter.author.id
        current_time = int(time.time())

        # Константы накопления: 5 монет/мин, лимит 6 часов (360 минут = 1800 монет)
        RATE_PER_MINUTE = 5
        MAX_HOURS = 6
        MAX_MINUTES = MAX_HOURS * 60
        MAX_INCOME = MAX_MINUTES * RATE_PER_MINUTE

        balance = await get_user_info(user_id, "balance") or 0
        last_work = await get_user_info(user_id, "last_work")

        # Если пользователь запускает команду впервые, начинаем отсчет времени
        if last_work is None:
            await update_user_info(user_id, "last_work", str(current_time))
            embed = create_embed(
                title="💼 Начало рабочей смены",
                description=(
                    f"Вы приступили к работе!\n\n"
                    f"• Скорость накопления: `+{RATE_PER_MINUTE}` R$ в минуту\n"
                    f"• Максимальное время накопления: `{MAX_HOURS}` часов (до `{format_number(MAX_INCOME)}` R$)\n\n"
                    f"Вы можете забрать накопленные средства в любой момент, вызвав `/work` снова."
                ),
                color=disnake.Color.blue()
            )
            return await inter.response.send_message(embed=embed)

        # Расчет прошедшего времени
        elapsed_seconds = current_time - int(last_work)
        elapsed_minutes = elapsed_seconds // 60

        if elapsed_minutes < 1:
            seconds_left = 60 - (elapsed_seconds % 60)
            embed = create_embed(
                title="⏳ Слишком рано",
                description=f"Вы только начали новый цикл работы. Подождите ещё `{seconds_left}` сек., чтобы накопились первые средства.",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        # Ограничиваем накопление 6 часами
        counted_minutes = min(elapsed_minutes, MAX_MINUTES)
        earned_amount = counted_minutes * RATE_PER_MINUTE

        # Обновляем баланс и сбрасываем таймер работы
        new_balance = balance + earned_amount
        await update_user_info(user_id, "balance", str(new_balance))
        await update_user_info(user_id, "last_work", str(current_time))

        # Форматирование времени работы для отчета
        hours = counted_minutes // 60
        minutes = counted_minutes % 60
        time_parts = []
        if hours > 0:
            time_parts.append(f"{hours} ч.")
        if minutes > 0 or hours == 0:
            time_parts.append(f"{minutes} мин.")
        time_worked_str = " ".join(time_parts)

        cap_notice = "\n⚠️ *Достигнут максимум накопления (6 ч.)*" if elapsed_minutes >= MAX_MINUTES else ""

        desc = (
            f"Вы успешно забрали заработанные средства!\n\n"
            f"⏱️ **Отработано:** `{time_worked_str}`{cap_notice}\n"
            f"💵 **Начислено:** `+{format_number(earned_amount)}` R$\n"
            f"💳 **Текущий баланс:** `{format_number(new_balance)}` R$"
        )

        embed = create_embed(
            title="💼 Выплата за работу",
            description=desc,
            color=disnake.Color.green(),
            footer_text="Счетчик накопления перезапущен"
        )
        await inter.response.send_message(embed=embed)




    # ==========================================
    #         РЕГИСТРАЦИЯ И АННУЛИРОВАНИЕ
    # ==========================================



    @commands.slash_command(
        name="register",
        description="Зарегистрировать РП-персонажа гражданину (Админ)",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def register(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Пользователь для регистрации"),
        fio: str = commands.Param(description="РП ФИО персонажа")
    ):
        """Прямая регистрация персонажа с начислением стартового капитала."""
        current_fio = await get_user_info(member.id, "FIO")
        if current_fio:
            embed = create_embed(
                title="⚠️ Персонаж уже зарегистрирован",
                description=f"Пользователь {member.mention} уже записан как **{current_fio}**.",
                color=disnake.Color.orange()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        clean_fio = fio.strip()

        # Запись в БД со стартовым балансом и пустыми должностями
        async with aiosqlite.connect("dbs/main.db") as db:
            await db.execute(
                """
                INSERT INTO users (user_id, balance, FIO, functions, photo, last_collection, last_work)
                VALUES (?, ?, ?, NULL, NULL, NULL, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    balance = ?,
                    FIO = ?,
                    functions = NULL,
                    photo = NULL,
                    last_collection = NULL,
                    last_work = NULL
                """,
                (member.id, self.starting_balance, clean_fio, self.starting_balance, clean_fio)
            )
            await db.commit()

        # Ответ администратору
        admin_embed = create_embed(
            title="✅ Персонаж зарегистрирован",
            description=(
                f"**Гражданин:** {member.mention} (`{member.id}`)\n"
                f"**ФИО:** `{clean_fio}`\n"
                f"**Стартовый капитал:** `{format_number(self.starting_balance)}` ₽"
            ),
            color=disnake.Color.green()
        )
        await inter.response.send_message(embed=admin_embed, ephemeral=True)

        # Уведомление игроку в ЛС
        user_embed = create_embed(
            title="🎉 Регистрация завершена",
            description=(
                f"Вы были успешно внесены в государственную базу данных!\n\n"
                f"👤 **ФИО:** `{clean_fio}`\n"
                f"💳 **Стартовый капитал:** `{format_number(self.starting_balance)}` ₽\n"
                f"Регистратор: {inter.author.mention}"
            ),
            color=disnake.Color.green()
        )
        try:
            await member.send(embed=user_embed)
        except disnake.Forbidden:
            pass

    @commands.slash_command(
        name="unregister",
        description="Аннулировать регистрацию персонажа (Админ)",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def unregister(
        self,
        inter: disnake.ApplicationCommandInteraction,
        member: disnake.Member = commands.Param(description="Пользователь для сброса регистрации"),
        reason: str = commands.Param(default="Сброс персонажа", description="Причина аннулирования")
    ):
        """Сбрасывает регистрацию пользователя: очищает FIO, роли, фото и баланс."""
        async with aiosqlite.connect("dbs/main.db") as db:
            await db.execute(
                """
                UPDATE users 
                SET FIO = '', balance = 0, functions = NULL, photo = NULL, last_collection = NULL, last_work = NULL
                WHERE user_id = ?
                """,
                (member.id,)
            )
            await db.commit()

        embed = create_embed(
            title="🗑️ Персонаж аннулирован",
            description=f"Регистрация пользователя {member.mention} была успешно сброшена.\n**Причина:** {reason}",
            color=disnake.Color.red()
        )
        await inter.response.send_message(embed=embed, ephemeral=True)

        user_embed = create_embed(
            title="⚠️ Регистрация аннулирована",
            description=f"Ваш РП-персонаж был сброшен администрацией.\n**Причина:** {reason}",
            color=disnake.Color.red()
        )
        try:
            await member.send(embed=user_embed)
        except disnake.Forbidden:
            pass



    # ==========================================
    #            ДУБЛИРОВАНИЕ КОМАНД
    # ==========================================



    @commands.command(
        name="profile",
        aliases=["профиль", "пасп", "паспорт"],
        description="Посмотреть государственный паспорт / профиль гражданина"
    )
    async def profile_second(
        self,
        ctx: commands.Context,
        member: disnake.Member = None
    ):
        """Карточка гражданина: ФИО, баланс, список должностей и доходы."""
        target = member or ctx.author
        target_id = target.id

        # 1. Получение данных пользователя из БД
        balance = await get_user_info(target_id, "balance") or 0
        fio = await get_user_info(target_id, "FIO")
        roles_raw = await get_user_info(target_id, "functions")
        photo_url = await get_user_info(target_id, "photo")
        last_collection = await get_user_info(target_id, "last_collection")
        raw_mandates = await get_user_info(target_id, "mandates")

        # 2. Формирование строки мандатов
        mandates_display = ""
        if raw_mandates and int(raw_mandates) > 0:
            mandates_display = f"📜 **Количество мандатных мест:** `{raw_mandates}`\n"

        # 3. Форматирование должностей и подсчет суммарного дохода
        user_roles = parse_roles(roles_raw)
        total_income = 0
        roles_list_str = []

        for role_key in user_roles:
            if role_key in ROLES_CONFIG:
                role_data = ROLES_CONFIG[role_key]
                role_name = role_data.get("name", role_key)
                income = role_data.get("income", 0)
                total_income += income
                roles_list_str.append(f"• **{role_name}** (`+{format_number(income)}` R$ / 3 дня)")
            else:
                roles_list_str.append(f"• **{role_key}**")

        roles_display = "\n".join(roles_list_str) if roles_list_str else "Отсутствуют"
        fio_display = fio if fio else "Не заполнено"

        # 4. Сборка описания эмбеда
        desc = (
            f"👤 **ФИО:** `{fio_display}`\n"
            f"💳 **Баланс:** `{format_number(balance)}` R$\n"
            f"📈 **Суммарная зарплата:** `{format_number(total_income)}` R$ / 3 дня\n"
            f"{mandates_display}\n"
            f"🏛️ **Государственные должности:**\n"
            f"{roles_display}"
        )

        embed = create_embed(
            title=f"📋 Личное дело: {target.display_name}",
            description=desc,
            color=disnake.Color.blue(),
            footer_text=f"ID: {target.id}"
        )

        if photo_url and photo_url.startswith("http"):
            embed.set_thumbnail(url=photo_url)
        else:
            embed.set_thumbnail(url=target.display_avatar.url)

        await ctx.send(embed=embed)

    @commands.command(
        name="collect",
        aliases=["коллект", "собрать"],
        description="Получить прибыль от своей деятельности"
    )
    async def collect_second(self, ctx: commands.Context):
        user_id = ctx.author.id
        current_time = int(time.time())

        balance = await get_user_info(user_id, "balance") or 0
        last_collection = await get_user_info(user_id, "last_collection")
        roles = await get_user_info(user_id, "functions")
        COOLDOWN_SECONDS = 3 * 24 * 60 * 60

        # Проверка кулдауна
        if last_collection:
            time_passed = current_time - int(last_collection)
            if time_passed < COOLDOWN_SECONDS:
                ready_timestamp = int(last_collection) + COOLDOWN_SECONDS
                embed = create_embed(
                    title="⏳ Сбор недоступен",
                    description=f"Вы уже собирали прибыль.\nСледующий сбор доступен: <t:{ready_timestamp}:R> (<t:{ready_timestamp}:F>).",
                    color=disnake.Color.red()
                )
                return await ctx.send(embed=embed)

        # Парсинг ролей
        user_roles = []
        if roles:
            try:
                user_roles = json.loads(roles) if roles.startswith("[") else [r.strip() for r in roles.split(",")]
            except Exception:
                user_roles = [roles]

        if not user_roles:
            embed = create_embed(
                title="❌ Нет доступной прибыли",
                description="У вас нет активных должностей для получения дохода.",
                color=disnake.Color.orange()
            )
            return await ctx.send(embed=embed)

        # Расчет начислений
        total_income = 0
        collected_details = []

        for role_key in user_roles:
            if role_key in ROLES_CONFIG:
                role_data = ROLES_CONFIG[role_key]
                income = role_data.get("income", 0)
                total_income += income
                collected_details.append(f"• **{role_data.get('name', role_key)}**: `{format_number(income)}` R$")

        if total_income == 0:
            embed = create_embed(
                title="❌ Нет доступной прибыли",
                description="Ваши текущие роли не приносят дохода.",
                color=disnake.Color.orange()
            )
            return await ctx.send(embed=embed)

        # Сохранение в БД
        new_balance = balance + total_income
        await update_user_info(user_id, "balance", str(new_balance))
        await update_user_info(user_id, "last_collection", str(current_time))

        # Ответ пользователю
        desc = (
            f"Вы успешно получили выплату за свои должности!\n\n"
            f"**Начисления:**\n" + "\n".join(collected_details) + "\n\n"
            f"**Итого начислено:** `+{format_number(total_income)}` R$\n"
            f"**Текущий баланс:** `{format_number(new_balance)}` R$"
        )

        embed = create_embed(
            title="💰 Государственная казна / Доход",
            description=desc,
            color=disnake.Color.green(),
            footer_text="Следующий сбор через 3 дня"
        )
        await ctx.send(embed=embed)

    @commands.command(
        name="work",
        aliases=["работа", "работать"],
        description="Забрать накопленную заработную плату за рабочее время"
    )
    async def work_second(self, ctx: commands.Context):
        user_id = ctx.author.id
        current_time = int(time.time())

        RATE_PER_MINUTE = 5
        MAX_HOURS = 6
        MAX_MINUTES = MAX_HOURS * 60
        MAX_INCOME = MAX_MINUTES * RATE_PER_MINUTE

        balance = await get_user_info(user_id, "balance") or 0
        last_work = await get_user_info(user_id, "last_work")

        if last_work is None:
            await update_user_info(user_id, "last_work", str(current_time))
            embed = create_embed(
                title="💼 Начало рабочей смены",
                description=(
                    f"Вы приступили к работе!\n\n"
                    f"• Скорость накопления: `+{RATE_PER_MINUTE}` R$ в минуту\n"
                    f"• Максимальное время накопления: `{MAX_HOURS}` часов (до `{format_number(MAX_INCOME)}` R$)\n\n"
                    f"Вы можете забрать накопленные средства в любой момент, вызвав команду снова."
                ),
                color=disnake.Color.blue()
            )
            return await ctx.send(embed=embed)

        elapsed_seconds = current_time - int(last_work)
        elapsed_minutes = elapsed_seconds // 60

        if elapsed_minutes < 1:
            seconds_left = 60 - (elapsed_seconds % 60)
            embed = create_embed(
                title="⏳ Слишком рано",
                description=f"Вы только начали новый цикл работы. Подождите ещё `{seconds_left}` сек., чтобы накопились первые средства.",
                color=disnake.Color.orange()
            )
            return await ctx.send(embed=embed)

        counted_minutes = min(elapsed_minutes, MAX_MINUTES)
        earned_amount = counted_minutes * RATE_PER_MINUTE

        new_balance = balance + earned_amount
        await update_user_info(user_id, "balance", str(new_balance))
        await update_user_info(user_id, "last_work", str(current_time))

        hours = counted_minutes // 60
        minutes = counted_minutes % 60
        time_parts = []
        if hours > 0:
            time_parts.append(f"{hours} ч.")
        if minutes > 0 or hours == 0:
            time_parts.append(f"{minutes} мин.")
        time_worked_str = " ".join(time_parts)

        cap_notice = "\n⚠️ *Достигнут максимум накопления (6 ч.)*" if elapsed_minutes >= MAX_MINUTES else ""

        desc = (
            f"Вы успешно забрали заработанные средства!\n\n"
            f"⏱️ **Отработано:** `{time_worked_str}`{cap_notice}\n"
            f"💵 **Начислено:** `+{format_number(earned_amount)}` R$\n"
            f"💳 **Текущий баланс:** `{format_number(new_balance)}` R$"
        )

        embed = create_embed(
            title="💼 Выплата за работу",
            description=desc,
            color=disnake.Color.green(),
            footer_text="Счетчик накопления перезапущен"
        )
        await ctx.send(embed=embed)



    @commands.command(
        name="balance",
        aliases=["bal", "баланс"],
        description="Посмотреть свой баланс или баланс другого пользователя"
    )
    async def balance_second(
        self, 
        ctx: commands.Context, 
        member: disnake.Member = None
    ):
        """Префиксная команда проверки баланса (например: !balance @user)."""
        target = member or ctx.author

        # Получаем данные из функции БД
        user_balance = await get_user_info(target.id, "balance") or 0

        # Создаем красивый эмбед через utils
        embed = create_embed(
            title=f"Баланс: {target.display_name}",
            description=f"На счету пользователя: `{format_number(user_balance)}` R$.",
            color=disnake.Color.green()
        )

        await ctx.send(embed=embed)






def setup(bot: commands.Bot):
    bot.add_cog(EconomyCog(bot))