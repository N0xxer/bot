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
    is_user_registered,
    get_loan_request_by_id,
    get_user_bank_active_loans_count,
    has_pending_loan_request,
    get_account_active_loans,
    close_bank_account,
    get_national_bank,
    set_national_bank,
    get_bank_active_interbank_loans,
    get_bank_interbank_loans_count,
    has_pending_interbank_loan_request,
    rename_bank_account,
    get_user_bank_accounts_count,
    add_to_bank_blacklist,
    remove_from_bank_blacklist,
    get_user_bank_blacklist_entry,
    get_bank_blacklist,
    create_bank_deposit,
    get_user_deposits,
    get_bank_deposits_total,
    get_deposit_by_id,
    close_bank_deposit
)
from functions.utils import ensure_admin, ensure_user_registered, notification_send, create_embed, format_number, MAIN_COLOR


class BanksCog(commands.Cog):
    """Банковская система Резендии."""
    
    # ID категории для создания каналов банков
    BANK_CATEGORY_ID = 1548395973338996836

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_overdue_loans.start()
        self.process_deposit_payouts.start()

    async def cog_load(self):
        await init_bank_db()

    def cog_unload(self):
        self.check_overdue_loans.cancel()
        self.process_deposit_payouts.cancel()

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

    @tasks.loop(hours=24)
    async def process_deposit_payouts(self):
        """Периодическое начисление процентов по вкладам (раз в сутки)."""
        now = int(time.time())
        updated_banks = set()
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT d.*, b.name as bank_name, b.balance as bank_balance
                FROM bank_deposits d
                JOIN banks b ON d.bank_id = b.id
                WHERE d.is_closed = 0 AND d.last_payout_time <= ?
            """, (now - 86400,)) as cursor:
                deposits = await cursor.fetchall()

            bank_balances = {}
            for dep in deposits:
                bid = dep["bank_id"]
                if bid not in bank_balances:
                    bank_balances[bid] = dep["bank_balance"]

                # Начисляем процент от суммы депозита
                profit = max(1, int(dep["amount"] * (dep["interest_rate"] / 100)))
                # Проверяем, есть ли у банка средства в казне на выплату процентов
                if bank_balances[bid] >= profit:
                    bank_balances[bid] -= profit
                    await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (profit, bid))
                    await db.execute("UPDATE bank_accounts SET balance = balance + ? WHERE account_number = ?", (profit, dep["account_number"]))
                    await db.execute("UPDATE bank_deposits SET last_payout_time = ? WHERE id = ?", (now, dep["id"]))
                    updated_banks.add(bid)

                    await self.bank_money_logger(
                        bank_id=dep["bank_id"],
                        op_type="withdraw",
                        user1_id=dep["user_id"],
                        account1=dep["account_number"],
                        amount=profit,
                        extra=f"Выплата процентов по вкладу #{dep['id']} ({dep['interest_rate']}%): +{format_number(profit)} R$"
                    )

                    user = self.bot.get_user(dep["user_id"])
                    if user:
                        embed = create_embed(
                            title="📈 Доход по вкладу",
                            description=(
                                f"Вам начислены проценты по вкладу **#{dep['id']}** в банке «{dep['bank_name']}»!\n\n"
                                f"• Начислено: `+{format_number(profit)}` R$ ({dep['interest_rate']}%)\n"
                                f"• Средства зачислены на счет: `{dep['account_number']}`"
                            ),
                            color=disnake.Color.green()
                        )
                        try:
                            await user.send(embed=embed)
                        except disnake.Forbidden:
                            pass
            await db.commit()

        # Обновляем контрольные панели банков, у которых изменился баланс казны
        for bid in updated_banks:
            try:
                b = await get_bank(bid)
                if b and b["control_channel_id"] and b["control_message_id"]:
                    ch = self.bot.get_channel(b["control_channel_id"])
                    if ch:
                        m = await ch.fetch_message(b["control_message_id"])
                        comps = await self.build_bank_control_components(bid)
                        await m.edit(components=comps)
            except Exception:
                pass

    @process_deposit_payouts.before_loop
    async def before_process_deposit_payouts(self):
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

        min_loan = bank["min_loan_amount"] if "min_loan_amount" in bank.keys() else 100
        max_loan = bank["max_loan_amount"] if "max_loan_amount" in bank.keys() else 1000000
        threshold = bank["loan_approval_threshold"] if "loan_approval_threshold" in bank.keys() else 100000
        max_user_loans = bank["max_loans_per_user"] if "max_loans_per_user" in bank.keys() else 1
        max_accs = bank["max_accounts_per_user"] if "max_accounts_per_user" in bank.keys() else 3
        threshold_text = f"`{format_number(threshold)}` R$" if threshold > 0 else "Отключено (авто-выдача)"
        is_nat = bool(bank["is_national"] if "is_national" in bank.keys() else False)

        dep_enabled = bool(bank["deposits_enabled"] if "deposits_enabled" in bank.keys() else False)
        dep_rate = bank["deposit_interest_rate"] if "deposit_interest_rate" in bank.keys() else 3.0
        dep_status_text = f"🟢 Включены (`{dep_rate}%` в сутки)" if dep_enabled else "🔴 Выключены"
        deposits_total = await get_bank_deposits_total(bank_id)

        status_badge = "\n🌟 **СТАТУС: НАЦИОНАЛЬНЫЙ БАНК РЕЗЕНДИИ** 🏛️" if is_nat else ""
        loan_target_desc = "межбанковские кредиты" if is_nat else "кредитование граждан"

        # Проверяем, есть ли у этого банка непогашенные межбанковские кредиты в Нацбанке
        interbank_loans = await get_bank_active_interbank_loans(bank_id) if not is_nat else []
        interbank_debt_total = sum(l["remaining_debt"] for l in interbank_loans)
        interbank_info = f"\n• **Долг перед Нацбанком:** `{format_number(interbank_debt_total)}` R$ ({len(interbank_loans)} кред.)" if interbank_debt_total > 0 else ""

        content = (
            f"### 🏛️ Панель управления банком «{bank['name']}»{status_badge}\n\n"
            f"• **ID банка:** `{bank['id']}`\n"
            f"• **Тип:** `{bank['type']}`\n"
            f"• **Владелец:** {owner_text}\n"
            f"• **Казна (Резерв):** `{format_number(bank['balance'])}` R$\n"
            f"• **Кредитная ставка:** `{bank['interest_rate']}%`\n"
            f"• **Лимиты ({loan_target_desc}):** от `{format_number(min_loan)}` до `{format_number(max_loan)}` R$\n"
            f"• **Порог одобрения:** {threshold_text}\n"
            f"• **Лимит кредитов на клиента:** `{max_user_loans}` шт.\n"
            f"• **Лимит счетов на человека:** `{max_accs}` шт.\n"
            f"• **Вклады (Депозиты):** {dep_status_text} (В обороте: `{format_number(deposits_total)}` R$)\n"
            f"• **Активные выданные займы:** `{format_number(active_loans_total)}` R${interbank_info}\n"
        )

        row1 = disnake.ui.ActionRow(
            disnake.ui.Button(
                label="Изменить данные",
                emoji="⚙️",
                style=disnake.ButtonStyle.secondary,
                custom_id=f"bank_ctrl:edit:{bank_id}"
            ),
            disnake.ui.Button(
                label="Лимиты",
                emoji="📊",
                style=disnake.ButtonStyle.secondary,
                custom_id=f"bank_ctrl:limits:{bank_id}"
            ),
            disnake.ui.Button(
                label="Счета клиентов",
                emoji="🔒",
                style=disnake.ButtonStyle.secondary,
                custom_id=f"bank_ctrl:freeze:{bank_id}"
            ),
            disnake.ui.Button(
                label="Казна",
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

        row2 = disnake.ui.ActionRow(
            disnake.ui.Button(
                label="Депозиты",
                emoji="📈",
                style=disnake.ButtonStyle.primary,
                custom_id=f"bank_ctrl:deposits:{bank_id}"
            ),
            disnake.ui.Button(
                label="Чёрный список",
                emoji="🚫",
                style=disnake.ButtonStyle.danger,
                custom_id=f"bank_ctrl:blacklist:{bank_id}"
            )
        )

        rows = [disnake.ui.Container(disnake.ui.TextDisplay(content=content)), row1, row2]

        # Для коммерческих банков добавляем третий ряд с кнопками межбанковского кредитования
        if not is_nat:
            nat_bank = await get_national_bank()
            if nat_bank:
                row3_buttons = [
                    disnake.ui.Button(
                        label="Кредит в Нацбанке",
                        emoji="🏛️",
                        style=disnake.ButtonStyle.primary,
                        custom_id=f"bank_ctrl:interbank_req:{bank_id}"
                    )
                ]
                if interbank_debt_total > 0:
                    row3_buttons.append(
                        disnake.ui.Button(
                            label="Погасить долг Нацбанку",
                            emoji="💸",
                            style=disnake.ButtonStyle.danger,
                            custom_id=f"bank_ctrl:interbank_pay:{bank_id}"
                        )
                    )
                rows.append(disnake.ui.ActionRow(*row3_buttons))

        return rows

    async def user_accounts_autocomp(self, inter: disnake.ApplicationCommandInteraction, user_input: str):
        accounts = await get_user_accounts(inter.author.id)
        res = {}
        for acc in accounts:
            acc_name = acc["account_name"] if "account_name" in acc.keys() else None
            name_part = f" («{acc_name}»)" if acc_name else ""
            label = f"{acc['bank_name']} | {acc['account_number']}{name_part} ({format_number(acc['balance'])} R$)"
            if (user_input.lower() in acc['account_number'].lower() or
                user_input.lower() in acc['bank_name'].lower() or
                (acc_name and user_input.lower() in acc_name.lower())):
                res[label[:100]] = acc['account_number']
        return res


    async def bank_autocomplete(self, inter: disnake.ApplicationCommandInteraction, user_input: str):
        banks = await get_all_banks()
        return {
            f"{b['name']} (Резерв: {format_number(b['balance'])} R$)": str(b['id'])
            for b in banks if user_input.lower() in b["name"].lower() or user_input in str(b["id"])
        }

    async def user_deposits_autocomp(self, inter: disnake.ApplicationCommandInteraction, user_input: str):
        deposits = await get_user_deposits(inter.author.id, only_active=True)
        res = {}
        for dep in deposits:
            label = f"#{dep['id']} | {dep['bank_name']} ({format_number(dep['amount'])} R$, {dep['interest_rate']}%)"
            if user_input.lower() in str(dep["id"]) or user_input.lower() in dep["bank_name"].lower():
                res[label[:100]] = str(dep["id"])
        return res



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


    @bank_group.sub_command(
        name="set_national",
        description="Назначить банк Национальным банком Резендии (Администрация)"
    )
    @commands.has_permissions(administrator=True)
    async def bank_set_national(
        self,
        inter: disnake.ApplicationCommandInteraction,
        банк: str = commands.Param(description="Выберите банк", autocomplete=bank_autocomplete)
    ):
        if not await ensure_admin(inter):
            return

        await inter.response.defer(ephemeral=True)
        bank_id = int(банк)
        target_bank = await get_bank(bank_id)
        if not target_bank:
            return await inter.edit_original_message(content="❌ Указанный банк не найден.")

        # Назначаем выбранный банк Национальным банком
        await set_national_bank(bank_id)

        # Обновляем карточку банка
        comps = await self.build_bank_control_components(bank_id)
        channel = self.bot.get_channel(target_bank["control_channel_id"])
        if channel and target_bank["control_message_id"]:
            try:
                msg = await channel.fetch_message(target_bank["control_message_id"])
                await msg.edit(components=comps)
            except Exception:
                pass

        await notification_send(
            inter,
            title="🏛️ Назначен Национальный банк",
            description=f"Банк **«{target_bank['name']}»** официально наделен статусом **Национального банка Резендии**!\nНацбанк осуществляет межбанковское кредитование коммерческих банков страны.",
            color=disnake.Color.gold()
        )

        await inter.edit_original_message(content=f"✅ Банк **«{target_bank['name']}»** (ID: `{bank_id}`) успешно назначен **Национальным банком Резендии**.")


    @bank_group.sub_command(
        name="set_max_accounts",
        description="Установить максимальное число счетов на одного человека в банке"
    )
    async def bank_set_max_accounts(
        self,
        inter: disnake.ApplicationCommandInteraction,
        банк: str = commands.Param(description="Выберите банк", autocomplete=bank_autocomplete),
        лимит: int = commands.Param(description="Максимум счетов на человека (от 1 до 20)", ge=1, le=20)
    ):
        await inter.response.defer(ephemeral=True)
        bank_id = int(банк)
        target_bank = await get_bank(bank_id)
        if not target_bank:
            return await inter.edit_original_message(content="❌ Указанный банк не найден.")

        is_owner = (target_bank["owner_id"] == inter.author.id)
        is_admin = inter.author.guild_permissions.administrator
        if not (is_owner or is_admin):
            return await inter.edit_original_message(content="⛔ Настроить лимит счетов может только владелец данного банка или администратор.")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE banks SET max_accounts_per_user = ? WHERE id = ?", (лимит, bank_id))
            await db.commit()

        # Обновляем карточку банка
        comps = await self.build_bank_control_components(bank_id)
        channel = self.bot.get_channel(target_bank["control_channel_id"])
        if channel and target_bank["control_message_id"]:
            try:
                msg = await channel.fetch_message(target_bank["control_message_id"])
                await msg.edit(components=comps)
            except Exception:
                pass

        await inter.edit_original_message(
            content=f"✅ В банке **«{target_bank['name']}»** установлен лимит счетов: не более **{лимит}** сч./чел."
        )


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
                acc_name = acc["account_name"] if "account_name" in acc.keys() else None
                name_str = f"\nНазвание: {acc_name}" if acc_name else ""
                block = (
                    f"```Счет в банке {acc['bank_name']}\n"
                    f"Номер: {acc['account_number']}{name_str}\n"
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


    @bank_group.sub_command(name="close", description="Закрыть лицевой счет в банке")
    async def bank_account_close(
        self,
        inter: disnake.ApplicationCommandInteraction,
        счет: str = commands.Param(description="Номер закрываемого счета", autocomplete=user_accounts_autocomp)
    ):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)
        account = await self.validate_account(inter, счет, check_owner=True, check_frozen=False)
        if not account:
            return

        # Проверяем, нет ли активных незакрытых кредитов на этом счете
        active_loans = await get_account_active_loans(счет)
        if active_loans:
            loan_ids = ", ".join([f"#{l['id']}" for l in active_loans])
            return await inter.edit_original_message(
                content=f"❌ Нельзя закрыть лицевой счет `{счет}`, пока на нем числятся непогашенные кредиты ({loan_ids})! Погасите их через `/credit pay`."
            )

        balance_to_refund = account["balance"]
        closed_account = await close_bank_account(счет)
        if not closed_account:
            return await inter.edit_original_message(content="❌ Не удалось закрыть счет. Возможно, он уже был удален.")

        refund_text = f"\n💵 Остаток средств в размере `{format_number(balance_to_refund)}` R$ выдан вам на руки наличными." if balance_to_refund > 0 else ""

        await self.bank_money_logger(
            bank_id=account["bank_id"],
            op_type="close",
            user1_id=inter.author.id,
            account1=счет,
            amount=balance_to_refund,
            extra=f"Счет закрыт клиентом. Выплачено на руки: {format_number(balance_to_refund)} R$"
        )

        embed = create_embed(
            title="🗑️ Лицевой счет закрыт",
            description=f"Ваш лицевой счет `{счет}` в банке **«{account['bank_name']}»** был успешно аннулирован.{refund_text}",
            color=disnake.Color.dark_gray()
        )
        await inter.edit_original_message(embed=embed)


    @bank_group.sub_command(name="deposit_list", description="Посмотреть ваши открытые вклады (депозиты)")
    async def bank_deposit_list(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пользователь: Optional[disnake.Member] = commands.Param(default=None, description="Чьи вклады посмотреть")
    ):
        target = пользователь or inter.author
        if not await ensure_user_registered(inter, target.id):
            return

        await inter.response.defer(ephemeral=True)
        deposits = await get_user_deposits(target.id, only_active=False)

        lines = [f"## Вклады (депозиты) пользователя {target.mention}\n"]
        if not deposits:
            lines.append("```У пользователя нет активных или закрытых вкладов.```")
        else:
            for dep in deposits:
                status_str = "🔴 Закрыт" if dep["is_closed"] else "🟢 Активен (начисление % каждые 24ч)"
                daily_profit = max(1, int(dep["amount"] * (dep["interest_rate"] / 100)))
                block = (
                    f"```Вклад #{dep['id']} в банке «{dep['bank_name']}»\n"
                    f"Тело вклада: {format_number(dep['amount'])} R$\n"
                    f"Ставка: {dep['interest_rate']}% в сутки (~+{format_number(daily_profit)} R$/день)\n"
                    f"Счет зачисления: {dep['account_number']}\n"
                    f"Статус: {status_str}```"
                )
                lines.append(block)

        await inter.edit_original_message(content="\n".join(lines))


    @bank_group.sub_command(name="deposit_close", description="Закрыть вклад и вернуть средства на счет")
    async def bank_deposit_close_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        вклад_id: str = commands.Param(description="Выберите вклад для закрытия", autocomplete=user_deposits_autocomp)
    ):
        if not await ensure_user_registered(inter, inter.author.id):
            return

        await inter.response.defer(ephemeral=True)
        try:
            dep_id = int(вклад_id)
        except ValueError:
            return await inter.edit_original_message(content="❌ Некорректный ID вклада.")

        dep = await get_deposit_by_id(dep_id)
        if not dep or dep["user_id"] != inter.author.id:
            return await inter.edit_original_message(content="❌ Вклад не найден.")

        if dep["is_closed"]:
            return await inter.edit_original_message(content="ℹ️ Этот вклад уже закрыт.")

        bank = await get_bank(dep["bank_id"])
        if not bank:
            return await inter.edit_original_message(content="❌ Банк не найден.")

        # Проверяем наличие средств в казне банка для возврата тела вклада
        if bank["balance"] < dep["amount"]:
            return await inter.edit_original_message(
                content=(
                    f"❌ **В казне банка недостаточно средств для возврата вклада!**\n\n"
                    f"• Требуется к выплате: `{format_number(dep['amount'])}` R$\n"
                    f"• В казне банка «{dep['bank_name']}»: `{format_number(bank['balance'])}` R$\n\n"
                    f"⚠️ Банк временно не может закрыть вклад из-за недостатка ликвидности. Свяжитесь с руководством банка."
                )
            )

        # Возвращаем тело депозита на счет
        account = await get_account_by_number(dep["account_number"])
        if not account or account["is_frozen"]:
            return await inter.edit_original_message(
                content=f"❌ Счет зачисления `{dep['account_number']}` заблокирован или отсутствует! Разблокируйте счет для вывода средств."
            )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE bank_accounts SET balance = balance + ? WHERE account_number = ?", (dep["amount"], dep["account_number"]))
            await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (dep["amount"], dep["bank_id"]))
            await db.execute("UPDATE bank_deposits SET is_closed = 1 WHERE id = ?", (dep_id,))
            await db.commit()

        await self.bank_money_logger(
            bank_id=dep["bank_id"],
            op_type="deposit",
            user1_id=inter.author.id,
            account1=dep["account_number"],
            amount=dep["amount"],
            extra=f"Закрытие вклада #{dep_id}. Тело депозита ({format_number(dep['amount'])} R$) выплачено из казны банка на счет {dep['account_number']}."
        )

        # Обновляем панель управления банка
        comps = await self.build_bank_control_components(dep["bank_id"])
        ctrl_ch = self.bot.get_channel(bank["control_channel_id"])
        if ctrl_ch and bank["control_message_id"]:
            try:
                m = await ctrl_ch.fetch_message(bank["control_message_id"])
                await m.edit(components=comps)
            except Exception:
                pass

        embed = create_embed(
            title="📈 Вклад успешно закрыт",
            description=(
                f"Ваш вклад **#{dep_id}** в банке **«{dep['bank_name']}»** был успешно закрыт.\n\n"
                f"💵 Тело вклада в размере `{format_number(dep['amount'])}` R$ возвращено на ваш счет `{dep['account_number']}` из казны банка."
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
                disnake.ui.MediaGallery(disnake.MediaGalleryItem(media="attachment://banki.png"))
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
                ),
                disnake.ui.Button(
                    label="Открыть вклад",
                    style=disnake.ButtonStyle.primary,
                    emoji="📈",
                    custom_id="service_btn:open_deposit"
                ),
                disnake.ui.Button(
                    label="Закрыть счет",
                    style=disnake.ButtonStyle.danger,
                    emoji="🗑️",
                    custom_id="service_btn:close_account"
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
            # Физические лица не могут брать кредит в Национальном банке
            commercial_banks = [b for b in banks if not (b["is_national"] if "is_national" in b.keys() else False)]
            if not commercial_banks:
                return await inter.response.send_message("❌ В государстве нет коммерческих банков, выдающих кредиты гражданам.", ephemeral=True)

            loan_bank_options = [
                disnake.SelectOption(
                    label=b["name"][:100],
                    value=str(b["id"]),
                    description=f"Ставка: {b['interest_rate']}% | Баланс: {format_number(b['balance'])} R$"
                ) for b in commercial_banks[:25]
            ]

            modal_components = [
                disnake.ui.Label(
                    text="Выберите коммерческий банк",
                    component=disnake.ui.StringSelect(
                        custom_id="loan_bank_id",
                        placeholder="Банк-кредитор",
                        options=loan_bank_options,
                        min_values=1,
                        max_values=1
                    )
                ),
                disnake.ui.Label(
                    text="Сумма кредита (в R$)",
                    component=disnake.ui.TextInput(
                        custom_id="loan_amount",
                        placeholder="Например: 50000",
                        style=disnake.TextInputStyle.short,
                        min_length=1,
                        max_length=15,
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
                        max_length=4,
                        required=True
                    )
                )
            ]
            await inter.response.send_modal(
                title="Оформление кредита",
                custom_id="modal:bank_service:take_loan",
                components=modal_components
            )

        elif action == "open_deposit":
            # Выбираем только банки, где включены депозиты
            deposit_banks = [b for b in banks if ("deposits_enabled" in b.keys() and b["deposits_enabled"])]
            if not deposit_banks:
                return await inter.response.send_message("❌ В настоящее время ни один банк не принимает вклады.", ephemeral=True)

            user_accs = await get_user_accounts(inter.author.id)
            active_accs = [a for a in user_accs if not a["is_frozen"]]
            if not active_accs:
                return await inter.response.send_message("❌ Для открытия вклада вам необходим активный лицевой счет в банке. Сначала откройте счет.", ephemeral=True)

            dep_bank_options = [
                disnake.SelectOption(
                    label=b["name"][:100],
                    value=str(b["id"]),
                    description=f"Ставка: {b['deposit_interest_rate'] if 'deposit_interest_rate' in b.keys() else 3.0}% | Мин. вклад: {format_number(b['min_deposit_amount'] if 'min_deposit_amount' in b.keys() else 1000)} R$"[:100]
                ) for b in deposit_banks[:25]
            ]

            acc_options = [
                disnake.SelectOption(
                    label=f"{a['account_number']} ({a['bank_name']})"[:100],
                    value=a["account_number"],
                    description=f"Баланс: {format_number(a['balance'])} R$"[:100]
                ) for a in active_accs[:25]
            ]

            modal_components = [
                disnake.ui.Label(
                    text="Выберите банк для вклада",
                    component=disnake.ui.StringSelect(
                        custom_id="deposit_bank_id",
                        placeholder="Банк для открытия депозита",
                        options=dep_bank_options,
                        min_values=1,
                        max_values=1
                    )
                ),
                disnake.ui.Label(
                    text="Счет списания средств",
                    component=disnake.ui.StringSelect(
                        custom_id="deposit_acc_num",
                        placeholder="Счет списания",
                        options=acc_options,
                        min_values=1,
                        max_values=1
                    )
                ),
                disnake.ui.Label(
                    text="Сумма вклада (в R$)",
                    component=disnake.ui.TextInput(
                        custom_id="deposit_amount",
                        placeholder="Например: 50000",
                        style=disnake.TextInputStyle.short,
                        min_length=1,
                        max_length=15,
                        required=True
                    )
                )
            ]
            await inter.response.send_modal(
                title="Открытие вклада (депозита)",
                custom_id="modal:bank_service:open_deposit",
                components=modal_components
            )

        elif action == "close_account":
            user_accs = await get_user_accounts(inter.author.id)
            if not user_accs:
                return await inter.response.send_message("❌ У вас нет открытых лицевых счетов в банках.", ephemeral=True)

            acc_options = [
                disnake.SelectOption(
                    label=f"{acc['account_number']} ({acc['bank_name']})",
                    value=acc["account_number"],
                    description=f"Баланс: {format_number(acc['balance'])} R$"[:100]
                ) for acc in user_accs[:25]
            ]

            view = disnake.ui.View(timeout=60)
            select = disnake.ui.StringSelect(
                custom_id="service_select:close_acc",
                placeholder="Выберите счет, который хотите закрыть",
                options=acc_options
            )
            view.add_item(select)
            await inter.response.send_message("Выберите лицевой счет для закрытия (удаления):", view=view, ephemeral=True)

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
            if not bank:
                return await inter.response.send_message("❌ Банк не найден.", ephemeral=True)

            # Проверка чёрного списка банка
            bl_entry = await get_user_bank_blacklist_entry(bank_id, inter.author.id)
            if bl_entry:
                return await inter.response.send_message(
                    f"⛔ **Отказано в обслуживании!**\nВы внесены в **чёрный список** банка «{bank['name']}».\n**Причина:** {bl_entry['reason']}",
                    ephemeral=True
                )

            # Проверка лимита счетов на одного человека в этом банке
            max_accs = bank["max_accounts_per_user"] if "max_accounts_per_user" in bank.keys() else 3
            user_acc_count = await get_user_bank_accounts_count(inter.author.id, bank_id)
            if user_acc_count >= max_accs:
                return await inter.response.send_message(
                    f"❌ В банке «{bank['name']}» действует ограничение: не более **{max_accs}** счетов на одного человека. У вас уже открыто: **{user_acc_count}**.",
                    ephemeral=True
                )

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
            raw_amount = inter.text_values.get("loan_amount", "").strip().replace(" ", "")
            raw_days = inter.text_values.get("loan_days", "").strip().replace(" ", "")

            if not raw_amount.isdigit() or not raw_days.isdigit():
                return await inter.response.send_message("❌ Сумма и срок должны быть числами.", ephemeral=True, delete_after=10)

            amount = int(raw_amount)
            days = int(raw_days)

            bank = await get_bank(bank_id)
            if not bank:
                return await inter.response.send_message("❌ Выбранный банк не найден.", ephemeral=True, delete_after=10)

            # Проверка: Национальный банк кредитует только коммерческие банки
            is_national = bank["is_national"] if "is_national" in bank.keys() else False
            if is_national:
                return await inter.response.send_message(
                    "❌ Национальный банк Резендии кредитует исключительно коммерческие банки государства. Кредитование физических лиц запрещено.",
                    ephemeral=True,
                    delete_after=15
                )

            # Проверка чёрного списка банка (запрет на взятие кредитов)
            bl_entry = await get_user_bank_blacklist_entry(bank_id, inter.author.id)
            if bl_entry:
                return await inter.response.send_message(
                    f"⛔ **Отказано в кредитовании!**\nВы находитесь в **чёрном списке** банка «{bank['name']}».\n**Причина:** {bl_entry['reason']}",
                    ephemeral=True,
                    delete_after=15
                )

            min_loan = bank["min_loan_amount"] if "min_loan_amount" in bank.keys() else 100
            max_loan = bank["max_loan_amount"] if "max_loan_amount" in bank.keys() else 1000000
            threshold = bank["loan_approval_threshold"] if "loan_approval_threshold" in bank.keys() else 100000
            max_user_loans = bank["max_loans_per_user"] if "max_loans_per_user" in bank.keys() else 1

            if not (min_loan <= amount <= max_loan):
                return await inter.response.send_message(
                    f"❌ Сумма кредита в данном банке должна быть в диапазоне от `{format_number(min_loan)}` до `{format_number(max_loan)}` R$.",
                    ephemeral=True,
                    delete_after=10
                )

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

            # Проверка максимального количества активных кредитов в этом банке
            active_loans_count = await get_user_bank_active_loans_count(inter.author.id, bank_id)
            if active_loans_count >= max_user_loans:
                return await inter.response.send_message(
                    f"❌ Превышен лимит активных кредитов в банке «{bank['name']}»! Допустимо: `{max_user_loans}`, у вас активно: `{active_loans_count}`.",
                    ephemeral=True,
                    delete_after=10
                )

            # Проверка, нет ли уже ожидающей заявки
            if await has_pending_loan_request(inter.author.id, bank_id):
                return await inter.response.send_message(
                    "❌ У вас уже есть заявка на кредит в этом банке, ожидающая рассмотрения руководством.",
                    ephemeral=True,
                    delete_after=10
                )

            total_debt = int(amount * (1 + (bank["interest_rate"] / 100)))

            # Проверяем, требуется ли ручное одобрение руководством банка
            needs_approval = (threshold > 0 and amount >= threshold)

            if needs_approval:
                embed = create_embed(
                    title="📝 Заявка на кредит (требует одобрения)",
                    description=(
                        f"🏦 **Банк:** {bank['name']}\n"
                        f"💵 **Запрашиваемая сумма:** `{format_number(amount)}` R$\n"
                        f"📈 **Итого к возврату:** `{format_number(total_debt)}` R$ ({bank['interest_rate']}%)\n"
                        f"⏳ **Срок:** `{days}` дн.\n"
                        f"💳 **Счет зачисления:** `{target_acc['account_number']}`\n\n"
                        f"⚠️ Сумма кредита превышает установленный порог авто-выдачи (`{format_number(threshold)}` R$).\n"
                        f"Заявка будет направлена руководству банка на рассмотрение."
                    ),
                    color=disnake.Color.gold()
                )
                view = disnake.ui.View(timeout=60)
                view.add_item(disnake.ui.Button(label="Отправить на рассмотрение", style=disnake.ButtonStyle.primary, custom_id=f"submit_loan_req:{bank_id}:{amount}:{days}"))
                view.add_item(disnake.ui.Button(label="Отмена", style=disnake.ButtonStyle.secondary, custom_id="confirm_acc_cancel"))
                await inter.response.send_message(embed=embed, view=view, ephemeral=True, delete_after=45)
            else:
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

        elif action == "open_deposit":
            bank_id = int(inter.values.get("deposit_bank_id")[0])
            acc_num = inter.values.get("deposit_acc_num")[0]
            raw_amount = inter.text_values.get("deposit_amount", "").strip().replace(" ", "")

            if not raw_amount.isdigit():
                return await inter.response.send_message("❌ Сумма вклада должна быть числом.", ephemeral=True, delete_after=10)

            amount = int(raw_amount)
            bank = await get_bank(bank_id)
            if not bank:
                return await inter.response.send_message("❌ Выбранный банк не найден.", ephemeral=True, delete_after=10)

            if not (bank["deposits_enabled"] if "deposits_enabled" in bank.keys() else False):
                return await inter.response.send_message("❌ Приём депозитов в данном банке приостановлен.", ephemeral=True, delete_after=10)

            min_dep = bank["min_deposit_amount"] if "min_deposit_amount" in bank.keys() else 1000
            if amount < min_dep:
                return await inter.response.send_message(
                    f"❌ Минимальная сумма вклада в данном банке составляет `{format_number(min_dep)}` R$.",
                    ephemeral=True,
                    delete_after=10
                )

            account = await get_account_by_number(acc_num)
            if not account or account["user_id"] != inter.author.id:
                return await inter.response.send_message("❌ Счет списания не найден.", ephemeral=True, delete_after=10)

            if account["is_frozen"]:
                return await inter.response.send_message("❌ Счет списания заморожен.", ephemeral=True, delete_after=10)

            if account["balance"] < amount:
                return await inter.response.send_message(
                    f"❌ Недостаточно средств на счете `{acc_num}` (Доступно: `{format_number(account['balance'])}` R$, требуется: `{format_number(amount)}` R$).",
                    ephemeral=True,
                    delete_after=10
                )

            # Списываем средства со счета, пополняем казну банка и открываем депозит
            interest_rate = bank["deposit_interest_rate"] if "deposit_interest_rate" in bank.keys() else 3.0
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE bank_accounts SET balance = balance - ? WHERE account_number = ?", (amount, acc_num))
                await db.execute("UPDATE banks SET balance = balance + ? WHERE id = ?", (amount, bank_id))
                await db.commit()

            dep_id = await create_bank_deposit(inter.author.id, bank_id, acc_num, amount, interest_rate)

            await self.bank_money_logger(
                bank_id=bank_id,
                op_type="deposit",
                user1_id=inter.author.id,
                account1=acc_num,
                amount=amount,
                extra=f"Открыт вклад #{dep_id} под {interest_rate}% в сутки. Списано со счета {acc_num}, зачислено в казну банка."
            )

            # Обновляем панель банка
            comps = await self.build_bank_control_components(bank_id)
            ctrl_ch = self.bot.get_channel(bank["control_channel_id"])
            if ctrl_ch and bank["control_message_id"]:
                try:
                    m = await ctrl_ch.fetch_message(bank["control_message_id"])
                    await m.edit(components=comps)
                except Exception:
                    pass

            daily_payout = int(amount * (interest_rate / 100))
            embed = create_embed(
                title="📈 Вклад успешно открыт!",
                description=(
                    f"**Банк:** «{bank['name']}»\n"
                    f"**Номер депозита:** `#{dep_id}`\n"
                    f"**Сумма вклада:** `{format_number(amount)}` R$\n"
                    f"**Ставка:** `{interest_rate}%` в сутки (около `{format_number(daily_payout)}` R$/день)\n"
                    f"**Счет для начислений:** `{acc_num}`\n\n"
                    f"💡 Средства поступили в оборот банка. Проценты начисляются автоматически каждые 24 часа из казны банка.\n"
                    f"Закрыть вклад и вернуть тело можно командой `/bank deposit_close`."
                ),
                color=disnake.Color.green()
            )
            await inter.response.send_message(embed=embed, ephemeral=True)



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
            if not bank:
                return await inter.response.edit_message(content="❌ Банк не найден.", embed=None, view=None)

            # Проверка ЧС в момент подтверждения
            bl_entry = await get_user_bank_blacklist_entry(bank_id, inter.author.id)
            if bl_entry:
                return await inter.response.edit_message(
                    content=f"⛔ Операция отклонена. Вы внесены в чёрный список банка «{bank['name']}» (Причина: {bl_entry['reason']}).",
                    embed=None,
                    view=None
                )

            # Проверка лимита количества счетов
            max_accs = bank["max_accounts_per_user"] if "max_accounts_per_user" in bank.keys() else 3
            user_acc_count = await get_user_bank_accounts_count(inter.author.id, bank_id)
            if user_acc_count >= max_accs:
                return await inter.response.edit_message(
                    content=f"❌ Превышен лимит счетов в банке «{bank['name']}» ({user_acc_count} из {max_accs})!",
                    embed=None,
                    view=None
                )

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
            _, bank_id_str, amount_str, days_str = custom_id.split(":")
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

        elif custom_id.startswith("submit_loan_req:"):
            _, bank_id_str, amount_str, days_str = custom_id.split(":")
            bank_id = int(bank_id_str)
            amount = int(amount_str)
            days = int(days_str)

            user_accs = await get_user_accounts(inter.author.id)
            target_acc = next((a for a in user_accs if a["bank_id"] == bank_id and not a["is_frozen"]), None)
            if not target_acc:
                return await inter.response.edit_message(content="❌ Активный счет не найден.", embed=None, view=None)

            bank = await get_bank(bank_id)
            now = int(time.time())

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    INSERT INTO bank_loan_requests (user_id, bank_id, account_number, amount, days, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """, (inter.author.id, bank_id, target_acc["account_number"], amount, days, now))
                req_id = cursor.lastrowid
                await db.commit()

            # Отправляем карточку заявки в канал управления банком
            control_channel = self.bot.get_channel(bank["control_channel_id"]) if bank["control_channel_id"] else None
            req_msg = None
            total_debt = int(amount * (1 + (bank["interest_rate"] / 100)))

            if control_channel:
                req_embed = create_embed(
                    title=f"📋 Заявка на кредит #{req_id}",
                    description=(
                        f"**Клиент:** {inter.author.mention} (`{inter.author.id}`)\n"
                        f"**Лицевой счет:** `{target_acc['account_number']}`\n"
                        f"**Запрашиваемая сумма:** `{format_number(amount)}` R$\n"
                        f"**Срок:** `{days}` дн.\n"
                        f"**Итоговый долг:** `{format_number(total_debt)}` R$ ({bank['interest_rate']}%)\n"
                        f"**Казна банка:** `{format_number(bank['balance'])}` R$\n"
                        f"**Дата подачи:** <t:{now}:F>"
                    ),
                    color=disnake.Color.gold()
                )
                req_view = disnake.ui.View(timeout=None)
                req_view.add_item(disnake.ui.Button(
                    label="Одобрить",
                    style=disnake.ButtonStyle.success,
                    emoji="✅",
                    custom_id=f"bank_loan_appr:{req_id}"
                ))
                req_view.add_item(disnake.ui.Button(
                    label="Отклонить",
                    style=disnake.ButtonStyle.danger,
                    emoji="❌",
                    custom_id=f"bank_loan_rejt:{req_id}"
                ))
                try:
                    owner_ping = f"<@{bank['owner_id']}> " if bank["owner_id"] else ""
                    req_msg = await control_channel.send(content=f"🔔 {owner_ping}Новая заявка на кредит, требующая одобрения!", embed=req_embed, view=req_view)
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE bank_loan_requests SET message_id = ? WHERE id = ?", (req_msg.id, req_id))
                        await db.commit()
                except Exception:
                    pass

            await inter.response.edit_message(
                content=f"✅ Ваша заявка `#{req_id}` на кредит `{format_number(amount)}` R$ передана на рассмотрение руководству банка «{bank['name']}». Вы получите уведомление о решении.",
                embed=None,
                view=None
            )

        elif custom_id.startswith("bank_loan_appr:"):
            req_id = int(custom_id.split(":")[1])
            req = await get_loan_request_by_id(req_id)
            if not req:
                return await inter.response.send_message("❌ Заявка не найдена.", ephemeral=True)

            bank = await get_bank(req["bank_id"])
            is_owner = (bank["owner_id"] == inter.author.id)
            is_admin = inter.author.guild_permissions.administrator
            if not (is_owner or is_admin):
                return await inter.response.send_message("⛔ Только руководство банка или администратор может одобрить эту заявку.", ephemeral=True)

            if req["status"] != "pending":
                return await inter.response.send_message(f"ℹ️ Эта заявка уже рассмотрена (Статус: `{req['status']}`).", ephemeral=True)

            if bank["balance"] < req["amount"]:
                return await inter.response.send_message(
                    f"❌ В казне банка недостаточно средств для выдачи кредита! (В казне: `{format_number(bank['balance'])}` R$, требуется: `{format_number(req['amount'])}` R$).",
                    ephemeral=True
                )

            borrower_bank_id = req["borrower_bank_id"] if "borrower_bank_id" in req.keys() else None
            total_debt = int(req["amount"] * (1 + (bank["interest_rate"] / 100)))
            now = int(time.time())
            next_due = now + (req["days"] * 86400)

            if borrower_bank_id:
                # Межбанковский кредит: зачисляем в казну коммерческого банка
                borrower_bank = await get_bank(borrower_bank_id)
                if not borrower_bank:
                    return await inter.response.send_message("❌ Банк-заемщик не найден в базе данных.", ephemeral=True)

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE banks SET balance = balance + ? WHERE id = ?", (req["amount"], borrower_bank_id))
                    await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (req["amount"], bank["id"]))
                    await db.execute("""
                        INSERT INTO bank_loans (
                            user_id, bank_id, account_number, original_amount,
                            total_debt, remaining_debt, period_payment, created_at, next_payment_time, is_closed, borrower_bank_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """, (req["user_id"], bank["id"], req["account_number"], req["amount"], total_debt, total_debt, total_debt, now, next_due, borrower_bank_id))
                    await db.execute("""
                        UPDATE bank_loan_requests
                        SET status = 'approved', reviewed_by = ?
                        WHERE id = ?
                    """, (inter.author.id, req_id))
                    await db.commit()

                # Логируем в оба банка
                await self.bank_money_logger(
                    bank_id=bank["id"],
                    op_type="credit",
                    user1_id=req["user_id"],
                    account1=f"КАЗНА {borrower_bank['name']}",
                    amount=req["amount"],
                    extra=f"Межбанковский кредит банку «{borrower_bank['name']}» одобрен {inter.author.display_name}. Долг: {format_number(total_debt)} R$."
                )
                await self.bank_money_logger(
                    bank_id=borrower_bank["id"],
                    op_type="deposit",
                    user1_id=req["user_id"],
                    account1="КАЗНА",
                    amount=req["amount"],
                    extra=f"Поступление кредита от Нацбанка «{bank['name']}». Сумма: {format_number(req['amount'])} R$."
                )

                # Обновляем панель управления банка-заемщика
                borrower_comps = await self.build_bank_control_components(borrower_bank_id)
                borrower_ch = self.bot.get_channel(borrower_bank["control_channel_id"])
                if borrower_ch and borrower_bank["control_message_id"]:
                    try:
                        b_msg = await borrower_ch.fetch_message(borrower_bank["control_message_id"])
                        await b_msg.edit(components=borrower_comps)
                    except Exception:
                        pass
            else:
                # Обычный кредит физлицу: проверяем счет заемщика
                acc = await get_account_by_number(req["account_number"])
                if not acc or acc["is_frozen"]:
                    return await inter.response.send_message("❌ Счет заемщика заблокирован или не существует.", ephemeral=True)

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE bank_accounts SET balance = balance + ? WHERE account_number = ?", (req["amount"], req["account_number"]))
                    await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (req["amount"], bank["id"]))
                    await db.execute("""
                        INSERT INTO bank_loans (
                            user_id, bank_id, account_number, original_amount,
                            total_debt, remaining_debt, period_payment, created_at, next_payment_time, is_closed
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """, (req["user_id"], bank["id"], req["account_number"], req["amount"], total_debt, total_debt, total_debt, now, next_due))
                    await db.execute("""
                        UPDATE bank_loan_requests
                        SET status = 'approved', reviewed_by = ?
                        WHERE id = ?
                    """, (inter.author.id, req_id))
                    await db.commit()

                await self.bank_money_logger(
                    bank_id=bank["id"],
                    op_type="credit",
                    user1_id=req["user_id"],
                    account1=req["account_number"],
                    amount=req["amount"],
                    extra=f"Заявка #{req_id} одобрена {inter.author.display_name}. Долг: {format_number(total_debt)} R$ на {req['days']} дн."
                )

            # Уведомляем заемщика в ЛС
            applicant = self.bot.get_user(req["user_id"])
            if applicant:
                applicant_embed = create_embed(
                    title="🎉 Кредит одобрен!",
                    description=(
                        f"Руководство банка «{bank['name']}» одобрило вашу заявку `#{req_id}`.\n\n"
                        f"• **Сумма:** `{format_number(req['amount'])}` R$\n"
                        f"• **Зачислено в:** `{'Казну банка' if borrower_bank_id else req['account_number']}`\n"
                        f"• **Итоговый долг:** `{format_number(total_debt)}` R$\n"
                        f"• **Срок погашения:** <t:{next_due}:D> (<t:{next_due}:R>)"
                    ),
                    color=disnake.Color.green()
                )
                try:
                    await applicant.send(embed=applicant_embed)
                except disnake.Forbidden:
                    pass

            # Обновляем карточку сообщения
            updated_embed = create_embed(
                title=f"✅ Заявка на кредит #{req_id} [ОДОБРЕНА]",
                description=(
                    f"**Клиент:** <@{req['user_id']}>\n"
                    f"**Назначение:** `{'Казна банка' if borrower_bank_id else req['account_number']}`\n"
                    f"**Сумма:** `{format_number(req['amount'])}` R$\n"
                    f"**Одобрил:** {inter.author.mention}\n"
                    f"**Дата решения:** <t:{now}:F>"
                ),
                color=disnake.Color.green()
            )
            await inter.response.edit_message(content="✅ Заявка успешно одобрена, средства выданы.", embed=updated_embed, view=None)

            # Обновляем панель управления банка-кредитора
            comps = await self.build_bank_control_components(bank["id"])
            channel = self.bot.get_channel(bank["control_channel_id"])
            if channel and bank["control_message_id"]:
                try:
                    msg = await channel.fetch_message(bank["control_message_id"])
                    await msg.edit(components=comps)
                except Exception:
                    pass

        elif custom_id.startswith("bank_loan_rejt:"):
            req_id = int(custom_id.split(":")[1])
            req = await get_loan_request_by_id(req_id)
            if not req:
                return await inter.response.send_message("❌ Заявка не найдена.", ephemeral=True)

            bank = await get_bank(req["bank_id"])
            is_owner = (bank["owner_id"] == inter.author.id)
            is_admin = inter.author.guild_permissions.administrator
            if not (is_owner or is_admin):
                return await inter.response.send_message("⛔ Только руководство банка или администратор может отклонить эту заявку.", ephemeral=True)

            if req["status"] != "pending":
                return await inter.response.send_message(f"ℹ️ Эта заявка уже рассмотрена (Статус: `{req['status']}`).", ephemeral=True)

            now = int(time.time())
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE bank_loan_requests
                    SET status = 'rejected', reviewed_by = ?
                    WHERE id = ?
                """, (inter.author.id, req_id))
                await db.commit()

            # Уведомляем заемщика в ЛС
            applicant = self.bot.get_user(req["user_id"])
            if applicant:
                applicant_embed = create_embed(
                    title="❌ Заявка на кредит отклонена",
                    description=(
                        f"Ваша заявка `#{req_id}` на получение кредита на сумму `{format_number(req['amount'])}` R$ "
                        f"в банке «{bank['name']}» была отклонена руководством банка."
                    ),
                    color=disnake.Color.red()
                )
                try:
                    await applicant.send(embed=applicant_embed)
                except disnake.Forbidden:
                    pass

            updated_embed = create_embed(
                title=f"❌ Заявка на кредит #{req_id} [ОТКЛОНЕНА]",
                description=(
                    f"**Клиент:** <@{req['user_id']}>\n"
                    f"**Счет:** `{req['account_number']}`\n"
                    f"**Сумма:** `{format_number(req['amount'])}` R$\n"
                    f"**Отклонил:** {inter.author.mention}\n"
                    f"**Дата решения:** <t:{now}:F>"
                ),
                color=disnake.Color.red()
            )
            await inter.response.edit_message(content="❌ Заявка отклонена.", embed=updated_embed, view=None)

        elif custom_id.startswith("confirm_close_acc:"):
            acc_num = custom_id.split(":")[1]
            account = await self.validate_account(inter, acc_num, check_owner=True, check_frozen=False)
            if not account:
                return

            active_loans = await get_account_active_loans(acc_num)
            if active_loans:
                loan_ids = ", ".join([f"#{l['id']}" for l in active_loans])
                return await inter.response.edit_message(
                    content=f"❌ Нельзя закрыть лицевой счет `{acc_num}`, пока на нем числятся непогашенные кредиты ({loan_ids})!",
                    embed=None,
                    view=None
                )

            balance_to_refund = account["balance"]
            closed_account = await close_bank_account(acc_num)
            if not closed_account:
                return await inter.response.edit_message(content="❌ Не удалось закрыть счет.", embed=None, view=None)

            refund_text = f"\n💵 Остаток средств `{format_number(balance_to_refund)}` R$ выплачен вам наличными." if balance_to_refund > 0 else ""

            await self.bank_money_logger(
                bank_id=account["bank_id"],
                op_type="close",
                user1_id=inter.author.id,
                account1=acc_num,
                amount=balance_to_refund,
                extra=f"Счет закрыт клиентом через меню услуг. Выплачено на руки: {format_number(balance_to_refund)} R$"
            )

            await inter.response.edit_message(
                content=f"✅ Лицевой счет `{acc_num}` в банке «{account['bank_name']}» успешно закрыт и удален!{refund_text}",
                embed=None,
                view=None
            )

        elif custom_id.startswith("bank_bl_btn:add:"):
            bank_id = int(custom_id.split(":")[2])
            bank = await get_bank(bank_id)
            if not bank:
                return await inter.response.send_message("❌ Банк не найден.", ephemeral=True)

            modal = disnake.ui.Modal(
                title=f"Внесение в ЧС: {bank['name'][:25]}",
                custom_id=f"modal:bank_bl_add:{bank_id}",
                components=[
                    disnake.ui.TextInput(
                        label="ID или Пинг пользователя",
                        placeholder="Например: 123456789012345678 или @пользователь",
                        custom_id="user_id_raw",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=40
                    ),
                    disnake.ui.TextInput(
                        label="Причина внесения в ЧС",
                        placeholder="Укажите причину (невозврат кредита, махинации...)",
                        custom_id="reason",
                        style=disnake.TextInputStyle.paragraph,
                        required=True,
                        max_length=300
                    )
                ]
            )
            await inter.response.send_modal(modal=modal)

        elif custom_id.startswith("bank_acc_action:"):
            parts = custom_id.split(":")
            sub_action = parts[1]
            bank_id = int(parts[2])
            acc_num = parts[3]

            if sub_action == "toggle_freeze":
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("SELECT is_frozen FROM bank_accounts WHERE account_number = ?", (acc_num,)) as cur:
                        row = await cur.fetchone()
                        if not row:
                            return await inter.response.edit_message(content="❌ Счет не найден.", embed=None, view=None)

                    new_status = 0 if row["is_frozen"] else 1
                    await db.execute("UPDATE bank_accounts SET is_frozen = ? WHERE account_number = ?", (new_status, acc_num))
                    await db.commit()

                status_label = "заморожен 🔒" if new_status else "разблокирован 🟢"
                await inter.response.edit_message(content=f"✅ Лицевой счет `{acc_num}` теперь **{status_label}**.", embed=None, view=None)

            elif sub_action == "rename":
                acc = await get_account_by_number(acc_num)
                cur_name = (acc["account_name"] or "") if (acc and "account_name" in acc.keys()) else ""
                modal = disnake.ui.Modal(
                    title=f"Название счета {acc_num}",
                    custom_id=f"modal:bank_account_rename:{bank_id}:{acc_num}",
                    components=[
                        disnake.ui.TextInput(
                            label="Новое название (метка) счета",
                            placeholder="Например: Зарплатный счет, Депозитный, Резервный",
                            value=cur_name,
                            custom_id="account_name",
                            style=disnake.TextInputStyle.short,
                            required=True,
                            max_length=50
                        )
                    ]
                )
                await inter.response.send_modal(modal=modal)





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

        # 3. Настройка кредитных лимитов и правил выдачи
        elif action == "limits":
            min_loan = bank["min_loan_amount"] if "min_loan_amount" in bank.keys() else 100
            max_loan = bank["max_loan_amount"] if "max_loan_amount" in bank.keys() else 1000000
            threshold = bank["loan_approval_threshold"] if "loan_approval_threshold" in bank.keys() else 100000
            max_user_loans = bank["max_loans_per_user"] if "max_loans_per_user" in bank.keys() else 1

            modal = disnake.ui.Modal(
                title=f"Лимиты кредитов: {bank['name'][:25]}",
                custom_id=f"bank_modal:limits:{bank_id}",
                components=[
                    disnake.ui.TextInput(
                        label="Минимальная сумма кредита (R$)",
                        placeholder=str(min_loan),
                        value=str(min_loan),
                        custom_id="min_loan",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=10
                    ),
                    disnake.ui.TextInput(
                        label="Максимальная сумма кредита (R$)",
                        placeholder=str(max_loan),
                        value=str(max_loan),
                        custom_id="max_loan",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=10
                    ),
                    disnake.ui.TextInput(
                        label="Порог одобрения (R$) (0 = без одобрения)",
                        placeholder=str(threshold),
                        value=str(threshold),
                        custom_id="loan_threshold",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=10
                    ),
                    disnake.ui.TextInput(
                        label="Макс. активных кредитов на человека (шт)",
                        placeholder=str(max_user_loans),
                        value=str(max_user_loans),
                        custom_id="max_user_loans",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=3
                    )
                ]
            )
            await inter.response.send_modal(modal=modal)

        # 4. Заморозка / Разморозка счетов клиентов банка
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
                placeholder="Выберите лицевой счет для управления",
                options=options
            )
            view.add_item(select)
            await inter.response.send_message("Выберите счет клиента для управления (заморозка/разморозка или переименование):", view=view, ephemeral=True)

        # 5. Обновление карточки
        elif action == "refresh":
            await inter.response.defer()
            comps = await self.build_bank_control_components(bank_id)
            try:
                await inter.edit_original_message(components=comps)
            except Exception:
                # Если кнопка была нажата на самом сообщении канала
                await inter.message.edit(components=comps)

        # 6. Настройка депозитов банка
        elif action == "deposits":
            enabled_str = "да" if ("deposits_enabled" in bank.keys() and bank["deposits_enabled"]) else "нет"
            rate_val = str(bank["deposit_interest_rate"] if "deposit_interest_rate" in bank.keys() else 3.0)
            min_val = str(bank["min_deposit_amount"] if "min_deposit_amount" in bank.keys() else 1000)

            modal = disnake.ui.Modal(
                title=f"Депозиты: {bank['name'][:25]}",
                custom_id=f"bank_modal:deposits:{bank_id}",
                components=[
                    disnake.ui.TextInput(
                        label="Включить вклады? (да / нет)",
                        placeholder="да / нет",
                        value=enabled_str,
                        custom_id="deposits_status",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=10
                    ),
                    disnake.ui.TextInput(
                        label="Процентная ставка по вкладам (% в 24ч)",
                        placeholder="3.0",
                        value=rate_val,
                        custom_id="deposit_rate",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=6
                    ),
                    disnake.ui.TextInput(
                        label="Минимальная сумма вклада (R$)",
                        placeholder="1000",
                        value=min_val,
                        custom_id="min_deposit",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=12
                    )
                ]
            )
            await inter.response.send_modal(modal=modal)

        # 7. Чёрный список банка
        elif action == "blacklist":
            bl_list = await get_bank_blacklist(bank_id)
            desc_lines = [f"### 🚫 Чёрный список банка «{bank['name']}»\n"]
            if not bl_list:
                desc_lines.append("В чёрном списке банка сейчас никого нет.\nЛица из ЧС не могут брать кредиты и открывать новые счета в данном банке.")
            else:
                desc_lines.append(f"Всего в ЧС: **{len(bl_list)}** чел.\n")
                for item in bl_list[:10]:
                    desc_lines.append(f"• <@{item['user_id']}> (`{item['user_id']}`)\n  Причина: *{item['reason']}*\n  Внёс: <@{item['added_by']}> (<t:{item['created_at']}:d>)")

            embed = create_embed(
                title=f"Управление ЧС: {bank['name']}",
                description="\n".join(desc_lines),
                color=disnake.Color.dark_red()
            )
            view = disnake.ui.View(timeout=120)
            view.add_item(disnake.ui.Button(
                label="Добавить в ЧС",
                emoji="➕",
                style=disnake.ButtonStyle.danger,
                custom_id=f"bank_bl_btn:add:{bank_id}"
            ))

            if bl_list:
                bl_options = [
                    disnake.SelectOption(
                        label=f"ID: {b['user_id']}",
                        value=str(b["user_id"]),
                        description=f"Причина: {b['reason']}"[:100]
                    )
                    for b in bl_list[:25]
                ]
                select_del = disnake.ui.StringSelect(
                    custom_id=f"bank_bl_select:remove:{bank_id}",
                    placeholder="Выберите пользователя для удаления из ЧС",
                    options=bl_options
                )
                view.add_item(select_del)

            await inter.response.send_message(embed=embed, view=view, ephemeral=True)

        # 8. Запрос кредита коммерческим банком у Нацбанка
        elif action == "interbank_req":
            nat_bank = await get_national_bank()
            if not nat_bank:
                return await inter.response.send_message("❌ В государстве не назначен действующий Национальный банк.", ephemeral=True)

            min_l = nat_bank["min_loan_amount"] if "min_loan_amount" in nat_bank.keys() else 100
            max_l = nat_bank["max_loan_amount"] if "max_loan_amount" in nat_bank.keys() else 1000000
            rate = nat_bank["interest_rate"]

            modal = disnake.ui.Modal(
                title=f"Кредит в Нацбанке: {bank['name'][:20]}",
                custom_id=f"modal:interbank_req:{bank_id}:{nat_bank['id']}",
                components=[
                    disnake.ui.TextInput(
                        label=f"Сумма в казну банка ({min_l} - {max_l} R$)",
                        placeholder="Например: 500000",
                        custom_id="amount",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=12
                    ),
                    disnake.ui.TextInput(
                        label="Срок кредита в днях (от 1 до 30)",
                        placeholder="Например: 14",
                        custom_id="days",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=3
                    )
                ]
            )
            await inter.response.send_modal(modal=modal)

        # 9. Погашение межбанковского кредита перед Нацбанком
        elif action == "interbank_pay":
            active_loans = await get_bank_active_interbank_loans(bank_id)
            if not active_loans:
                return await inter.response.send_message("ℹ️ У вашего банка нет активных задолженностей перед Национальным банком.", ephemeral=True)

            options = [
                disnake.SelectOption(
                    label=f"Кредит #{l['id']} (Остаток: {format_number(l['remaining_debt'])} R$)",
                    value=str(l["id"]),
                    description=f"Кредитор: {l['lender_bank_name']} | Срок до <t:{l['next_payment_time']}:D>"[:100]
                ) for l in active_loans[:25]
            ]

            view = disnake.ui.View(timeout=60)
            select = disnake.ui.StringSelect(
                custom_id=f"select:interbank_pay_loan:{bank_id}",
                placeholder="Выберите кредит для погашения из казны банка",
                options=options
            )
            view.add_item(select)
            await inter.response.send_message("Выберите межбанковский кредит для оплаты:", view=view, ephemeral=True)


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
                    account1="КАЗНА",  # <-- Добавлен обязательный аргумент
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
                    account1="КАЗНА",  # <-- Добавлен обязательный аргумент
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

        # 3. Настройка кредитных лимитов
        elif custom_id.startswith("bank_modal:limits:"):
            bank_id = int(custom_id.split(":")[2])
            bank = await get_bank(bank_id)
            if not bank:
                return await inter.response.send_message("❌ Банк не найден.", ephemeral=True)

            raw_min = inter.text_values.get("min_loan", "").strip().replace(" ", "")
            raw_max = inter.text_values.get("max_loan", "").strip().replace(" ", "")
            raw_thresh = inter.text_values.get("loan_threshold", "").strip().replace(" ", "")
            raw_max_user = inter.text_values.get("max_user_loans", "").strip().replace(" ", "")

            if not (raw_min.isdigit() and raw_max.isdigit() and raw_thresh.isdigit() and raw_max_user.isdigit()):
                return await inter.response.send_message("❌ Все значения должны быть положительными целыми числами.", ephemeral=True)

            min_val = int(raw_min)
            max_val = int(raw_max)
            thresh_val = int(raw_thresh)
            max_user_val = int(raw_max_user)

            if min_val <= 0 or max_val <= 0 or max_user_val <= 0:
                return await inter.response.send_message("❌ Лимиты и количество кредитов должны быть больше 0.", ephemeral=True)

            if min_val > max_val:
                return await inter.response.send_message("❌ Минимальная сумма не может превышать максимальную!", ephemeral=True)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE banks
                    SET min_loan_amount = ?, max_loan_amount = ?, loan_approval_threshold = ?, max_loans_per_user = ?
                    WHERE id = ?
                """, (min_val, max_val, thresh_val, max_user_val, bank_id))
                await db.commit()

            # Обновляем карточку управления банком
            components = await self.build_bank_control_components(bank_id)
            channel = self.bot.get_channel(bank["control_channel_id"])
            if channel and bank["control_message_id"]:
                try:
                    msg = await channel.fetch_message(bank["control_message_id"])
                    await msg.edit(components=components)
                except Exception:
                    pass

            thresh_desc = f"`{format_number(thresh_val)}` R$" if thresh_val > 0 else "Отключено"
            embed = create_embed(
                title="⚙️ Параметры кредитования обновлены",
                description=(
                    f"**Банк:** «{bank['name']}»\n"
                    f"• **Минимальный кредит:** `{format_number(min_val)}` R$\n"
                    f"• **Максимальный кредит:** `{format_number(max_val)}` R$\n"
                    f"• **Порог ручного одобрения:** {thresh_desc}\n"
                    f"• **Максимум активных кредитов на человека:** `{max_user_val}` шт."
                ),
                color=disnake.Color.green()
            )
            await inter.response.send_message(embed=embed, ephemeral=True, delete_after=15)

        # 4. Запрос межбанковского кредита у Нацбанка коммерческим банком
        elif custom_id.startswith("modal:interbank_req:"):
            _, _, borrower_bank_id_str, nat_bank_id_str = custom_id.split(":")
            borrower_bank_id = int(borrower_bank_id_str)
            nat_bank_id = int(nat_bank_id_str)

            borrower_bank = await get_bank(borrower_bank_id)
            nat_bank = await get_bank(nat_bank_id)

            if not borrower_bank or not nat_bank:
                return await inter.response.send_message("❌ Банк или Национальный банк не найдены.", ephemeral=True)

            raw_amount = inter.text_values.get("amount", "").strip().replace(" ", "")
            raw_days = inter.text_values.get("days", "").strip().replace(" ", "")

            if not (raw_amount.isdigit() and raw_days.isdigit()):
                return await inter.response.send_message("❌ Сумма и срок должны быть положительными целыми числами.", ephemeral=True)

            amount = int(raw_amount)
            days = int(raw_days)

            min_loan = nat_bank["min_loan_amount"] if "min_loan_amount" in nat_bank.keys() else 100
            max_loan = nat_bank["max_loan_amount"] if "max_loan_amount" in nat_bank.keys() else 1000000
            threshold = nat_bank["loan_approval_threshold"] if "loan_approval_threshold" in nat_bank.keys() else 100000
            max_loans = nat_bank["max_loans_per_user"] if "max_loans_per_user" in nat_bank.keys() else 1

            if not (min_loan <= amount <= max_loan):
                return await inter.response.send_message(
                    f"❌ Сумма кредита Нацбанка должна быть в диапазоне от `{format_number(min_loan)}` до `{format_number(max_loan)}` R$.",
                    ephemeral=True
                )

            if not (1 <= days <= 30):
                return await inter.response.send_message("❌ Срок межбанковского кредита должен составлять от 1 до 30 дней.", ephemeral=True)

            # Проверка лимита активных займов банка у Нацбанка
            active_count = await get_bank_interbank_loans_count(borrower_bank_id, nat_bank_id)
            if active_count >= max_loans:
                return await inter.response.send_message(
                    f"❌ Банк «{borrower_bank['name']}» уже имеет максимум активных кредитов в Нацбанке (`{active_count}` из `{max_loans}`).",
                    ephemeral=True
                )

            # Проверка наличия ожидающей заявки
            if await has_pending_interbank_loan_request(borrower_bank_id, nat_bank_id):
                return await inter.response.send_message(
                    "❌ У вашего банка уже есть заявка на межбанковский кредит, ожидающая рассмотрения руководством Нацбанка.",
                    ephemeral=True
                )

            total_debt = int(amount * (1 + (nat_bank["interest_rate"] / 100)))
            needs_approval = (threshold > 0 and amount >= threshold)

            if needs_approval:
                now = int(time.time())
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute("""
                        INSERT INTO bank_loan_requests (
                            user_id, bank_id, account_number, amount, days, status, created_at, borrower_bank_id
                        )
                        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """, (inter.author.id, nat_bank_id, f"КАЗНА {borrower_bank['name']}", amount, days, now, borrower_bank_id))
                    req_id = cursor.lastrowid
                    await db.commit()

                # Отправляем карточку заявки в канал управления Нацбанком
                nat_ctrl_ch = self.bot.get_channel(nat_bank["control_channel_id"]) if nat_bank["control_channel_id"] else None
                if nat_ctrl_ch:
                    req_embed = create_embed(
                        title=f"🏛️ Межбанковская заявка на кредит #{req_id}",
                        description=(
                            f"**Банк-заемщик:** «{borrower_bank['name']}» (ID: `{borrower_bank_id}`)\n"
                            f"**Представитель:** {inter.author.mention} (`{inter.author.id}`)\n"
                            f"**Запрашиваемая сумма в казну:** `{format_number(amount)}` R$\n"
                            f"**Срок:** `{days}` дн.\n"
                            f"**Сумма к возврату:** `{format_number(total_debt)}` R$ ({nat_bank['interest_rate']}%)\n"
                            f"**Резерв Нацбанка:** `{format_number(nat_bank['balance'])}` R$\n"
                            f"**Дата подачи:** <t:{now}:F>"
                        ),
                        color=disnake.Color.gold()
                    )
                    req_view = disnake.ui.View(timeout=None)
                    req_view.add_item(disnake.ui.Button(
                        label="Одобрить",
                        style=disnake.ButtonStyle.success,
                        emoji="✅",
                        custom_id=f"bank_loan_appr:{req_id}"
                    ))
                    req_view.add_item(disnake.ui.Button(
                        label="Отклонить",
                        style=disnake.ButtonStyle.danger,
                        emoji="❌",
                        custom_id=f"bank_loan_rejt:{req_id}"
                    ))
                    try:
                        owner_ping = f"<@{nat_bank['owner_id']}> " if nat_bank["owner_id"] else ""
                        msg = await nat_ctrl_ch.send(content=f"🔔 {owner_ping}Новая заявка на межбанковский кредит!", embed=req_embed, view=req_view)
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE bank_loan_requests SET message_id = ? WHERE id = ?", (msg.id, req_id))
                            await db.commit()
                    except Exception:
                        pass

                return await inter.response.send_message(
                    f"✅ Заявка `#{req_id}` на межбанковский кредит `{format_number(amount)}` R$ направлена руководству Национального банка. Ожидайте решения.",
                    ephemeral=True
                )

            # Если порог не превышен — моментальная выдача из резерва Нацбанка в казну коммерческого банка
            if nat_bank["balance"] < amount:
                return await inter.response.send_message(
                    f"❌ В резервах Национального банка недостаточно средств для моментальной выдачи (в наличии `{format_number(nat_bank['balance'])}` R$).",
                    ephemeral=True
                )

            now = int(time.time())
            next_due = now + (days * 86400)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (amount, nat_bank_id))
                await db.execute("UPDATE banks SET balance = balance + ? WHERE id = ?", (amount, borrower_bank_id))
                await db.execute("""
                    INSERT INTO bank_loans (
                        user_id, bank_id, account_number, original_amount,
                        total_debt, remaining_debt, period_payment, created_at, next_payment_time, is_closed, borrower_bank_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """, (inter.author.id, nat_bank_id, f"КАЗНА {borrower_bank['name']}", amount, total_debt, total_debt, total_debt, now, next_due, borrower_bank_id))
                await db.commit()

            # Логируем операцию
            await self.bank_money_logger(
                bank_id=nat_bank_id,
                op_type="credit",
                user1_id=inter.author.id,
                account1=f"КАЗНА {borrower_bank['name']}",
                amount=amount,
                extra=f"Межбанковский кредит банку «{borrower_bank['name']}» (автовыдача). Долг: {format_number(total_debt)} R$ на {days} дн."
            )
            await self.bank_money_logger(
                bank_id=borrower_bank_id,
                op_type="deposit",
                user1_id=inter.author.id,
                account1="КАЗНА",
                amount=amount,
                extra=f"Кредит от Нацбанка «{nat_bank['name']}». Поступило в казну: {format_number(amount)} R$."
            )

            # Обновляем обе панели управления
            for b_id in (borrower_bank_id, nat_bank_id):
                b_data = await get_bank(b_id)
                if b_data and b_data["control_channel_id"] and b_data["control_message_id"]:
                    ch = self.bot.get_channel(b_data["control_channel_id"])
                    if ch:
                        try:
                            msg = await ch.fetch_message(b_data["control_message_id"])
                            c = await self.build_bank_control_components(b_id)
                            await msg.edit(components=c)
                        except Exception:
                            pass

            await inter.response.send_message(
                f"✅ Межбанковский кредит на сумму `{format_number(amount)}` R$ успешно выдан и зачислен в казну банка «{borrower_bank['name']}»!\n"
                f"• Долг перед Нацбанком: `{format_number(total_debt)}` R$\n"
                f"• Срок возврата: до <t:{next_due}:F> (<t:{next_due}:R>).",
                ephemeral=True
            )

        # 5. Погашение межбанковского кредита из казны
        elif custom_id.startswith("modal:interbank_pay_confirm:"):
            _, _, borrower_bank_id_str, loan_id_str = custom_id.split(":")
            borrower_bank_id = int(borrower_bank_id_str)
            loan_id = int(loan_id_str)

            loan = await get_loan_by_id(loan_id)
            if not loan or loan["borrower_bank_id"] != borrower_bank_id or loan["is_closed"]:
                return await inter.response.send_message("❌ Кредит не найден или уже закрыт.", ephemeral=True)

            borrower_bank = await get_bank(borrower_bank_id)
            lender_bank = await get_bank(loan["bank_id"])
            if not borrower_bank or not lender_bank:
                return await inter.response.send_message("❌ Данные банков не найдены.", ephemeral=True)

            raw_amount = inter.text_values.get("pay_amount", "").strip().replace(" ", "")
            if not raw_amount.isdigit():
                return await inter.response.send_message("❌ Сумма платежа должна быть положительным числом.", ephemeral=True)

            pay_amount = int(raw_amount)
            if pay_amount <= 0:
                return await inter.response.send_message("❌ Сумма должна быть больше 0.", ephemeral=True)

            pay_amount = min(pay_amount, loan["remaining_debt"])

            if borrower_bank["balance"] < pay_amount:
                return await inter.response.send_message(
                    f"❌ В казне банка недостаточно средств для погашения! В казне: `{format_number(borrower_bank['balance'])}` R$, требуется: `{format_number(pay_amount)}` R$.",
                    ephemeral=True
                )

            new_remaining = loan["remaining_debt"] - pay_amount
            is_closed = (new_remaining <= 0)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE banks SET balance = balance - ? WHERE id = ?", (pay_amount, borrower_bank_id))
                await db.execute("UPDATE banks SET balance = balance + ? WHERE id = ?", (pay_amount, lender_bank["id"]))
                await db.execute(
                    "UPDATE bank_loans SET remaining_debt = ?, is_closed = ? WHERE id = ?",
                    (new_remaining, int(is_closed), loan_id)
                )
                await db.commit()

            # Логирование
            await self.bank_money_logger(
                bank_id=borrower_bank_id,
                op_type="withdraw",
                user1_id=inter.author.id,
                account1="КАЗНА",
                amount=pay_amount,
                extra=f"Платеж по кредиту #{loan_id} в Нацбанк «{lender_bank['name']}». Остаток долга: {format_number(new_remaining)} R$"
            )
            await self.bank_money_logger(
                bank_id=lender_bank["id"],
                op_type="deposit",
                user1_id=inter.author.id,
                account1=f"КАЗНА {borrower_bank['name']}",
                amount=pay_amount,
                extra=f"Поступление оплаты по межбанковскому кредиту #{loan_id} от «{borrower_bank['name']}». Сумма: {format_number(pay_amount)} R$"
            )

            # Обновляем панели управления банков
            for b_id in (borrower_bank_id, lender_bank["id"]):
                b_data = await get_bank(b_id)
                if b_data and b_data["control_channel_id"] and b_data["control_message_id"]:
                    ch = self.bot.get_channel(b_data["control_channel_id"])
                    if ch:
                        try:
                            msg = await ch.fetch_message(b_data["control_message_id"])
                            c = await self.build_bank_control_components(b_id)
                            await msg.edit(components=c)
                        except Exception:
                            pass

            close_status = "\n🎉 **Межбанковский кредит полностью закрыт!**" if is_closed else f"\nОстаток долга: `{format_number(new_remaining)}` R$."
            await inter.response.send_message(
                f"✅ Успешно выплачено `{format_number(pay_amount)}` R$ из казны банка в счет погашения кредита #{loan_id}!{close_status}",
                ephemeral=True
            )

        # 6. Настройка параметров депозитов
        elif custom_id.startswith("bank_modal:deposits:"):
            bank_id = int(custom_id.split(":")[2])
            bank = await get_bank(bank_id)
            if not bank:
                return await inter.response.send_message("❌ Банк не найден.", ephemeral=True)

            raw_status = inter.text_values.get("deposits_status", "").strip().lower()
            raw_rate = inter.text_values.get("deposit_rate", "").strip().replace(",", ".")
            raw_min = inter.text_values.get("min_deposit", "").strip().replace(" ", "")

            is_enabled = 1 if any(w in raw_status for w in ["да", "yes", "+", "вкл", "1", "true"]) else 0

            try:
                rate_val = float(raw_rate)
                if rate_val < 0:
                    raise ValueError
            except ValueError:
                return await inter.response.send_message("❌ Некорректная процентная ставка.", ephemeral=True)

            if not raw_min.isdigit() or int(raw_min) <= 0:
                return await inter.response.send_message("❌ Минимальная сумма должна быть положительным числом.", ephemeral=True)

            min_val = int(raw_min)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE banks
                    SET deposits_enabled = ?, deposit_interest_rate = ?, min_deposit_amount = ?
                    WHERE id = ?
                """, (is_enabled, rate_val, min_val, bank_id))
                await db.commit()

            # Обновляем карточку управления
            comps = await self.build_bank_control_components(bank_id)
            ch = self.bot.get_channel(bank["control_channel_id"])
            if ch and bank["control_message_id"]:
                try:
                    msg = await ch.fetch_message(bank["control_message_id"])
                    await msg.edit(components=comps)
                except Exception:
                    pass

            status_text = "🟢 **Включены**" if is_enabled else "🔴 **Отключены**"
            embed = create_embed(
                title="📈 Настройки депозитов сохранены",
                description=(
                    f"**Банк:** «{bank['name']}»\n"
                    f"• **Статус:** {status_text}\n"
                    f"• **Процентная ставка:** `{rate_val}%` в 24 часа\n"
                    f"• **Мин. сумма вклада:** `{format_number(min_val)}` R$"
                ),
                color=disnake.Color.green()
            )
            await inter.response.send_message(embed=embed, ephemeral=True, delete_after=15)

        # 7. Добавление пользователя в ЧС банка
        elif custom_id.startswith("modal:bank_bl_add:"):
            bank_id = int(custom_id.split(":")[2])
            bank = await get_bank(bank_id)
            if not bank:
                return await inter.response.send_message("❌ Банк не найден.", ephemeral=True)

            raw_target = inter.text_values.get("user_id_raw", "").strip().replace("<@", "").replace(">", "").replace("!", "")
            reason = inter.text_values.get("reason", "").strip()

            if not raw_target.isdigit():
                return await inter.response.send_message("❌ Укажите корректный ID или пинг пользователя.", ephemeral=True)

            target_user_id = int(raw_target)
            if not reason:
                return await inter.response.send_message("❌ Указание причины внесения в ЧС обязательно!", ephemeral=True)

            # Проверяем, не является ли пользователь владельцем или админом
            if target_user_id == bank["owner_id"]:
                return await inter.response.send_message("❌ Нельзя добавить владельца банка в собственный ЧС!", ephemeral=True)

            success = await add_to_bank_blacklist(bank_id, target_user_id, reason, inter.author.id)
            if not success:
                return await inter.response.send_message("ℹ️ Этот пользователь уже находится в чёрном списке банка.", ephemeral=True)

            embed = create_embed(
                title="🚫 Пользователь добавлен в чёрный список",
                description=(
                    f"**Банк:** «{bank['name']}»\n"
                    f"**Пользователь:** <@{target_user_id}> (`{target_user_id}`)\n"
                    f"**Причина:** {reason}\n"
                    f"**Добавил:** {inter.author.mention}\n\n"
                    f"⚠️ Данный пользователь больше не сможет брать кредиты и открывать новые счета в данном банке."
                ),
                color=disnake.Color.red()
            )
            await inter.response.send_message(embed=embed, ephemeral=True, delete_after=20)

        # 8. Переименование счёта владельцем банка
        elif custom_id.startswith("modal:bank_account_rename:"):
            parts = custom_id.split(":")
            bank_id = int(parts[2])
            acc_num = parts[3]

            new_name = inter.text_values.get("account_name", "").strip()
            if not new_name:
                return await inter.response.send_message("❌ Название счёта не может быть пустым.", ephemeral=True)

            renamed = await rename_bank_account(acc_num, new_name)
            if not renamed:
                return await inter.response.send_message("❌ Счет не найден.", ephemeral=True)

            await inter.response.send_message(
                f"✅ Лицевому счету `{acc_num}` успешно присвоено новое название: **«{new_name}»**!",
                ephemeral=True,
                delete_after=15
            )


    @commands.Cog.listener("on_dropdown")
    async def handle_bank_dropdowns(self, inter: disnake.MessageInteraction):
        if inter.component.custom_id.startswith("select:interbank_pay_loan:"):
            borrower_bank_id = int(inter.component.custom_id.split(":")[2])
            loan_id = int(inter.values[0])
            loan = await get_loan_by_id(loan_id)
            if not loan or loan["borrower_bank_id"] != borrower_bank_id or loan["is_closed"]:
                return await inter.response.send_message("❌ Кредит не найден или уже погашен.", ephemeral=True)

            modal = disnake.ui.Modal(
                title=f"Оплата кредита #{loan_id}",
                custom_id=f"modal:interbank_pay_confirm:{borrower_bank_id}:{loan_id}",
                components=[
                    disnake.ui.TextInput(
                        label=f"Сумма списания (Долг: {format_number(loan['remaining_debt'])} R$)",
                        placeholder=str(loan["remaining_debt"]),
                        custom_id="pay_amount",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=12
                    )
                ]
            )
            return await inter.response.send_modal(modal=modal)

        # Выбор счета в панели банка: показать меню действий (Заморозка/разморозка или Переименование)
        if inter.component.custom_id.startswith("bank_freeze_select:"):
            bank_id = int(inter.component.custom_id.split(":")[1])
            acc_num = inter.values[0]

            acc = await get_account_by_number(acc_num)
            if not acc:
                return await inter.response.edit_message(content="❌ Счет не найден.", view=None)

            current_name = (acc["account_name"] if "account_name" in acc.keys() and acc["account_name"] else "Не задано")
            status_str = "🔒 Заморожен" if acc["is_frozen"] else "🟢 Активен"
            toggle_label = "Разблокировать" if acc["is_frozen"] else "Заморозить"
            toggle_style = disnake.ButtonStyle.success if acc["is_frozen"] else disnake.ButtonStyle.danger

            embed = create_embed(
                title=f"Управление счетом {acc_num}",
                description=(
                    f"**Банк:** {acc['bank_name']}\n"
                    f"**Владелец:** <@{acc['user_id']}>\n"
                    f"**Текущее название:** `{current_name}`\n"
                    f"**Баланс:** `{format_number(acc['balance'])}` R$\n"
                    f"**Статус:** {status_str}\n\n"
                    f"Выберите необходимое действие:"
                ),
                color=disnake.Color.blue()
            )
            view = disnake.ui.View(timeout=60)
            view.add_item(disnake.ui.Button(
                label=toggle_label,
                style=toggle_style,
                emoji="🔒",
                custom_id=f"bank_acc_action:toggle_freeze:{bank_id}:{acc_num}"
            ))
            view.add_item(disnake.ui.Button(
                label="Изменить название",
                style=disnake.ButtonStyle.primary,
                emoji="✏️",
                custom_id=f"bank_acc_action:rename:{bank_id}:{acc_num}"
            ))
            return await inter.response.edit_message(embed=embed, view=view)

        # Удаление пользователя из ЧС банка через select
        if inter.component.custom_id.startswith("bank_bl_select:remove:"):
            bank_id = int(inter.component.custom_id.split(":")[2])
            target_user_id = int(inter.values[0])
            removed = await remove_from_bank_blacklist(bank_id, target_user_id)
            if removed:
                return await inter.response.edit_message(
                    content=f"✅ Пользователь <@{target_user_id}> (`{target_user_id}`) исключён из чёрного списка банка.",
                    embed=None,
                    view=None
                )
            else:
                return await inter.response.edit_message(
                    content="❌ Не удалось найти пользователя в чёрном списке банка.",
                    embed=None,
                    view=None
                )

    @commands.Cog.listener("on_dropdown")
    async def handle_service_dropdowns(self, inter: disnake.MessageInteraction):
        if inter.component.custom_id != "service_select:close_acc":
            return

        acc_num = inter.values[0]
        account = await self.validate_account(inter, acc_num, check_owner=True, check_frozen=False)
        if not account:
            return

        active_loans = await get_account_active_loans(acc_num)
        if active_loans:
            loan_ids = ", ".join([f"#{l['id']}" for l in active_loans])
            return await inter.response.edit_message(
                content=f"❌ Нельзя закрыть лицевой счет `{acc_num}`, пока на нем числятся непогашенные кредиты ({loan_ids})!",
                embed=None,
                view=None
            )

        embed = create_embed(
            title="⚠️ Подтверждение закрытия счета",
            description=(
                f"Вы собираетесь закрыть счет `{acc_num}` в банке **«{account['bank_name']}»**.\n"
                f"• Текущий баланс: `{format_number(account['balance'])}` R$\n"
                f"• При закрытии все оставшиеся средства будут выплачены вам наличными.\n\n"
                f"Вы уверены?"
            ),
            color=disnake.Color.red()
        )
        view = disnake.ui.View(timeout=60)
        view.add_item(disnake.ui.Button(
            label="Да, закрыть счет",
            style=disnake.ButtonStyle.danger,
            emoji="🗑️",
            custom_id=f"confirm_close_acc:{acc_num}"
        ))
        view.add_item(disnake.ui.Button(
            label="Отмена",
            style=disnake.ButtonStyle.secondary,
            custom_id="confirm_acc_cancel"
        ))
        await inter.response.edit_message(content=None, embed=embed, view=view)



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