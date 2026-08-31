import json
import os
from typing import Optional
import disnake
from disnake.ext import commands

from functions.db_helpers import (
    get_user_info,
    update_user_info,
    get_region_by_name,
    update_region_balance,
    update_region_owner,
    init_regions_from_config,
    is_user_registered,
    get_company_by_id,
    update_company_balance,
    get_all_companies,
    get_all_registered_users

)

from functions.utils import create_embed, format_number

CONFIG_PATH = "configs/regions_config.json"
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        REGIONS_LIST = json.load(f).get("regions", [])
else:
    REGIONS_LIST = []


async def region_autocomplete(inter: disnake.ApplicationCommandInteraction, user_input: str):
    """Автокомплит регионов."""
    choices = [reg for reg in REGIONS_LIST if user_input.lower() in reg.lower()]
    return [disnake.OptionChoice(name=reg, value=reg) for reg in choices[:25]]



async def unified_recipient_autocomplete(inter: disnake.ApplicationCommandInteraction, user_input: str):
    """Общий автокомплит: Регионы, Компании и Граждане."""
    query = user_input.lower()
    choices = []

    # 1. Регионы
    for reg in REGIONS_LIST:
        if query in reg.lower():
            choices.append(disnake.OptionChoice(name=f"📍 [Регион] {reg}", value=f"region:{reg}"))

    # 2. Компании
    companies = await get_all_companies()
    for comp in companies:
        name = comp["name"]
        comp_id = comp["id"]
        if query in name.lower() or query in str(comp_id):
            label = f"🏢 [Компания] {name} (ID: {comp_id})"[:100]
            choices.append(disnake.OptionChoice(name=label, value=f"company:{comp_id}"))

    # 3. Граждане (пользователи)
    users = await get_all_registered_users()
    for u in users:
        fio = u["FIO"]
        uid = u["user_id"]
        if query in fio.lower() or query in str(uid):
            label = f"👤 [Гражданин] {fio} ({uid})"[:100]
            choices.append(disnake.OptionChoice(name=label, value=f"user:{uid}"))

    return choices[:25]



class RegionsCog(commands.Cog):
    """Модуль управления регионами и их бюджетом."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        # Создает записи под каждый регион в БД при запуске бота
        await init_regions_from_config()

    @commands.slash_command(
        name="region",
        description="Система управления регионами"
    )
    async def region_group(self, inter: disnake.ApplicationCommandInteraction):
        pass



    # ==========================================
    #            НАЗНАЧЕНИЕ ЛИДЕРА
    # ==========================================



    @region_group.sub_command(
        name="leader",
        description="Назначить или снять руководителя (губернатора) региона (Только для Администрации)"
    )
    @commands.has_permissions(administrator=True)
    async def region_leader(
        self,
        inter: disnake.ApplicationCommandInteraction,
        region_name: str = commands.Param(name="region", description="Выберите регион", autocomplete=region_autocomplete),
        member: Optional[disnake.Member] = commands.Param(
            default=None,
            description="Пользователь для назначения (оставьте пустым, чтобы снять лидера)"
        )
    ):
        await inter.response.defer(ephemeral=True)

        if not inter.author.guild_permissions.administrator:
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description="Назначать и снимать руководителей регионов может только администрация.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        region = await get_region_by_name(region_name)
        if not region:
            embed = create_embed(
                title="❌ Ошибка",
                description=f"Регион **«{region_name}»** не найден в базе данных!",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        # Вариант 1: Снятие руководителя (лидер не указан)
        if member is None:
            await update_region_owner(region_name, None)

            embed = create_embed(
                title="🏛️ Руководитель региона снят",
                description=(
                    f"**Регион:** «{region_name}»\n"
                    f"**Текущий статус:** `Без руководителя`\n"
                    f"**Приказ издал:** {inter.author.mention}"
                ),
                color=disnake.Color.orange()
            )
            return await inter.edit_original_message(embed=embed)

        # Вариант 2: Назначение нового руководителя
        if not await is_user_registered(member.id):
            embed = create_embed(
                title="❌ Ошибка регистрации",
                description=f"Пользователь {member.mention} не зарегистрирован в государственной системе!",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        await update_region_owner(region_name, member.id)

        embed = create_embed(
            title="🏛️ Назначение руководителя региона",
            description=(
                f"**Регион:** «{region_name}»\n"
                f"**Новый руководитель:** {member.mention} (`{member.id}`)\n"
                f"**Приказ издал:** {inter.author.mention}"
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)

        # Уведомление игроку в ЛС
        dm_embed = create_embed(
            title="📜 Назначение на государственную должность",
            description=(
                f"Вы были официально назначены руководителем региона **«{region_name}»**!\n\n"
                f"Вам открыт доступ к управлению казной и бюджетом региона через команду `/region money`."
            ),
            color=disnake.Color.gold()
        )
        try:
            await member.send(embed=dm_embed)
        except disnake.Forbidden:
            pass



    # ==========================================
    #             ИНФОРМАЦИЯ О РЕГИОНЕ
    # ==========================================



    @region_group.sub_command(
        name="info",
        description="Посмотреть информацию о регионе"
    )
    async def region_info(
        self,
        inter: disnake.ApplicationCommandInteraction,
        region_name: str = commands.Param(name="region", description="Выберите регион", autocomplete=region_autocomplete)
    ):
        await inter.response.defer()

        region = await get_region_by_name(region_name)
        if not region:
            embed = create_embed(
                title="❌ Ошибка",
                description=f"Регион **«{region_name}»** не найден!",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        owner_id = region.get("owner")
        owner_mention = f"<@{owner_id}> (`{owner_id}`)" if owner_id else "Не назначен (Федеральное управление)"

        desc = (
            f"🏛️ **Регион:** `{region['name']}`\n"
            f"👑 **Руководитель:** {owner_mention}\n"
            f"💳 **Бюджет региона:** `{format_number(region.get('balance', 0))}` R$"
        )

        embed = create_embed(
            title=f"📍 Паспорт региона: {region['name']}",
            description=desc,
            color=disnake.Color.blue()
        )
        await inter.edit_original_message(embed=embed)



    # ==========================================
    #       ФИНАНСОВЫЕ ОПЕРАЦИИ (/region money)
    # ==========================================



    @region_group.sub_command_group(
        name="money",
        description="Управление бюджетом региона"
    )
    async def region_money_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    # --- ПЕРЕВОД МЕЖДУ РЕГИОНАМИ (PAY) ---
    @region_money_group.sub_command(
        name="pay",
        description="Перевести средства из бюджета региона"
    )
    async def money_pay(
        self,
        inter: disnake.ApplicationCommandInteraction,
        from_region: str = commands.Param(name="from_region", description="Ваш регион (отправитель)", autocomplete=region_autocomplete),
        recipient: str = commands.Param(name="recipient", description="Выберите получателя (Регион, Компания или Гражданин)", autocomplete=unified_recipient_autocomplete),
        amount: int = commands.Param(description="Сумма перевода", min_value=1),
        comment: str = commands.Param(default="Государственный перевод / Субсидия", description="Назначение платежа")
    ):
        await inter.response.defer(ephemeral=True)

        # 1. Проверка региона-отправителя
        sender_reg = await get_region_by_name(from_region)
        if not sender_reg:
            embed = create_embed(title="❌ Ошибка", description=f"Регион **«{from_region}»** не найден!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (sender_reg.get("owner") == inter.author.id)
        if not (is_admin or is_owner):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description=f"Вы не являетесь руководителем региона **«{from_region}»**.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        if sender_reg.get("balance", 0) < amount:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"В бюджете региона **«{from_region}»** недостаточно средств!\n\n"
                    f"• Доступно: `{format_number(sender_reg.get('balance', 0))}` R$\n"
                    f"• Требуется: `{format_number(amount)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        # 2. Разбор получателя
        if ":" not in recipient:
            embed = create_embed(title="❌ Ошибка", description="Выберите получателя из выпадающего списка автокомплита!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        r_type, r_id = recipient.split(":", 1)
        recipient_display = ""
        dm_target_user = None
        dm_text = ""

        # --- РЕГИОН ---
        if r_type == "region":
            target_reg_name = r_id
            if from_region == target_reg_name:
                embed = create_embed(title="❌ Ошибка", description="Нельзя перевести средства в тот же самый регион.", color=disnake.Color.red())
                return await inter.edit_original_message(embed=embed)

            target_reg = await get_region_by_name(target_reg_name)
            if not target_reg:
                embed = create_embed(title="❌ Ошибка", description=f"Регион **«{target_reg_name}»** не найден!", color=disnake.Color.red())
                return await inter.edit_original_message(embed=embed)

            new_target_bal = target_reg.get("balance", 0) + amount
            await update_region_balance(target_reg_name, new_target_bal)
            recipient_display = f"Регион «{target_reg_name}»"

            if target_reg.get("owner"):
                dm_target_user = self.bot.get_user(target_reg["owner"])
                dm_text = f"В бюджет вашего региона **«{target_reg_name}»** поступили средства из бюджета «{from_region}»."

        # --- КОМПАНИЯ ---
        elif r_type == "company":
            comp_id = int(r_id)
            target_comp = await get_company_by_id(comp_id)
            if not target_comp:
                embed = create_embed(title="❌ Ошибка", description="Указанная компания не найдена!", color=disnake.Color.red())
                return await inter.edit_original_message(embed=embed)

            new_comp_bal = target_comp.get("balance", 0) + amount
            await update_company_balance(comp_id, new_comp_bal)
            recipient_display = f"Компания «{target_comp['name']}» (ID: `{comp_id}`)"

            dm_target_user = self.bot.get_user(target_comp["owner_id"])
            dm_text = f"На счёт вашей организации **«{target_comp['name']}»** поступили средства от региона «{from_region}»."

        # --- ГРАЖДАНИН ---
        elif r_type == "user":
            target_user_id = int(r_id)
            target_user_fio = await get_user_info(target_user_id, "FIO")
            if not target_user_fio:
                embed = create_embed(title="❌ Ошибка", description="Гражданин не зарегистрирован в системе.", color=disnake.Color.red())
                return await inter.edit_original_message(embed=embed)

            user_bal = await get_user_info(target_user_id, "balance") or 0
            new_user_bal = user_bal + amount
            await update_user_info(target_user_id, "balance", str(new_user_bal))
            recipient_display = f"Гражданин {target_user_fio} (<@{target_user_id}>)"

            dm_target_user = self.bot.get_user(target_user_id)
            dm_text = f"Вам была выплачена государственная субсидия/перевод из бюджета региона **«{from_region}»**."

        # 3. Списание средств со счёта отправителя
        new_sender_bal = sender_reg.get("balance", 0) - amount
        await update_region_balance(from_region, new_sender_bal)

        # 4. Ответ инициатору
        desc = (
            f"Финансовый перевод успешно проведён.\n\n"
            f"🏛️ **Регион-плательщик:** «{from_region}»\n"
            f"📥 **Получатель:** {recipient_display}\n"
            f"💵 **Сумма перевода:** `{format_number(amount)}` R$\n"
            f"💬 **Назначение:** {comment}\n\n"
            f"💼 **Остаток бюджета «{from_region}»:** `{format_number(new_sender_bal)}` R$"
        )

        embed = create_embed(
            title="💳 Финансовый перевод региона",
            description=desc,
            color=disnake.Color.green(),
            footer_text=f"Инициатор: {inter.author.display_name}"
        )
        await inter.edit_original_message(embed=embed)

        # 5. Отправка в ЛС получателю
        if dm_target_user and dm_target_user.id != inter.author.id:
            dm_embed = create_embed(
                title="💰 Поступление средств от региона",
                description=(
                    f"{dm_text}\n\n"
                    f"• **Сумма:** `+{format_number(amount)}` R$\n"
                    f"• **Назначение:** {comment}\n"
                    f"• **Регион-отправитель:** «{from_region}»"
                ),
                color=disnake.Color.blue()
            )
            try:
                await dm_target_user.send(embed=dm_embed)
            except disnake.Forbidden:
                pass



    # --- РАСХОД БЮДЖЕТА РЕГИОНА (USE) ---
    @region_money_group.sub_command(
        name="use",
        description="Списать средства из бюджета региона на нужды субъекта"
    )
    async def money_use(
        self,
        inter: disnake.ApplicationCommandInteraction,
        region_name: str = commands.Param(name="region", description="Выберите регион", autocomplete=region_autocomplete),
        amount: int = commands.Param(description="Сумма списания", min_value=1),
        purpose: str = commands.Param(description="Статья расходов / проект")
    ):
        await inter.response.defer(ephemeral=True)

        region = await get_region_by_name(region_name)
        if not region:
            embed = create_embed(title="❌ Ошибка", description=f"Регион **«{region_name}»** не найден!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (region.get("owner") == inter.author.id)

        if not (is_admin or is_owner):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description=f"Вы не являетесь руководителем региона **«{region_name}»**.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        if region.get("balance", 0) < amount:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"В бюджете региона **«{region_name}»** недостаточно средств!\n\n"
                    f"• Доступно: `{format_number(region.get('balance', 0))}` R$\n"
                    f"• Требуется: `{format_number(amount)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        new_bal = region.get("balance", 0) - amount
        await update_region_balance(region_name, new_bal)

        embed = create_embed(
            title="🧾 Расход регионального бюджета",
            description=(
                f"Средства успешно списаны со счёта региона.\n\n"
                f"📍 **Регион:** «{region_name}»\n"
                f"💸 **Сумма:** `-{format_number(amount)}` R$\n"
                f"📋 **Статья:** {purpose}\n\n"
                f"🏛️ **Остаток бюджета:** `{format_number(new_bal)}` R$"
            ),
            color=disnake.Color.orange(),
            footer_text=f"Инициатор: {inter.author.display_name}"
        )
        await inter.edit_original_message(embed=embed)



    # --- ПОПОЛНЕНИЕ БЮДЖЕТА РЕГИОНА (DEPOSIT) ---
    @region_money_group.sub_command(
        name="deposit",
        description="Внести средства с личного баланса в бюджет региона"
    )
    async def money_deposit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        region_name: str = commands.Param(name="region", description="Выберите регион", autocomplete=region_autocomplete),
        amount: int = commands.Param(description="Сумма пополнения", min_value=1)
    ):
        await inter.response.defer(ephemeral=True)

        region = await get_region_by_name(region_name)
        if not region:
            embed = create_embed(title="❌ Ошибка", description=f"Регион **«{region_name}»** не найден!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (region.get("owner") == inter.author.id)

        if not (is_admin or is_owner):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description=f"Вы не являетесь руководителем региона **«{region_name}»**.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        user_balance = await get_user_info(inter.author.id, "balance") or 0
        if user_balance < amount:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"На вашем личном счете недостаточно средств!\n\n"
                    f"• Баланс: `{format_number(user_balance)}` R$\n"
                    f"• Требуется: `{format_number(amount)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        new_user_bal = user_balance - amount
        new_reg_bal = region.get("balance", 0) + amount

        await update_user_info(inter.author.id, "balance", str(new_user_bal))
        await update_region_balance(region_name, new_reg_bal)

        embed = create_embed(
            title="📥 Пополнение регионального бюджета",
            description=(
                f"Бюджет региона **«{region_name}»** успешно пополнен!\n\n"
                f"💵 **Внесено:** `+{format_number(amount)}` R$\n"
                f"🏛️ **Казна региона:** `{format_number(new_reg_bal)}` R$\n"
                f"👤 **Ваш личный баланс:** `{format_number(new_user_bal)}` R$"
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)



    # --- ВЫВОД ИЗ БЮДЖЕТА РЕГИОНА (WITHDRAW) ---
    @region_money_group.sub_command(
        name="withdraw",
        description="Вывести средства из бюджета региона на личный баланс"
    )
    async def money_withdraw(
        self,
        inter: disnake.ApplicationCommandInteraction,
        region_name: str = commands.Param(name="region", description="Выберите регион", autocomplete=region_autocomplete),
        amount: int = commands.Param(description="Сумма вывода", min_value=1)
    ):
        await inter.response.defer(ephemeral=True)

        region = await get_region_by_name(region_name)
        if not region:
            embed = create_embed(title="❌ Ошибка", description=f"Регион **«{region_name}»** не найден!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (region.get("owner") == inter.author.id)

        if not (is_admin or is_owner):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description=f"Вы не являетесь руководителем региона **«{region_name}»**.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        if region.get("balance", 0) < amount:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"В бюджете региона **«{region_name}»** недостаточно средств!\n\n"
                    f"• Доступно: `{format_number(region.get('balance', 0))}` R$\n"
                    f"• Требуется: `{format_number(amount)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        user_balance = await get_user_info(inter.author.id, "balance") or 0
        new_reg_bal = region.get("balance", 0) - amount
        new_user_bal = user_balance + amount

        await update_region_balance(region_name, new_reg_bal)
        await update_user_info(inter.author.id, "balance", str(new_user_bal))

        embed = create_embed(
            title="📤 Вывод средств из бюджета региона",
            description=(
                f"Средства успешно переведены на ваш личный счёт!\n\n"
                f"📍 **Регион:** «{region_name}»\n"
                f"💵 **Выведено:** `{format_number(amount)}` R$\n"
                f"🏛️ **Остаток в казне:** `{format_number(new_reg_bal)}` R$\n"
                f"👤 **Ваш баланс:** `{format_number(new_user_bal)}` R$"
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)




def setup(bot: commands.Bot):
    bot.add_cog(RegionsCog(bot))