import disnake
from typing import Callable, List, Awaitable, Union

from functions.db_helpers import is_user_registered
from disnake.ext import commands


MAIN_COLOR = disnake.Color(0x00A5FE)


def create_embed(
    title: str, 
    description: str = None, 
    color: disnake.Color = MAIN_COLOR,
    footer_text: str = None
) -> disnake.Embed:
    """Универсальная функция для создания красивых Embed-сообщений."""
    embed = disnake.Embed(
        title=title,
        description=description,
        color=color
    )
    if footer_text:
        embed.set_footer(text=footer_text)
    return embed


def format_number(number: int) -> str:
    """Форматирует числа с разделителями (например, 1000000 -> 1 000 000)."""
    return f"{number:,}".replace(",", " ")

class UniversalModal(disnake.ui.Modal):
    def __init__(
        self,
        title: str,
        components: List[disnake.ui.TextInput],
        callback_func: Callable[[disnake.ModalInteraction, dict], Awaitable[None]],
        custom_id: str = "universal_modal"
    ):
        """
        :param title: Заголовок модального окна.
        :param components: Список полей ввода (TextInput).
        :param callback_func: Асинхронная функция, которая обработает результат.
        :param custom_id: Уникальный ID окна (опционально).
        """
        self.callback_func = callback_func
        
        super().__init__(
            title=title, 
            components=components, 
            custom_id=custom_id
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await self.callback_func(inter, inter.text_values)



async def ensure_user_registered(ctx_or_inter: Union[disnake.Interaction, commands.Context], user_id: int) -> bool:
    """
    Проверяет регистрацию. Если не зарегистрирован — отправляет отказ и возвращает False.
    Работает как со слэш-командами (Interaction), так и с префиксными (Context).
    """
    if not await is_user_registered(user_id):
        embed = create_embed(
            title="⚠️ Требуется регистрация",
            description=(
                "Вы не зарегистрированы в государственной базе данных!\n\n"
                "Для использования этой команды необходимо пройти регистрацию персонажа."
            ),
            color=disnake.Color.orange()
        )

        if isinstance(ctx_or_inter, disnake.Interaction):
            if ctx_or_inter.response.is_done():
                await ctx_or_inter.edit_original_message(embed=embed)
            else:
                await ctx_or_inter.response.send_message(embed=embed, ephemeral=True)
        else:
            # Для префиксных команд (commands.Context)
            await ctx_or_inter.reply(embed=embed)

        return False

    return True



async def notification_send(
    ctx_or_inter: Union[disnake.Interaction, commands.Context],
    title: str,
    description: str,
    color: disnake.Color = disnake.Color.blue(),
    footer_text: str = None
):
    log_channel_id = 1520135463699087521
    log_channel = ctx_or_inter.guild.get_channel(log_channel_id)
    embed = create_embed(title=title, description=description, color=color, footer_text=footer_text)
    await log_channel.send(embed=embed)



async def ensure_admin(inter: disnake.ApplicationCommandInteraction) -> bool:
    """
    Проверяет наличие прав администратора у пользователя.
    Если прав нет — отправляет отказ и возвращает False.
    """
    if not inter.author.guild_permissions.administrator:
        embed = create_embed(
            title="⛔ Доступ запрещен",
            description="У вас недостаточно полномочий для выполнения этой команды.",
            color=disnake.Color.red()
        )
        if inter.response.is_done():
            await inter.edit_original_message(embed=embed)
        else:
            await inter.response.send_message(embed=embed, ephemeral=True)
        return False
    return True