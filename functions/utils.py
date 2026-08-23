import disnake
from typing import Callable, List, Awaitable


def create_embed(
    title: str, 
    description: str = None, 
    color: disnake.Color = disnake.Color.blue(),
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