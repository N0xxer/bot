from typing import Optional
import disnake
from disnake.ext import commands

from functions.db_helpers import (
    get_user_info,
    update_user_info,
    create_company_db,
    get_company_by_id,
    delete_company_db,
    is_user_registered
)
from functions.utils import create_embed, format_number

# ID родительской категории для создания форумов
COMPANY_CATEGORY_ID = 1526549976078094488
MEDIA_CATEGORY_ID = 1527075652098986095

# ID канала логирования действий
LOG_CHANNEL_ID = 1537894851331362948

# Стоимость создания предприятий
COMPANY_PRICES = {
    "Бизнес": 25000,
    "НКО": 500,
    "СМИ": 10000
}



from functions.db_helpers import (
    get_user_info,
    update_user_info,
    get_company_by_id,
    update_company_balance,
    get_all_companies
)


# Функция автокомплита для выбора целевой компании
async def company_autocomplete(inter: disnake.ApplicationCommandInteraction, user_input: str):
    companies = await get_all_companies()
    choices = []
    for comp in companies:
        display_label = f"{comp['name']} (ID: {comp['id']}) | {comp['company_type']}"
        if user_input.lower() in comp["name"].lower() or user_input in str(comp["id"]):
            choices.append(disnake.OptionChoice(name=display_label[:100], value=comp["id"]))
    return choices[:25]



class CompaniesCog(commands.Cog):
    """Модуль управления коммерческими и общественными организациями."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="company",
        description="Система управления компаниями"
    )
    async def company_group(self, inter: disnake.ApplicationCommandInteraction):
        if not inter.author.guild_permissions.administrator:
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description="Создавать компании может только администрация сервера.",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)
        pass



    # ==========================================
    #             СОЗДАНИЕ\ УДАЛЕНИЕ КОМПАНИИ
    # ==========================================



    @company_group.sub_command(
        name="create",
        description="Зарегистрировать новую компанию (Только для Администрации)"
    )
    @commands.has_permissions(administrator=True)
    async def company_create(
        self,
        inter: disnake.ApplicationCommandInteraction,
        пользователь: disnake.Member = commands.Param(description="Владелец компании"),
        название: str = commands.Param(description="Название компании", max_length=100),
        company_type: str = commands.Param(
            name="тип_компании",
            description="Тип создаваемой организации",
            choices=["Бизнес", "НКО", "СМИ"]
        )
    ):
        await inter.response.defer(ephemeral=True)

        # 1. Проверка регистрации владельца
        if not await is_user_registered(пользователь.id):
            embed = create_embed(
                title="❌ Ошибка",
                description=f"Пользователь {пользователь.mention} не зарегистрирован как персонаж!",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        # 2. Проверка баланса игрока
        cost = COMPANY_PRICES.get(company_type, 25000)
        owner_balance = await get_user_info(пользователь.id, "balance") or 0

        if owner_balance < cost:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"У {пользователь.mention} недостаточно средств для создания компании типа **{company_type}**.\n\n"
                    f"• Требуется: R$ `{format_number(cost)}`\n"
                    f"• На балансе: R$ `{format_number(owner_balance)}`"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        # 3. Получение родительской категории
        if company_type == "СМИ":
            category = inter.guild.get_channel(MEDIA_CATEGORY_ID)
        else:
            category = inter.guild.get_channel(COMPANY_CATEGORY_ID)

        if not isinstance(category, disnake.CategoryChannel):
            embed = create_embed(
                title="❌ Ошибка конфигурации",
                description="Указанная категория для компаний не найдена на сервере.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        # 4. Настройка прав доступа и создание канала
        # Для СМИ — текстовый канал (create_text_channel), для Бизнеса/НКО — форум (create_forum_channel)
        if company_type == "СМИ":
            overwrites = {
                inter.guild.default_role: disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                    create_public_threads=False,
                    create_private_threads=False
                ),
                пользователь: disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    attach_files=True,
                    embed_links=True
                )
            }
            try:
                channel = await category.create_text_channel(
                    name=f"📰・{название}",
                    overwrites=overwrites,
                    topic=f"СМИ «{название}». Владелец: {пользователь.display_name} | Тип: {company_type}"
                )
            except Exception as e:
                embed = create_embed(
                    title="❌ Ошибка при создании канала",
                    description=f"Не удалось создать текстовый канал СМИ. Проверьте права бота.\n`{e}`",
                    color=disnake.Color.red()
                )
                return await inter.edit_original_message(embed=embed)
        else:
            overwrites = {
                inter.guild.default_role: disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    create_public_threads=False,
                    create_private_threads=False,
                    send_messages_in_threads=False,
                    send_messages=False
                ),
                пользователь: disnake.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    create_public_threads=True,
                    send_messages_in_threads=True,
                    send_messages=True
                )
            }
            try:
                channel = await category.create_forum_channel(
                    name=f"❮🏢❯・{название}",
                    overwrites=overwrites,
                    topic=f"Организация «{название}». Владелец: {пользователь.display_name} | Тип: {company_type}"
                )
            except Exception as e:
                embed = create_embed(
                    title="❌ Ошибка при создании форума",
                    description=f"Не удалось создать форум компании. Проверьте права бота.\n`{e}`",
                    color=disnake.Color.red()
                )
                return await inter.edit_original_message(embed=embed)

        # Перемещаем созданный канал в самый низ категории
        try:
            bottom_pos = max((c.position for c in category.channels), default=0) + 1
            await channel.edit(position=bottom_pos)
        except Exception:
            pass

        # 5. Списание средств и сохранение в БД
        new_balance = owner_balance - cost
        await update_user_info(пользователь.id, "balance", str(new_balance))
        company_id = await create_company_db(название, пользователь.id, channel.id, company_type)

        channel_type_label = "Текстовый канал СМИ" if company_type == "СМИ" else "Форум компании"

        # 6. Ответ администратору
        admin_embed = create_embed(
            title="🏢 Компания успешно создана",
            description=(
                f"**ID компании:** `{company_id}`\n"
                f"**Название:** `{название}`\n"
                f"**Тип:** `{company_type}`\n"
                f"**Владелец:** {пользователь.mention} (`{пользователь.id}`)\n"
                f"**Канал:** {channel.mention} ({channel_type_label})\n"
                f"**Списано средств:** R$ `-{format_number(cost)}`\n"
                f"**Остаток у владельца:** R$ `{format_number(new_balance)}`"
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=admin_embed)

        # 7. Оповещение владельцу в ЛС
        dm_embed = create_embed(
            title="🏛️ Регистрация предприятия",
            description=(
                f"Поздравляем! Ваша организация **«{название}»** была успешно зарегистрирована.\n\n"
                f"• **ID компании:** `{company_id}`\n"
                f"• **Тип:** `{company_type}`\n"
                f"• **Стоимость:** R$ `{format_number(cost)}`\n"
                f"• **Канал:** {channel.mention}\n\n"
                f"*Вам предоставлены полные права на публикацию материалов в данном канале.*"
            ),
            color=disnake.Color.gold()
        )
        try:
            await пользователь.send(embed=dm_embed)
        except disnake.Forbidden:
            pass



    @company_group.sub_command(
        name="dissolution",
        description="Ликвидировать компанию (Владелец или Администратор)"
    )
    async def company_dissolution(
        self,
        inter: disnake.ApplicationCommandInteraction,
        компания: int = commands.Param(name="компания", description="Выберите компанию для ликвидации", autocomplete=company_autocomplete)
    ):
        await inter.response.defer(ephemeral=True)

        company = await get_company_by_id(компания)
        if not company:
            embed = create_embed(
                title="❌ Ошибка",
                description=f"Компания с ID `{компания}` не найдена!",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (company["owner_id"] == inter.author.id)

        if not (is_admin or is_owner):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description="Вы не являетесь владельцем данной компании или администратором сервера.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        # Подтверждение действия кнопками
        confirm_embed = create_embed(
            title="⚠️ Подтверждение ликвидации",
            description=(
                f"Вы действительно хотите ликвидировать компанию **«{company['name']}»** (ID: `{компания}`)?\n\n"
                f"• Баланс компании: R$ `{format_number(company['balance'])}`\n"
                f"• Все средства будут переведены на баланс владельца <@{company['owner_id']}>.\n"
                f"• Канал-форум будет безвозвратно удален."
            ),
            color=disnake.Color.orange()
        )

        view = disnake.ui.View(timeout=120)
        view.add_item(disnake.ui.Button(label="Подтвердить", style=disnake.ButtonStyle.danger, custom_id=f"comp_dissolve:confirm:{компания}"))
        view.add_item(disnake.ui.Button(label="Отмена", style=disnake.ButtonStyle.secondary, custom_id=f"comp_dissolve:cancel:{компания}"))

        await inter.edit_original_message(embed=confirm_embed, view=view)


    @commands.Cog.listener("on_button_click")
    async def handle_dissolution_buttons(self, inter: disnake.MessageInteraction):
        custom_id = inter.component.custom_id
        if not custom_id.startswith("comp_dissolve:"):
            return

        action = custom_id.split(":")[1]
        company_id = int(custom_id.split(":")[2])

        if action == "cancel":
            embed = create_embed(
                title="Ликвидация отменена",
                description="Процедура роспуска компании была отменена.",
                color=disnake.Color.light_gray()
            )
            return await inter.response.edit_message(embed=embed, view=None)

        if action == "confirm":
            await inter.response.defer(ephemeral=True)

            company = await get_company_by_id(company_id)
            if not company:
                embed = create_embed(
                    title="Ошибка",
                    description="Данная компания уже была удалена.",
                    color=disnake.Color.red()
                )
                return await inter.edit_original_message(embed=embed, view=None)

            is_admin = inter.author.guild_permissions.administrator
            is_owner = (company["owner_id"] == inter.author.id)

            if not (is_admin or is_owner):
                embed = create_embed(
                    title="⛔ Ошибка прав",
                    description="У вас нет прав для подтверждения этого действия.",
                    color=disnake.Color.red()
                )
                return await inter.edit_original_message(embed=embed)

            owner_id = company["owner_id"]
            comp_balance = company["balance"]
            comp_name = company["name"]
            forum_id = company["forum_id"]

            # 1. Перевод остатка средств на баланс владельца (даже при отрицательном значении)
            owner_balance = await get_user_info(owner_id, "balance") or 0
            new_owner_balance = owner_balance + comp_balance
            await update_user_info(owner_id, "balance", str(new_owner_balance))

            # 2. Удаление канала-форума
            if forum_id and inter.guild:
                channel = inter.guild.get_channel(forum_id)
                if channel:
                    try:
                        await channel.delete(reason=f"Ликвидация компании {comp_name} (Инициатор: {inter.author})")
                    except Exception:
                        pass

            # 3. Удаление из базы данных
            await delete_company_db(company_id)

            # 4. Сообщение об успешной ликвидации
            success_embed = create_embed(
                title="🏢 Компания ликвидирована",
                description=(
                    f"Организация **«{comp_name}»** (ID: `{company_id}`) была успешно распущена.\n\n"
                    f"• Баланс компании (`{format_number(comp_balance)}` R$) переведен владельцу <@{owner_id}>.\n"
                    f"• Новый баланс владельца: `{format_number(new_owner_balance)}` R$\n"
                    f"• Форум удален."
                ),
                color=disnake.Color.red()
            )
            await inter.edit_original_message(embed=success_embed, view=None)

            # 5. Отправка в канал логов
            log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                log_embed = create_embed(
                    title="📋 Лог: Роспуск компании",
                    description=(
                        f"**Инициатор:** {inter.author.mention} (`{inter.author.id}`)\n"
                        f"**Компания:** {comp_name} (ID: `{company_id}`)\n"
                        f"**Владелец:** <@{owner_id}> (`{owner_id}`)\n"
                        f"**Остаток средств компании:** `{format_number(comp_balance)}` R$\n"
                        f"**Итоговый баланс владельца:** `{format_number(new_owner_balance)}` R$"
                    ),
                    color=disnake.Color.dark_red(),
                    footer_text=f"ID компании: {company_id}"
                )
                await log_channel.send(embed=log_embed)



    # ==========================================
    #           УПРАВЛЕНИЕ КОМПАНИЕЙ
    # ==========================================



    @company_group.sub_command(
        name="info",
        description="Посмотреть информацию об организации"
    )
    async def company_info(
        self,
        inter: disnake.ApplicationCommandInteraction,
        компания: int = commands.Param(name="компания", description="Выберите компанию для просмотра", autocomplete=company_autocomplete)
    ):
        """Просмотр досье и баланса компании."""
        company = await get_company_by_id(компания)
        if not company:
            embed = create_embed(
                title="❌ Ошибка",
                description=f"Компания с ID `{компания}` не найдена!",
                color=disnake.Color.red()
            )
            return await inter.response.send_message(embed=embed, ephemeral=True)

        owner_id = company["owner_id"]
        owner_member = inter.guild.get_member(owner_id)
        owner_mention = owner_member.mention if owner_member else f"<@{owner_id}>"

        forum_id = company.get("forum_id")
        forum_channel = inter.guild.get_channel(forum_id) if forum_id else None
        forum_mention = forum_channel.mention if forum_channel else "Отсутствует / Удален"

        company_balance = company.get("balance", 0)
        company_type = company.get("company_type", "Бизнес")

        desc = (
            f"🏛️ **Название:** `{company['name']}`\n"
            f"🆔 **Регистрационный номер:** `{компания}`\n"
            f"🏷️ **Тип предприятия:** `{company_type}`\n"
            f"👑 **Владелец:** {owner_mention} (`{owner_id}`)\n"
            f"💳 **Казна компании:** `{format_number(company_balance)}` R$\n"
            f"📢 **Форум-канал:** {forum_mention}"
        )

        embed = create_embed(
            title=f"🏢 Досье организации: {company['name']}",
            description=desc,
            color=disnake.Color.blurple(),
            footer_text=f"ID компании: {компания}"
        )

        if owner_member:
            embed.set_thumbnail(url=owner_member.display_avatar.url)

        await inter.response.send_message(embed=embed)



    @company_group.sub_command_group(
        name="money",
        description="Финансовые операции предприятия"
    )
    async def company_money_group(self, inter: disnake.ApplicationCommandInteraction):
        pass

    # --- ПЕРЕВОД МЕЖДУ КОМПАНИЯМИ (PAY) ---
    @company_money_group.sub_command(
        name="pay",
        description="Перевести средства со счета своей компании на счет другой"
    )
    async def money_pay(
        self,
        inter: disnake.ApplicationCommandInteraction,
        отправитель: int = commands.Param(name="отправитель", description="Выберите вашу компанию (отправитель)", autocomplete=company_autocomplete),
        получатель: int = commands.Param(name="получатель", description="Выберите компанию-получателя", autocomplete=company_autocomplete),
        сумма: int = commands.Param(description="Сумма перевода", min_value=1),
        цель: str = commands.Param(default="Коммерческий перевод", description="Назначение платежа")
    ):
        await inter.response.defer(ephemeral=True)

        if отправитель == получатель:
            embed = create_embed(
                title="❌ Ошибка операции",
                description="Нельзя перевести средства на счет той же самой компании.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        sender_comp = await get_company_by_id(отправитель)
        target_comp = await get_company_by_id(получатель)

        if not sender_comp:
            embed = create_embed(title="❌ Ошибка", description=f"Компания-отправитель (ID: `{отправитель}`) не найдена!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        if not target_comp:
            embed = create_embed(title="❌ Ошибка", description=f"Компания-получатель (ID: `{получатель}`) не найдена!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (sender_comp["owner_id"] == inter.author.id)
        if not (is_admin or is_owner):
            embed = create_embed(
                title="⛔ Доступ запрещен",
                description=f"Вы не являетесь владельцем компании **«{sender_comp['name']}»**.",
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        if sender_comp["balance"] < сумма:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"На счете компании **«{sender_comp['name']}»** недостаточно средств!\n\n"
                    f"• Баланс казны: `{format_number(sender_comp['balance'])}` R$\n"
                    f"• Требуется: `{format_number(сумма)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        new_sender_bal = sender_comp["balance"] - сумма
        new_target_bal = target_comp["balance"] + сумма

        await update_company_balance(отправитель, new_sender_bal)
        await update_company_balance(получатель, new_target_bal)

        desc = (
            f"Операция межбанковского перевода успешно выполнена.\n\n"
            f"📤 **Отправитель:** «{sender_comp['name']}» (ID: `{отправитель}`)\n"
            f"📥 **Получатель:** «{target_comp['name']}» (ID: `{получатель}`)\n"
            f"💵 **Сумма транзакции:** `{format_number(сумма)}` R$\n"
            f"💬 **Назначение:** {цель}\n\n"
            f"💼 **Остаток на счете компании:** `{format_number(new_sender_bal)}` R$"
        )

        embed = create_embed(
            title="💳 Межбанковский перевод",
            description=desc,
            color=disnake.Color.green(),
            footer_text=f"Инициатор: {inter.author.display_name}"
        )
        await inter.edit_original_message(embed=embed)

        target_owner = self.bot.get_user(target_comp["owner_id"])
        if target_owner and target_comp["owner_id"] != inter.author.id:
            dm_embed = create_embed(
                title="💰 Поступление на счет компании",
                description=(
                    f"На баланс вашей организации **«{target_comp['name']}»** поступили средства!\n\n"
                    f"• **Сумма:** `+{format_number(сумма)}` R$\n"
                    f"• **Отправитель:** «{sender_comp['name']}»\n"
                    f"• **Назначение:** {цель}\n"
                    f"• **Текущий баланс предприятия:** `{format_number(new_target_bal)}` R$"
                ),
                color=disnake.Color.blue()
            )
            try:
                await target_owner.send(embed=dm_embed)
            except disnake.Forbidden:
                pass



    # --- РАСХОД СРЕДСТВ НА НУЖДЫ КОМПАНИИ (USE) ---
    @company_money_group.sub_command(
        name="use",
        description="Списать средства со счета компании на расходы предприятия"
    )
    async def money_use(
        self,
        inter: disnake.ApplicationCommandInteraction,
        компания: int = commands.Param(name="компания", description="Выберите компанию", autocomplete=company_autocomplete),
        сумма: int = commands.Param(description="Сумма списания", min_value=1),
        цель: str = commands.Param(description="Цель/причина расходов")
    ):
        await inter.response.defer(ephemeral=True)

        company = await get_company_by_id(компания)
        if not company:
            embed = create_embed(title="❌ Ошибка", description=f"Компания с ID `{компания}` не найдена!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (company["owner_id"] == inter.author.id)
        if not (is_admin or is_owner):
            embed = create_embed(title="⛔ Доступ запрещен", description="Вы не являетесь владельцем данной компании.", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        if company["balance"] < сумма:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"В казне компании **«{company['name']}»** недостаточно средств!\n\n"
                    f"• Баланс: `{format_number(company['balance'])}` R$\n"
                    f"• Требуется списать: `{format_number(сумма)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        new_bal = company["balance"] - сумма
        await update_company_balance(компания, new_bal)

        embed = create_embed(
            title="🧾 Корпоративный расход",
            description=(
                f"Средства успешно списаны с казны предприятия.\n\n"
                f"🏢 **Компания:** «{company['name']}» (ID: `{компания}`)\n"
                f"💸 **Списано:** `-{format_number(сумма)}` R$\n"
                f"📋 **Цель расхода:** {цель}\n\n"
                f"💳 **Остаток в казне:** `{format_number(new_bal)}` R$"
            ),
            color=disnake.Color.orange(),
            footer_text=f"Инициатор: {inter.author.display_name}"
        )
        await inter.edit_original_message(embed=embed)



    # --- ПОПОЛНЕНИЕ БАЛАНСА (DEPOSIT) ---
    @company_money_group.sub_command(
        name="deposit",
        description="Внести средства с личного баланса в казну компании"
    )
    async def money_deposit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        компания: int = commands.Param(name="компания", description="Выберите компанию для пополнения", autocomplete=company_autocomplete),
        сумма: int = commands.Param(description="Сумма пополнения", min_value=1)
    ):
        await inter.response.defer(ephemeral=True)

        company = await get_company_by_id(компания)
        if not company:
            embed = create_embed(title="❌ Ошибка", description=f"Компания с ID `{компания}` не найдена!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (company["owner_id"] == inter.author.id)
        if not (is_admin or is_owner):
            embed = create_embed(title="⛔ Доступ запрещен", description="Вы не являетесь владельцем компании или администратором.", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        user_balance = await get_user_info(inter.author.id, "balance") or 0
        if user_balance < сумма:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"На вашем личном счете недостаточно средств!\n\n"
                    f"• Ваш баланс: `{format_number(user_balance)}` R$\n"
                    f"• Требуется: `{format_number(сумма)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        new_user_bal = user_balance - сумма
        new_comp_bal = company["balance"] + сумма

        await update_user_info(inter.author.id, "balance", str(new_user_bal))
        await update_company_balance(компания, new_comp_bal)

        embed = create_embed(
            title="📥 Пополнение казны предприятия",
            description=(
                f"Вы успешно пополнили баланс компании **«{company['name']}»**!\n\n"
                f"💵 **Внесено:** `+{format_number(сумма)}` R$\n"
                f"🏢 **Баланс компании:** `{format_number(new_comp_bal)}` R$\n"
                f"👤 **Ваш личный остаток:** `{format_number(new_user_bal)}` R$"
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)



    # --- ВЫВОД СРЕДСТВ (WITHDRAW) ---
    @company_money_group.sub_command(
        name="withdraw",
        description="Вывести средства из казны компании на личный баланс"
    )
    async def money_withdraw(
        self,
        inter: disnake.ApplicationCommandInteraction,
        компания: int = commands.Param(name="компания", description="Выберите компанию для вывода", autocomplete=company_autocomplete),
        сумма: int = commands.Param(description="Сумма вывода", min_value=1)
    ):
        await inter.response.defer(ephemeral=True)

        company = await get_company_by_id(компания)
        if not company:
            embed = create_embed(title="❌ Ошибка", description=f"Компания с ID `{компания}` не найдена!", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        is_admin = inter.author.guild_permissions.administrator
        is_owner = (company["owner_id"] == inter.author.id)
        if not (is_admin or is_owner):
            embed = create_embed(title="⛔ Доступ запрещен", description="Вы не являетесь владельцем компании или администратором.", color=disnake.Color.red())
            return await inter.edit_original_message(embed=embed)

        if company["balance"] < сумма:
            embed = create_embed(
                title="❌ Недостаточно средств",
                description=(
                    f"В казне компании **«{company['name']}»** недостаточно средств!\n\n"
                    f"• Баланс компании: `{format_number(company['balance'])}` R$\n"
                    f"• Запрошено к выводу: `{format_number(сумма)}` R$"
                ),
                color=disnake.Color.red()
            )
            return await inter.edit_original_message(embed=embed)

        user_balance = await get_user_info(inter.author.id, "balance") or 0
        new_comp_bal = company["balance"] - сумма
        new_user_bal = user_balance + сумма

        await update_company_balance(компания, new_comp_bal)
        await update_user_info(inter.author.id, "balance", str(new_user_bal))

        embed = create_embed(
            title="📤 Вывод дивидендов / средств",
            description=(
                f"Средства успешно выведены на ваш личный счет!\n\n"
                f"🏢 **Компания:** «{company['name']}»\n"
                f"💵 **Выведено:** `{format_number(сумма)}` R$\n"
                f"💼 **Остаток в казне:** `{format_number(new_comp_bal)}` R$\n"
                f"👤 **Ваш текущий баланс:** `{format_number(new_user_bal)}` R$"
            ),
            color=disnake.Color.green()
        )
        await inter.edit_original_message(embed=embed)





def setup(bot: commands.Bot):
    bot.add_cog(CompaniesCog(bot))