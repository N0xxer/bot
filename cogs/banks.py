import os
import time
from typing import Optional
import disnake
from disnake.ext import commands, tasks
import aiosqlite

from functions.db_helpers import (
    DB_PATH,
    get_loan_by_id,
    get_user_loans,
    init_bank_db,
    generate_unique_account_number,
    get_all_banks,
    get_bank,
    get_bank_by_name,
    get_account_by_number,
    get_user_accounts,
    get_bank_accounts,
    get_bank_total_issued_loans,
    get_user_info,
    update_user_info,
    is_user_registered
)
from functions.utils import ensure_admin, ensure_user_registered, notification_send, create_embed, format_number, MAIN_COLOR


class BanksCog(commands.Cog):
    """Банковская система Резендии."""
    
    # ID категории для создания каналов банков
    BANK_CATEGORY_ID = 1548395973338996836

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_overdue_loans.start()

    async def cog_load(self):
        await init_bank_db()

    def cog_unload(self):
        self.check_overdue_loans.cancel()

    @tasks.loop(minutes=30)
    async def check_overdue_loans(self):
        """Периодическая проверка просроченных кредитов и начисление пени."""
        now = int(time.time())
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            # Ищем активные непогашенные кредиты, у которых вышел срок
            async with db.execute("""
                SELECT l.*, b.name as bank_name, b.log_channel_id 
                FROM bank_loans l
                JOIN banks b ON l.bank_id = b.id
                WHERE l.is_closed = 0 AND l.next_payment_time < ?
            """, (now,)) as cursor:
                overdue_loans = await cursor.fetchall()

            for loan in overdue_loans:
                # Начисляем пеню 5% от текущего остатка долга
                penalty = max(1, int(loan["remaining_debt"] * 0.05))
                new_remaining = loan["remaining_debt"] + penalty
                new_total = loan["total_debt"] + penalty
                new_next_due = loan["next_payment_time"] + 86400

                # Обновляем сумму долга и сдвигаем проверку на 24 часа вперед
                await db.execute("""
                    UPDATE bank_loans 
                    SET remaining_debt = ?, total_debt = ?, next_payment_time = ?
                    WHERE id = ?
                """, (new_remaining, new_total, new_next_due, loan["id"]))

                # Логируем операцию в лог-канал банка
                await self.bank_money_logger(
                    bank_id=loan["bank_id"],
                    op_type="credit",
                    user1_id=loan["user_id"],
                    account1=loan["account_number"],
                    amount=penalty,
                    extra=f"⚠️ ПРОСРОЧКА #{loan['id']}. Начислена пеня 5% (+{format_number(penalty)} R$). Долг: {format_number(new_remaining)} R$"
                )

                # Отправляем предупреждение должнику в ЛС
                user = self.bot.get_user(loan["user_id"])
                if user:
                    embed = create_embed(
                        title="⚠️ Просрочка по кредиту",
                        description=(
                            f"Срок оплаты по кредиту **#{loan['id']}** в банке «{loan['bank_name']}» истек.\n\n"
                            f"• Начислена пеня (5%): `+{format_number(penalty)}` R$\n"
                            f"• Общий остаток к выплате: `{format_number(new_remaining)}` R$\n"
                            f"• Следующее начисление пени: <t:{new_next_due}:R>\n\n"
                            f"Погасите задолженность командой `/credit pay`."
                        ),
                        color=disnake.Color.orange()
                    )
                    try:
                        await user.send(embed=embed)
                    except disnake.Forbidden:
                        pass

            await db.commit()

    @check_overdue_loans.before_loop
    async def before_check_overdue_loans(self):
        await self.bot.wait_until_ready()


    # =========================================================
    #                   ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
    # =========================================================



    async def validate_account(
        self,
        inter: disnake.ApplicationCommandInteraction,
        account_number: str,
        check_owner: bool = True,
        check_frozen: bool = True
    ):
        """Единая валидация счета: проверка существования, владельца и статуса заморозки."""
        account = await get_account_by_number(account_number.strip())
        if not account:
            embed = create_embed(
                title="❌ Счет не найден",
                description=f"Лицевой счет `{account_number}` не существует в банковской системе.",
                color=disnake.Color.red()
            )
            await inter.edit_original_message(embed=embed)
            return None

        if check_owner and account["user_id"] != inter.author.id:
            embed = create_embed(
                title="⛔ Отказано в доступе",
                description=f"Лицевой счет `{account_number}` вам не принадлежит.",
                color=disnake.Color.red()
            )
            await inter.edit_original_message(embed=embed)
            return None

        if check_frozen and account["is_frozen"]:
            embed = create_embed(
                title="🔒 Счет заморожен",
                description=f"Операции со счетом `{account_number}` приостановлены.",
                color=disnake.Color.red()
            )
            await inter.edit_original_message(embed=embed)
            return None

        return account

    async def bank_money_logger(
        self,
        bank_id: int,
        op_type: str,
        user1_id: int,
        account1: str,
        amount: int = 0,
        user2_id: Optional[int] = None,
        account2: Optional[str] = None,
        extra: str = ""
    ):
        """Единое логирование банковских операций в лог-канал банка."""
        bank = await get_bank(bank_id)
        if not bank or not bank["log_channel_id"]:
            return

        log_channel = self.bot.get_channel(bank["log_channel_id"])
        if not log_channel:
            return

        types_map = {
            "deposit": ("📥 Пополнение счета", disnake.Color.green()),
            "withdraw": ("📤 Снятие средств", disnake.Color.orange()),
            "transfer": ("💸 Перевод между счетами", disnake.Color.blue()),
            "freeze": ("🔒 Заморозка счета", disnake.Color.red()),
            "unfreeze": ("🟢 Разморозка счета", disnake.Color.green()),
            "open": ("💳 Открытие счета", disnake.Color.teal()),
            "close": ("🗑️ Закрытие счета", disnake.Color.dark_gray()),
            "credit": ("🏦 Выдача кредита", disnake.Color.purple())
        }
        title, color = types_map.get(op_type, ("Банковская операция", disnake.Color.light_gray()))

        lines = [f"**Клиент:** <@{user1_id}> (`{user1_id}`)"]
        if account1:
            lines.append(f"**Лицевой счет:** `{account1}`")
        if amount > 0:
            lines.append(f"**Сумма:** `{format_number(amount)}` R$")
        if account2:
            u2_text = f" (<@{user2_id}>)" if user2_id else ""
            lines.append(f"**Счет назначения:** `{account2}`{u2_text}")
        if extra:
            lines.append(f"**Детали:** {extra}")

        embed = create_embed(title=title, description="\n".join(lines), color=color)
        try:
            await log_channel.send(embed=embed)
        except disnake.Forbidden:
            pass

    async def build_bank_control_components(self, bank_id: int):
        bank = await get_bank(bank_id)
        if not bank:
            return []

        owner_text = f"<@{bank['owner_id']}>" if bank["owner_id"] else "Федеральное управление"
        
        # Считаем сумму активных кредитов банка
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT SUM(remaining_debt) FROM bank_loans WHERE bank_id = ? AND is_closed = 0",
                (bank_id,)
            ) as cursor:
                active_loans_row = await cursor.fetchone()
                active_loans_total = active_loans_row[0] or 0

        content = (
            f"### 🏛️ Панель управления банком «{bank['name']}»\n\n"
            f"• **ID банка:** `{bank['id']}`\n"
            f"• **Тип:** `{bank['type']}`\n"
            f"• **Владелец:** {owner_text}\n"
            f"• **Казна (Резерв):** `{format_number(bank['balance'])}` R$\n"
            f"• **Кредитная ставка:** `{bank['interest_rate']}%`\n"
            f"• **Активные выданные займы:** `{format_number(active_loans_total)}` R$\n"
        )

        return [
            disnake.ui.Container(disnake.ui.TextDisplay(content=content)),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Изменить данные",
                    emoji="⚙️",
                    style=disnake.ButtonStyle.secondary,
                    custom_id=f"bank_ctrl:edit:{bank_id}"
                ),
                disnake.ui.Button(
                    label="Счета клиентов",
                    emoji="🔒",
                    style=disnake.ButtonStyle.secondary,
                    custom_id=f"bank_ctrl:freeze:{bank_id}"
                ),
                disnake.ui.Button(
                    label="Казна банка",
                    emoji="💰",
                    style=disnake.ButtonStyle.success,
                    custom_id=f"bank_ctrl:balance:{bank_id}"
                ),
                disnake.ui.Button(
                    label="Обновить",
                    emoji="🔄",
                    style=disnake.ButtonStyle.primary,
                    custom_id=f"bank_ctrl:refresh:{bank_id}"
                )
            )
        ]

    async def user_accounts_autocomp(self, inter: disnake.ApplicationCommandInteraction, user_input: str):
        accounts = await get_user_accounts(inter.author.id)
        return {
            f"{acc['bank_name']} | {acc['account_number']} ({format_number(acc['balance'])} R$)": acc['account_number']
            for acc in accounts if user_input.lower() in acc['account_number'].lower() or user_input.lower() in acc['bank_name'].lower()
        }


    async def bank_autocomplete(self, inter: disnake.ApplicationCommandInteraction, user_input: str):
        banks = await get_all_banks()
        return {
            f"{b['name']} (Резерв: {format_number(b['balance'])} R$)": str(b['id'])
            for b in banks if user_input.lower() in b["name"].lower() or user_input in str(b["id"])
        }



    # =========================================================
    #                   СЛЭШ-КОМАНДЫ БАНКА
    # =========================================================



    @commands.slash_command(name="bank", description="Банковская система Резендии")
    async def bank_group(self, inter: disnake.ApplicationCommandInteraction):
        pass


    @bank_group.sub_command(
        name="create",
        description="Зарегистрировать новый банк (Администрация)"
    )
    @commands.has_permissions(administrator=True)
    async def bank_create(
        self,
        inter: disnake.ApplicationCommandInteraction,
        название: str = commands.Param(description="Название банка"),
        тип: str = commands.Param(
            default="коммерческий",
            choices=["коммерческий", "государственный"],
            description="Тип банка"
        ),
        владелец: Optional[disnake.Member] = commands.Param(
            default=None,
            description="Владелец банка (по умолчанию: Федеральное управление)"
        ),
        создавать_форум: bool = commands.Param(
            default=True,
            description="Создавать ли публичный форум банка для работы с клиентами?"
        )
    ):
        await inter.response.defer(ephemeral=True)

        if владелец and not await is_user_registered(владелец.id):
            return await inter.edit_original_message(content=f"❌ Пользователь {владелец.mention} не зарегистрирован в базе.")

        category = inter.guild.get_channel(self.BANK_CATEGORY_ID)
        if not isinstance(category, disnake.CategoryChannel):
            return await inter.edit_original_message(content="❌ Категория для создания каналов банка не найдена.")

        owner_id = владелец.id if владелец else None

        # Права для служебных каналов (управление и логи)
        admin_overwrites = {
            inter.guild.default_role: disnake.PermissionOverwrite(view_channel=False),
            inter.guild.me: disnake.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }
        if владелец:
            admin_overwrites[владелец] = disnake.PermissionOverwrite(view_channel=True, send_messages=False)

        clean_name = название.lower().replace(" ", "-")[:25]

        control_channel = await category.create_text_channel(
            name=f"банк-{clean_name}",
            overwrites=admin_overwrites,
            topic=f"Панель управления банком {название}"
        )
        log_channel = await category.create_text_channel(
            name=f"логи-{clean_name}",
            overwrites=admin_overwrites,
            topic=f"Логи операций банка {название}"
        )

        # Создание публичного форума банка по аналогии с компаниями
        forum_channel = None
        if создавать_форум:
            forum_overwrites = {
                inter.guild.default_role: disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    create_public_threads=False,
                    create_private_threads=False,
                    send_messages_in_threads=False,
                    send_messages=False
                ),
                inter.guild.me: disnake.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_threads=True
                )
            }
            if владелец:
                forum_overwrites[владелец] = disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    create_public_threads=True,
                    send_messages_in_threads=True,
                    send_messages=True
                )

            owner_label = владелец.display_name if владелец else "Федеральное управление"
            try:
                forum_channel = await category.create_forum_channel(
                    name=f"❮🏛️❯・{название}",
                    overwrites=forum_overwrites,
                    topic=f"Филиал банка «{название}». Руководство: {owner_label} | Тип: {тип}"
                )
            except Exception as e:
                return await inter.edit_original_message(content=f"❌ Не удалось создать форум банка: `{e}`")

        try:
            bottom_pos = max((c.position for c in category.channels), default=0) + 1
            await control_channel.edit(position=bottom_pos)
            await log_channel.edit(position=bottom_pos + 1)
            if forum_channel:
                await forum_channel.edit(position=bottom_pos + 2)
        except Exception:
            pass

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO banks (name, type, owner_id, balance, interest_rate, control_channel_id, log_channel_id)
                VALUES (?, ?, ?, 1000000, 5.0, ?, ?)
            """, (название, тип, owner_id, control_channel.id, log_channel.id))
            bank_id = cursor.lastrowid
            await db.commit()

        components = await self.build_bank_control_components(bank_id)
        control_msg = await control_channel.send(components=components)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE banks SET control_message_id = ? WHERE id = ?", (control_msg.id, bank_id))
            await db.commit()

        owner_str = владелец.mention if владелец else "Федеральное управление"
        forum_info = f"\nФорум банка: {forum_channel.mention}" if forum_channel else ""

        await notification_send(
            inter,
            title="🏛️ Зарегистрирован новый банк",
            description=f"Банк **«{название}»** ({тип}) успешно создан.\nВладелец: {owner_str}\nУправление: {control_channel.mention}{forum_info}",
            color=disnake.Color.green()
        )

        await inter.edit_original_message(
            content=f"✅ Банк **«{название}»** успешно создан!\n• Панель: {control_channel.mention}\n• Логи: {log_channel.mention}" + (f"\n• Форум: {forum_channel.mention}" if forum_channel else "")
        )



    @bank_group.sub_command(
        name="dissolution",
        description="Полный роспуск банка (Администрация)"
    )
    @commands.has_permissions(administrator=True)
    async def bank_dissolution(
        self,
        inter: disnake.ApplicationCommandInteraction,
        название: str = commands.Param(description="Название банка для роспуска")
    ):
        await inter.response.defer(ephemeral=True)
        bank = await get_bank_by_name(название)
        if not bank:
            return await inter.edit_original_message(content=f"❌ Банк «{название}» не найден.")

        bank_id = bank["id"]

        accounts = await get_bank_accounts(bank_id)
        async with aiosqlite.connect(DB_PATH) as db:
            for acc in accounts:
                if acc["balance"] > 0:
                    await db.execute(
                        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                        (acc["balance"], acc["user_id"])
                    )
            if bank["owner_id"] and bank["balance"] > 0:
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (bank["balance"], bank["owner_id"])
                )

            await db.execute("DELETE FROM bank_accounts WHERE bank_id = ?", (bank_id,))
            await db.execute("DELETE FROM bank_loans WHERE bank_id = ?", (bank_id,))
            await db.execute("DELETE FROM banks WHERE id = ?", (bank_id,))
            await db.commit()

        for ch_key in ("control_channel_id", "log_channel_id"):
            ch_id = bank[ch_key]
            if ch_id:
                channel = inter.guild.get_channel(ch_id)
                if channel:
                    try:
                        await channel.delete(reason=f"Роспуск банка {название}")
                    except Exception:
                        pass

        await notification_send(
            inter,
            title="💥 Банк распущен",
            description=f"Банк **«{название}»** был полностью закрыт. Средства с лицевых счетов выплачены гражданам на руки.",
            color=disnake.Color.red()
        )
        await inter.edit_original_message(content=f"✅ Банк **«{название}»** успешно распущен.")


    @bank_group.sub_command(name="accounts", description="Посмотреть банковские счета")
    async def bank_accounts_view(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пользователь: Optional[disnake.Member] = commands.Param(default=None, description="Чьи счета посмотреть")
    ):
        target = пользователь or inter.author
        if not await ensure_user_registered(inter, target.id):
            return

        await inter.response.defer(ephemeral=True)
        accounts = await get_user_accounts(target.id)

        lines = [f"## Личные счета пользователя {target.mention}\n"]
        if not accounts:
            lines.append("```У пользователя нет открытых лицевых счетов.```")
        else:
            for acc in accounts:
                frozen_str = "да" if acc["is_frozen"] else "нет"
                block = (
                    f"```Счет в банке {acc['bank_name']}\n"
                    f"Номер: {acc['account_number']}\n"
                    f"Баланс: {format_number(acc['balance'])} R$\n"
                    f"Заморожен: {frozen_str}```"
                )
                lines.append(block)

        await inter.edit_original_message(content="\n".join(lines))


    @bank_group.sub_command(name="deposit", description="Внести наличные на лицевой счет")
    async def bank_deposit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        счет: str = commands.Param(description="Номер вашего счета", autocomplete=user_accounts_autocomp),
        сумма: int = commands.Param(description="Сумма для внесения", ge=1)
    ):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)
        account = await self.validate_account(inter, счет, check_owner=True, check_frozen=True)
        if not account:
            return

        user_cash = await get_user_info(inter.author.id, "balance") or 0
        if user_cash < сумма:
            return await inter.edit_original_message(
                content=f"❌ Недостаточно наличных! (В наличии: `{format_number(user_cash)}` R$)."
            )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (сумма, inter.author.id))
            await db.execute("UPDATE bank_accounts SET balance = balance + ? WHERE account_number = ?", (сумма, счет))
            await db.commit()

        await self.bank_money_logger(
            bank_id=account["bank_id"],
            op_type="deposit",
            user1_id=inter.author.id,
            account1=счет,
            amount=сумма
        )

        embed = create_embed(
            title="📥 Пополнение выполнено",
            description=f"На счет `{счет}` успешно внесено `{format_number(сумма)}` R$.",
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)


    @bank_group.sub_command(name="withdraw", description="Снять наличные с лицевого счета")
    async def bank_withdraw(
        self,
        inter: disnake.ApplicationCommandInteraction,
        счет: str = commands.Param(description="Номер вашего счета", autocomplete=user_accounts_autocomp),
        сумма: int = commands.Param(description="Сумма для снятия", ge=1)
    ):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)
        account = await self.validate_account(inter, счет, check_owner=True, check_frozen=True)
        if not account:
            return

        if account["balance"] < сумма:
            return await inter.edit_original_message(
                content=f"❌ Недостаточно средств на счете! (Баланс: `{format_number(account['balance'])}` R$)."
            )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE bank_accounts SET balance = balance - ? WHERE account_number = ?", (сумма, счет))
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (сумма, inter.author.id))
            await db.commit()

        await self.bank_money_logger(
            bank_id=account["bank_id"],
            op_type="withdraw",
            user1_id=inter.author.id,
            account1=счет,
            amount=сумма
        )

        embed = create_embed(
            title="📤 Снятие выполнено",
            description=f"Со счета `{счет}` успешно снято `{format_number(сумма)}` R$.",
            color=disnake.Color.orange()
        )
        await inter.edit_original_message(embed=embed)


    @bank_group.sub_command(name="transfer", description="Перевод средств между лицевыми счетами")
    async def bank_transfer(
        self,
        inter: disnake.ApplicationCommandInteraction,
        счет_отправки: str = commands.Param(description="Счет списания", autocomplete=user_accounts_autocomp),
        счет_принятия: str = commands.Param(description="Счет получателя"),
        сумма: int = commands.Param(description="Сумма перевода", ge=1)
    ):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)

        from_acc = await self.validate_account(inter, счет_отправки, check_owner=True, check_frozen=True)
        if not from_acc:
            return

        to_acc = await self.validate_account(inter, счет_принятия, check_owner=False, check_frozen=True)
        if not to_acc:
            return

        if from_acc["account_number"] == to_acc["account_number"]:
            return await inter.edit_original_message(content="❌ Нельзя перевести средства на тот же самый счет.")

        if from_acc["balance"] < сумма:
            return await inter.edit_original_message(
                content=f"❌ Недостаточно средств на счете списания! (Баланс: `{format_number(from_acc['balance'])}` R$)."
            )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE bank_accounts SET balance = balance - ? WHERE account_number = ?", (сумма, from_acc["account_number"]))
            await db.execute("UPDATE bank_accounts SET balance = balance + ? WHERE account_number = ?", (сумма, to_acc["account_number"]))
            await db.commit()

        await self.bank_money_logger(
            bank_id=from_acc["bank_id"],
            op_type="transfer",
            user1_id=inter.author.id,
            account1=from_acc["account_number"],
            amount=сумма,
            user2_id=to_acc["user_id"],
            account2=to_acc["account_number"]
        )
        if from_acc["bank_id"] != to_acc["bank_id"]:
            await self.bank_money_logger(
                bank_id=to_acc["bank_id"],
                op_type="transfer",
                user1_id=inter.author.id,
                account1=from_acc["account_number"],
                amount=сумма,
                user2_id=to_acc["user_id"],
                account2=to_acc["account_number"]
            )

        embed = create_embed(
            title="💸 Перевод выполнен",
            description=(
                f"Списано со счета `{from_acc['account_number']}`: `{format_number(сумма)}` R$\n"
                f"Зачислено на счет `{to_acc['account_number']}`."
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)



    @commands.slash_command(
        name="send_bank_services_panel",
        description="Отправить панель услуг банка (Администрация)",
        default_member_permissions=disnake.Permissions(administrator=True)
    )
    async def send_services_panel(self, inter: disnake.ApplicationCommandInteraction):
        await inter.response.defer(ephemeral=True)

        image_path = os.path.join("images", "banki.png")
        if not os.path.exists(image_path):
            return await inter.edit_original_message(
                content=f"❌ Файл `{image_path}` не найден."
            )

        file = disnake.File(image_path, filename="banki.png")

        content = (
            "### 🏛️ Банковские услуги Резендии\n\n"
            "Здесь вы можете открыть новый лицевой счет или оформить кредит в банках государства.\n\n"
            "• **Лицевой счет** позволяет безопасно хранить средства и совершать переводы.\n"
            "• **Кредитование** выдается на срок от 1 до 14 дней с фиксированной ставкой."
        )

        components = [
            disnake.ui.Container(
                disnake.ui.TextDisplay(content=content),
                disnake.ui.MediaGallery(disnake.MediaGalleryItem(media="attachment://banki.png")),
                accent_color=MAIN_COLOR
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Открыть лицевой счет",
                    style=disnake.ButtonStyle.success,
                    emoji="💳",
                    custom_id="service_btn:open_account"
                ),
                disnake.ui.Button(
                    label="Взять кредит",
                    style=disnake.ButtonStyle.primary,
                    emoji="💰",
                    custom_id="service_btn:take_loan"
                )
            )
        ]

        await inter.channel.send(file=file, components=components)
        await inter.edit_original_message(content="✅ Панель банковских услуг отправлена.")



    @bank_group.sub_command_group(name="money", description="Административное управление казной банков")
    async def bank_money_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    # Выдача денег банку (Админ)
    @bank_money_group.sub_command(
        name="add",
        description="Выдать деньги в казну банка (Администрация)"
    )
    async def bank_money_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        банк: str = commands.Param(description="Выберите банк", autocomplete=bank_autocomplete),
        количество: int = commands.Param(description="Сумма пополнения", min_value=1)
    ):
        if not await ensure_admin(inter):
            return

        await inter.response.defer(ephemeral=True)
        bank_id = int(банк)
        bank = await get_bank(bank_id)
        if not bank:
            return await inter.edit_original_message(content="❌ Указанный банк не найден.")

        new_balance = bank["balance"] + количество

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE banks SET balance = ? WHERE id = ?", (new_balance, bank_id))
            await db.commit()

        await self.bank_money_logger(
            bank_id=bank_id,
            op_type="deposit",
            user1_id=inter.author.id,
            amount=количество,
            extra=f"Администратор {inter.author.display_name} эмитировал средства в резерв банка."
        )

        # Обновляем панель управления банком
        components = await self.build_bank_control_components(bank_id)
        channel = self.bot.get_channel(bank["control_channel_id"])
        if channel and bank["control_message_id"]:
            try:
                msg = await channel.fetch_message(bank["control_message_id"])
                await msg.edit(components=components)
            except Exception:
                pass

        embed = create_embed(
            title="🏛️ Казна банка пополнена",
            description=(
                f"**Банк:** «{bank['name']}» (ID: `{bank_id}`)\n"
                f"**Сумма начисления:** `+{format_number(количество)}` R$\n"
                f"**Новый резерв банка:** `{format_number(new_balance)}` R$\n"
                f"**Администратор:** {inter.author.mention}"
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)

    # Снятие денег с банка (Админ)
    @bank_money_group.sub_command(
        name="remove",
        description="Снять деньги из казны банка (Администрация)"
    )
    async def bank_money_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        банк: str = commands.Param(description="Выберите банк", autocomplete=bank_autocomplete),
        количество: int = commands.Param(description="Сумма списания", min_value=1)
    ):
        if not await ensure_admin(inter):
            return

        await inter.response.defer(ephemeral=True)
        bank_id = int(банк)
        bank = await get_bank(bank_id)
        if not bank:
            return await inter.edit_original_message(content="❌ Указанный банк не найден.")

        new_balance = bank["balance"] - количество

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE banks SET balance = ? WHERE id = ?", (new_balance, bank_id))
            await db.commit()

        await self.bank_money_logger(
            bank_id=bank_id,
            op_type="withdraw",
            user1_id=inter.author.id,
            amount=количество,
            extra=f"Администратор {inter.author.display_name} списал средства из резерва банка."
        )

        # Обновляем панель управления банком
        components = await self.build_bank_control_components(bank_id)
        channel = self.bot.get_channel(bank["control_channel_id"])
        if channel and bank["control_message_id"]:
            try:
                msg = await channel.fetch_message(bank["control_message_id"])
                await msg.edit(components=components)
            except Exception:
                pass

        embed = create_embed(
            title="🏛️ Списание из казны банка",
            description=(
                f"**Банк:** «{bank['name']}» (ID: `{bank_id}`)\n"
                f"**Сумма списания:** `-{format_number(количество)}` R$\n"
                f"**Остаток в казне:** `{format_number(new_balance)}` R$\n"
                f"**Администратор:** {inter.author.mention}"
            ),
            color=disnake.Color.red()
        )
        await inter.edit_original_message(embed=embed)



    # =========================================================
    #                   КНОПКИ ПАНЕЛИ УСЛУГ
    # =========================================================



    @commands.Cog.listener("on_button_click")
    async def handle_service_panel_clicks(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id
        if not custom_id.startswith("service_btn:"):
            return

        if not await is_user_registered(inter.author.id):
            return await inter.response.send_message("❌ Для использования услуг банка требуется регистрация.", ephemeral=True)

        action = custom_id.split(":")[1]
        banks = await get_all_banks()
        if not banks:
            return await inter.response.send_message("❌ В государстве еще нет действующих банков.", ephemeral=True)

        bank_options = [
            disnake.SelectOption(
                label=b["name"][:100],
                value=str(b["id"]),
                description=f"Ставка: {b['interest_rate']}% | Баланс: {format_number(b['balance'])} R$"
            ) for b in banks[:25]
        ]

        if action == "open_account":
            modal_components = [
                disnake.ui.Label(
                    text="Выберите банк",
                    component=disnake.ui.StringSelect(
                        custom_id="open_bank_id",
                        placeholder="Выберите банк для открытия счета",
                        options=bank_options,
                        min_values=1,
                        max_values=1
                    )
                )
            ]
            await inter.response.send_modal(
                title="Открытие лицевого счета",
                custom_id="modal:bank_service:open_account",
                components=modal_components
            )

        elif action == "take_loan":
            modal_components = [
                disnake.ui.Label(
                    text="Выберите банк",
                    component=disnake.ui.StringSelect(
                        custom_id="loan_bank_id",
                        placeholder="Банк-кредитор",
                        options=bank_options,
                        min_values=1,
                        max_values=1
                    )
                ),
                disnake.ui.Label(
                    text="Сумма кредита (100 - 1 000 000 R$)",
                    component=disnake.ui.TextInput(
                        custom_id="loan_amount",
                        placeholder="Например: 50000",
                        style=disnake.TextInputStyle.short,
                        min_length=3,
                        max_length=7,
                        required=True
                    )
                ),
                disnake.ui.Label(
                    text="Срок кредита в днях (от 1 до 14)",
                    component=disnake.ui.TextInput(
                        custom_id="loan_days",
                        placeholder="Например: 7",
                        style=disnake.TextInputStyle.short,
                        min_length=1,
                        max_length=2,
                        required=True
                    )
                )
            ]
            await inter.response.send_modal(
                title="Оформление кредита",
                custom_id="modal:bank_service:take_loan",
                components=modal_components
            )

    # =========================================================
    #            ОБРАБОТКА МОДАЛОК ПАНЕЛИ УСЛУГ
    # =========================================================

    @commands.Cog.listener("on_modal_submit")
    async def handle_service_modals(self, inter: disnake.ModalInteraction):
        custom_id = inter.custom_id
        if not custom_id.startswith("modal:bank_service:"):
            return

        action = custom_id.split(":")[2]

        if action == "open_account":
            bank_id = int(inter.values.get("open_bank_id")[0])
            bank = await get_bank(bank_id)

            embed = create_embed(
                title="Подтверждение открытия счета",
                description=f"Вы собираетесь открыть лицевой счет в банке **«{bank['name']}»**.\nПодтвердить операцию?",
                color=disnake.Color.blue()
            )
            view = disnake.ui.View(timeout=60)
            view.add_item(disnake.ui.Button(label="Подтвердить", style=disnake.ButtonStyle.success, custom_id=f"confirm_acc_open:{bank_id}"))
            view.add_item(disnake.ui.Button(label="Отмена", style=disnake.ButtonStyle.secondary, custom_id="confirm_acc_cancel"))

            await inter.response.send_message(embed=embed, view=view, ephemeral=True, delete_after=30)

        elif action == "take_loan":
            bank_id = int(inter.values.get("loan_bank_id")[0])
            raw_amount = inter.text_values.get("loan_amount", "").strip()
            raw_days = inter.text_values.get("loan_days", "").strip()

            if not raw_amount.isdigit() or not raw_days.isdigit():
                return await inter.response.send_message("❌ Сумма и срок должны быть числами.", ephemeral=True, delete_after=10)

            amount = int(raw_amount)
            days = int(raw_days)

            if not (100 <= amount <= 1000000):
                return await inter.response.send_message("❌ Сумма кредита должна быть от 100 до 1 000 000 R$.", ephemeral=True, delete_after=10)

            if not (1 <= days <= 14):
                return await inter.response.send_message("❌ Срок кредита должен быть от 1 до 14 дней.", ephemeral=True, delete_after=10)

            user_accs = await get_user_accounts(inter.author.id)
            target_acc = next((a for a in user_accs if a["bank_id"] == bank_id and not a["is_frozen"]), None)
            if not target_acc:
                return await inter.response.send_message(
                    "❌ У вас нет активного счета в этом банке для зачисления кредита. Сначала откройте счет.",
                    ephemeral=True,
                    delete_after=10
                )

            bank = await get_bank(bank_id)
            total_debt = int(amount * (1 + (bank["interest_rate"] / 100)))

            embed = create_embed(
                title="Подтверждение кредита",
                description=(
                    f"🏦 **Банк:** {bank['name']}\n"
                    f"💵 **Тело кредита:** `{format_number(amount)}` R$\n"
                    f"📈 **Итого к возврату:** `{format_number(total_debt)}` R$ ({bank['interest_rate']}%)\n"
                    f"⏳ **Срок:** `{days}` дн.\n"
                    f"💳 **Счет зачисления:** `{target_acc['account_number']}`"
                ),
                color=disnake.Color.purple()
            )
            view = disnake.ui.View(timeout=60)
            view.add_item(disnake.ui.Button(label="Подтвердить", style=disnake.ButtonStyle.success, custom_id=f"confirm_loan:{bank_id}:{amount}:{days}"))
            view.add_item(disnake.ui.Button(label="Отмена", style=disnake.ButtonStyle.secondary, custom_id="confirm_acc_cancel"))

            await inter.response.send_message(embed=embed, view=view, ephemeral=True, delete_after=30)



    # =========================================================
    #        ОБРАБОТКА ПОДТВЕРЖДЕНИЙ (CONFIRM / CANCEL)
    # =========================================================



    @commands.Cog.listener("on_button_click")
    async def handle_confirmations(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id

        if custom_id == "confirm_acc_cancel":
            return await inter.response.edit_message(content="❌ Операция отменена.", embed=None, view=None)

        if custom_id.startswith("confirm_acc_open:"):
            bank_id = int(custom_id.split(":")[1])
            bank = await get_bank(bank_id)
            acc_num = await generate_unique_account_number(bank_id)
            now = int(time.time())

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO bank_accounts (account_number, user_id, bank_id, balance, is_frozen, created_at)
                    VALUES (?, ?, ?, 0, 0, ?)
                """, (acc_num, inter.author.id, bank_id, now))
                await db.commit()

            await self.bank_money_logger(
                bank_id=bank_id,
                op_type="open",
                user1_id=inter.author.id,
                account1=acc_num
            )

            await inter.response.edit_message(
                content=f"✅ Вам успешно открыт счет `{acc_num}` в банке **«{bank['name']}»**!",
                embed=None,
                view=None
            )

        elif custom_id.startswith("confirm_loan:"):
            _, _, bank_id_str, amount_str, days_str = custom_id.split(":")
            bank_id = int(bank_id_str)
            amount = int(amount_str)
            days = int(days_str)

            user_accs = await get_user_accounts(inter.author.id)
            target_acc = next((a for a in user_accs if a["bank_id"] == bank_id and not a["is_frozen"]), None)
            if not target_acc:
                return await inter.response.edit_message(content="❌ Активный счет не найден.", embed=None, view=None)

            bank = await get_bank(bank_id)
            total_debt = int(amount * (1 + (bank["interest_rate"] / 100)))
            now = int(time.time())
            next_due = now + (days * 86400)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE bank_accounts SET balance = balance + ? WHERE account_number = ?", (amount, target_acc["account_number"]))
                await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (amount, bank_id))
                await db.execute("""
                    INSERT INTO bank_loans (
                        user_id, bank_id, account_number, original_amount,
                        total_debt, remaining_debt, period_payment, created_at, next_payment_time, is_closed
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """, (inter.author.id, bank_id, target_acc["account_number"], amount, total_debt, total_debt, total_debt, now, next_due))
                await db.commit()

            await self.bank_money_logger(
                bank_id=bank_id,
                op_type="credit",
                user1_id=inter.author.id,
                account1=target_acc["account_number"],
                amount=amount,
                extra=f"Долг: {format_number(total_debt)} R$ на {days} дн. (до <t:{next_due}:D>)"
            )

            await inter.response.edit_message(
                content=f"✅ Кредит на сумму `{format_number(amount)}` R$ выдан на счет `{target_acc['account_number']}`!",
                embed=None,
                view=None
            )





    # =========================================================
    #            ПАНЕЛЬ УПРАВЛЕНИЯ БАНКОМ (ДЛЯ ВЛАДЕЛЬЦА)
    # =========================================================



    @commands.Cog.listener("on_button_click")
    async def handle_bank_control_buttons(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id

        # Обрабатываем только кнопки панели управления банком
        if not custom_id.startswith("bank_ctrl:"):
            return

        parts = custom_id.split(":")
        action = parts[1]
        bank_id = int(parts[2])

        bank = await get_bank(bank_id)
        if not bank:
            return await inter.response.send_message("❌ Данные банка не найдены в базе.", ephemeral=True)

        is_owner = (bank["owner_id"] == inter.author.id)
        is_admin = inter.author.guild_permissions.administrator

        # Доступ к управлению имеют только владелец банка или администраторы
        if not (is_owner or is_admin):
            return await inter.response.send_message("⛔ Только владелец банка или администратор может использовать эту панель.", ephemeral=True)

        # 1. Изменение данных банка (Название, Владелец, Ставка)
        if action == "edit":
            modal = disnake.ui.Modal(
                title=f"Настройки: {bank['name'][:30]}",
                custom_id=f"modal:bank_edit:{bank_id}",
                components=[
                    disnake.ui.TextInput(
                        label="Новое название банка",
                        placeholder=bank["name"],
                        custom_id="new_name",
                        style=disnake.TextInputStyle.short,
                        required=False,
                        max_length=100
                    ),
                    disnake.ui.TextInput(
                        label="Процентная ставка (%)",
                        placeholder=str(bank["interest_rate"]),
                        custom_id="new_rate",
                        style=disnake.TextInputStyle.short,
                        required=False,
                        max_length=5
                    )
                ]
            )
            await inter.response.send_modal(modal=modal)

        # 2. Управление казной (Внесение / Снятие денег владельцем)
        elif action == "balance":
            modal = disnake.ui.Modal(
                title=f"Казна: {bank['name'][:35]}",
                custom_id=f"bank_modal:balance:{bank_id}",
                components=[
                    disnake.ui.TextInput(
                        label="Действие (+ пополнить / - снять)",
                        placeholder="Напишите: пополнить ИЛИ снять",
                        custom_id="action_type",
                        style=disnake.TextInputStyle.short,
                        max_length=15,
                        required=True
                    ),
                    disnake.ui.TextInput(
                        label="Сумма (R$)",
                        placeholder="Например: 50000",
                        custom_id="amount",
                        style=disnake.TextInputStyle.short,
                        required=True
                    )
                ]
            )
            await inter.response.send_modal(modal=modal)

        # 3. Заморозка / Разморозка счетов клиентов банка
        elif action == "freeze":
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT account_number, user_id, balance, is_frozen FROM bank_accounts WHERE bank_id = ? LIMIT 25",
                    (bank_id,)
                ) as cursor:
                    accounts = await cursor.fetchall()

            if not accounts:
                return await inter.response.send_message("ℹ️ В этом банке пока не открыто ни одного лицевого счета.", ephemeral=True)

            options = [
                disnake.SelectOption(
                    label=f"Счет {acc['account_number']} ({'🔒 Заморожен' if acc['is_frozen'] else '🟢 Активен'})",
                    value=acc["account_number"],
                    description=f"Баланс: {format_number(acc['balance'])} R$ | Владелец: {acc['user_id']}"[:100]
                )
                for acc in accounts
            ]

            view = disnake.ui.View(timeout=60)
            select = disnake.ui.StringSelect(
                custom_id=f"bank_freeze_select:{bank_id}",
                placeholder="Выберите лицевой счет для переключения статуса",
                options=options
            )
            view.add_item(select)
            await inter.response.send_message("Выберите счет клиента для заморозки или разблокировки:", view=view, ephemeral=True)

        # 4. Обновление карточки
        elif action == "refresh":
            await inter.response.defer()
            comps = await self.build_bank_control_components(bank_id)
            try:
                await inter.edit_original_message(components=comps)
            except Exception:
                # Если кнопка была нажата на самом сообщении канала
                await inter.message.edit(components=comps)


    @commands.Cog.listener("on_modal_submit")
    async def handle_bank_modals(self, inter: disnake.ModalInteraction):
        custom_id = inter.custom_id

        # 1. Редактирование параметров банка
        if custom_id.startswith("modal:bank_edit:"):
            bank_id = int(custom_id.split(":")[2])
            new_name = inter.text_values.get("new_name", "").strip()
            new_rate_raw = inter.text_values.get("new_rate", "").strip()

            selected_owners = inter.values.get("new_owner", [])
            new_owner_id = int(selected_owners[0]) if selected_owners else None

            changes = []
            async with aiosqlite.connect(DB_PATH) as db:
                if new_name:
                    await db.execute("UPDATE banks SET name = ? WHERE id = ?", (new_name, bank_id))
                    changes.append(f"• **Название:** `{new_name}`")
                if new_owner_id:
                    await db.execute("UPDATE banks SET owner_id = ? WHERE id = ?", (new_owner_id, bank_id))
                    changes.append(f"• **Владелец:** <@{new_owner_id}>")
                if new_rate_raw:
                    try:
                        new_rate = float(new_rate_raw.replace(",", "."))
                        await db.execute("UPDATE banks SET interest_rate = ? WHERE id = ?", (new_rate, bank_id))
                        changes.append(f"• **Ставка:** `{new_rate}%`")
                    except ValueError:
                        pass
                await db.commit()

            bank = await get_bank(bank_id)
            if bank and bank["control_channel_id"] and bank["control_message_id"]:
                try:
                    ch = self.bot.get_channel(bank["control_channel_id"])
                    if ch:
                        msg = await ch.fetch_message(bank["control_message_id"])
                        comps = await self.build_bank_control_components(bank_id)
                        await msg.edit(components=comps)
                except Exception:
                    pass

            if changes:
                desc = "Были обновлены параметры банка:\n" + "\n".join(changes)
                await inter.response.send_message(desc, ephemeral=True, delete_after=10)
            else:
                await inter.response.defer()

        # 2. Пополнение / снятие средств из казны банка
        elif custom_id.startswith("bank_modal:balance:"):
            bank_id = int(custom_id.split(":")[2])
            bank = await get_bank(bank_id)
            if not bank:
                return await inter.response.send_message("❌ Банк не найден.", ephemeral=True)

            raw_action = inter.text_values["action_type"].strip().lower()
            raw_amount = inter.text_values["amount"].strip().replace(" ", "").replace(",", "")

            if not raw_amount.isdigit() or int(raw_amount) <= 0:
                return await inter.response.send_message("❌ Некорректная сумма операции.", ephemeral=True)

            amount = int(raw_amount)
            user_balance = await get_user_info(inter.author.id, "balance") or 0

            # Внесение наличных средств владельцем в казну банка
            if any(word in raw_action for word in ["пополнить", "внести", "взнос", "+", "deposit"]):
                if user_balance < amount:
                    return await inter.response.send_message(
                        f"❌ Недостаточно наличных средств! (У вас на руках: `{format_number(user_balance)}` R$).",
                        ephemeral=True
                    )

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, inter.author.id))
                    await db.execute("UPDATE banks SET balance = balance + ? WHERE id = ?", (amount, bank_id))
                    await db.commit()

                await self.bank_money_logger(
                    bank_id=bank_id,
                    op_type="deposit",
                    user1_id=inter.author.id,
                    amount=amount,
                    extra="Владелец пополнил казну банка из личных средств."
                )

                await inter.response.send_message(
                    f"✅ Вы внесли `{format_number(amount)}` R$ в казну банка «{bank['name']}»!",
                    ephemeral=True
                )

            # Снятие средств владельцем из казны банка в наличные
            elif any(word in raw_action for word in ["снять", "вывести", "-", "withdraw"]):
                if bank["balance"] < amount:
                    return await inter.response.send_message(
                        f"❌ В казне банка недостаточно средств! (Резерв: `{format_number(bank['balance'])}` R$).",
                        ephemeral=True
                    )

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (amount, bank_id))
                    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, inter.author.id))
                    await db.commit()

                await self.bank_money_logger(
                    bank_id=bank_id,
                    op_type="withdraw",
                    user1_id=inter.author.id,
                    amount=amount,
                    extra="Владелец снял средства из казны банка на руки."
                )

                await inter.response.send_message(
                    f"✅ Вы успешно вывели `{format_number(amount)}` R$ из казны банка!",
                    ephemeral=True
                )
            else:
                return await inter.response.send_message(
                    "❌ Неизвестное действие. Укажите: **пополнить** или **снять**.",
                    ephemeral=True
                )

            # Обновляем карточку управления банком
            components = await self.build_bank_control_components(bank_id)
            channel = self.bot.get_channel(bank["control_channel_id"])
            if channel and bank["control_message_id"]:
                try:
                    msg = await channel.fetch_message(bank["control_message_id"])
                    await msg.edit(components=components)
                except Exception:
                    pass


    @commands.Cog.listener("on_dropdown")
    async def handle_bank_dropdowns(self, inter: disnake.MessageInteraction):
        if not inter.component.custom_id.startswith("bank_freeze_select:"):
            return

        bank_id = int(inter.component.custom_id.split(":")[1])
        acc_num = inter.values[0]

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT is_frozen FROM bank_accounts WHERE account_number = ?", (acc_num,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return await inter.response.edit_message(content="❌ Счет не найден.", view=None)

            new_status = 0 if row["is_frozen"] else 1
            await db.execute("UPDATE bank_accounts SET is_frozen = ? WHERE account_number = ?", (new_status, acc_num))
            await db.commit()

        status_label = "заморожен 🔒" if new_status else "разблокирован 🟢"
        await inter.response.edit_message(content=f"✅ Лицевой счет `{acc_num}` теперь **{status_label}**.", view=None)



    # =========================================================
    #                   СИСТЕМА КРЕДИТОВАНИЯ
    # =========================================================



    @commands.slash_command(name="credit", description="Управление кредитами и выплатами")
    async def credit_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    # Автодополнение активных кредитов пользователя
    async def user_loans_autocomp(self, inter: disnake.ApplicationCommandInteraction, user_input: str):
        loans = await get_user_loans(inter.author.id, only_active=True)
        return {
            f"#{loan['id']} | {loan['bank_name']} (Остаток: {format_number(loan['remaining_debt'])} R$)": str(loan['id'])
            for loan in loans if user_input in str(loan['id']) or user_input.lower() in loan['bank_name'].lower()
        }

    # 1. Список кредитов пользователя
    @credit_group.sub_command(name="list", description="Посмотреть ваши активные кредиты")
    async def credit_list(self, inter: disnake.ApplicationCommandInteraction):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)
        loans = await get_user_loans(inter.author.id, only_active=True)

        if not loans:
            embed = create_embed(
                title="💳 Ваши кредиты",
                description="У вас нет активных кредитных задолженностей.",
                color=disnake.Color.green()
            )
            return await inter.edit_original_message(embed=embed)

        now = int(time.time())
        cards = []
        for loan in loans:
            is_overdue = now > loan["next_payment_time"]
            status_text = "🚨 **ПРОСРОЧЕН**" if is_overdue else "🟢 Активен"
            time_text = f"<t:{loan['next_payment_time']}:R> (<t:{loan['next_payment_time']}:F>)"

            cards.append(
                f"### Кредит #{loan['id']} в «{loan['bank_name']}»\n"
                f"• **Счет погашения:** `{loan['account_number']}`\n"
                f"• **Взято:** `{format_number(loan['original_amount'])}` R$\n"
                f"• **Остаток к выплате:** `{format_number(loan['remaining_debt'])}` R$ (из `{format_number(loan['total_debt'])}`)\n"
                f"• **Дата взятия:** <t:{loan['created_at']}:D>\n"
                f"• **Срок погашения:** {time_text}\n"
                f"• **Статус:** {status_text}\n"
            )

        components = [
            disnake.ui.Container(disnake.ui.TextDisplay(content="\n---\n".join(cards)))
        ]
        await inter.edit_original_message(components=components)

    # 2. Погашение кредита
    @credit_group.sub_command(name="pay", description="Оплатить часть или всю сумму кредита")
    async def credit_pay(
        self,
        inter: disnake.ApplicationCommandInteraction,
        кредит_id: str = commands.Param(description="Выберите кредит", autocomplete=user_loans_autocomp),
        сумма: int = commands.Param(description="Сумма для погашения", ge=1),
        способ: str = commands.Param(
            default="account",
            choices=[
                disnake.OptionChoice(name="С привязанного лицевого счета", value="account"),
                disnake.OptionChoice(name="Наличными деньгами", value="cash")
            ],
            description="Откуда списать средства"
        )
    ):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)
        loan = await get_loan_by_id(int(кредит_id))
        if not loan or loan["user_id"] != inter.author.id or loan["is_closed"]:
            return await inter.edit_original_message(content="❌ Кредит не найден или уже полностью закрыт.")

        pay_amount = min(сумма, loan["remaining_debt"])

        # Списание средств
        if способ == "account":
            acc = await self.validate_account(inter, loan["account_number"], check_owner=True, check_frozen=True)
            if not acc:
                return

            if acc["balance"] < pay_amount:
                return await inter.edit_original_message(
                    content=f"❌ Недостаточно средств на счете `{acc['account_number']}`! (Баланс: `{format_number(acc['balance'])}` R$)."
                )

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE bank_accounts SET balance = balance - ? WHERE account_number = ?", (pay_amount, acc["account_number"]))
        else:
            cash = await get_user_info(inter.author.id, "balance") or 0
            if cash < pay_amount:
                return await inter.edit_original_message(
                    content=f"❌ Недостаточно наличных денег! (В наличии: `{format_number(cash)}` R$)."
                )

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (pay_amount, inter.author.id))

        new_remaining = loan["remaining_debt"] - pay_amount
        is_closed = (new_remaining <= 0)

        # Зачисляем сумму платежа банку и уменьшаем долг
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE banks SET balance = balance + ? WHERE id = ?", (pay_amount, loan["bank_id"]))
            await db.execute(
                "UPDATE bank_loans SET remaining_debt = ?, is_closed = ? WHERE id = ?",
                (new_remaining, int(is_closed), loan["id"])
            )
            await db.commit()

        await self.bank_money_logger(
            bank_id=loan["bank_id"],
            op_type="deposit",
            user1_id=inter.author.id,
            account1=loan["account_number"],
            amount=pay_amount,
            extra=f"Погашение кредита #{loan['id']}. Остаток: {format_number(new_remaining)} R$"
        )

        close_text = "\n🎉 **Кредит полностью погашен и закрыт!**" if is_closed else f"\nОстаток долга: `{format_number(new_remaining)}` R$."
        embed = create_embed(
            title="💳 Платеж по кредиту принят",
            description=f"Вы внесли `{format_number(pay_amount)}` R$ в счет кредита #{loan['id']}.{close_text}",
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)

    # 3. Детальная информация о кредите
    @credit_group.sub_command(name="info", description="Подробная информация о конкретном кредите")
    async def credit_info(
        self,
        inter: disnake.ApplicationCommandInteraction,
        кредит_id: str = commands.Param(description="Выберите кредит", autocomplete=user_loans_autocomp)
    ):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)
        loan = await get_loan_by_id(int(кредит_id))
        if not loan or loan["user_id"] != inter.author.id:
            return await inter.edit_original_message(content="❌ Кредит не найден.")

        now = int(time.time())
        is_overdue = now > loan["next_payment_time"] and not loan["is_closed"]

        status = "🟢 Закрыт" if loan["is_closed"] else ("🚨 Просрочен (наложены санкции)" if is_overdue else "⏳ Активен")

        desc = (
            f"**Кредитный договор:** `#{loan['id']}`\n"
            f"**Банк:** {loan['bank_name']}\n"
            f"**Лицевой счет:** `{loan['account_number']}`\n\n"
            f"• **Тело кредита:** `{format_number(loan['original_amount'])}` R$\n"
            f"• **Процентная ставка:** `{loan['interest_rate']}%`\n"
            f"• **Сумма к возврату:** `{format_number(loan['total_debt'])}` R$\n"
            f"• **Осталось выплатить:** `{format_number(loan['remaining_debt'])}` R$\n\n"
            f"📅 **Дата оформления:** <t:{loan['created_at']}:F>\n"
            f"⏰ **Крайний срок:** <t:{loan['next_payment_time']}:F> (<t:{loan['next_payment_time']}:R>)\n"
            f"📊 **Текущий статус:** {status}"
        )

        embed = create_embed(title=f"Информация по кредиту #{loan['id']}", description=desc, color=disnake.Color.purple())
        await inter.edit_original_message(embed=embed)





def setup(bot: commands.Bot):
    bot.add_cog(BanksCog(bot))