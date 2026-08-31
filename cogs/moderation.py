import disnake
from disnake.ext import commands

from functions.db_helpers import is_user_registered
from functions.utils import create_embed

# Список команд, доступных без обязательной регистрации в БД
ALLOWED_WITHOUT_REG = {"send_reg_panel", "unregister", "function", "clear"}


class ModerationCog(commands.Cog):
    """Модуль модерации и управления сервером."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member):
        """Слушатель ивента: вызывается при входе нового участника."""
        print(f"Участник {member} присоединился к серверу {member.guild.name}.")

    async def bot_slash_command_check(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        """Глобальная проверка регистрации для всех слэш-команд."""
        # 1. Защита от вызова в ЛС (где нет guild_permissions)
        if not inter.guild or inter.author.guild_permissions.administrator:
            return True

        # 2. Проверяем как базовое имя, так и полное имя с подкомандами
        cmd_name = inter.application_command.name
        full_cmd_name = getattr(inter.application_command, "qualified_name", cmd_name)

        if cmd_name in ALLOWED_WITHOUT_REG or full_cmd_name in ALLOWED_WITHOUT_REG:
            return True

        # 3. Проверка в БД
        registered = await is_user_registered(inter.author.id)
        if not registered:
            embed = create_embed(
                title="⛔ Доступ ограничен",
                description=(
                    "Вы не зарегистрированы в государственной системе!\n\n"
                    "Пройдите регистрацию в канале подачи анкет, "
                    "чтобы получить доступ к экономике, голосованию и взаимодействию с ботом."
                ),
                color=disnake.Color.red()
            )
            # Отвечаем, только если интеракция еще не была подтверждена/отвечена
            if not inter.response.is_done():
                await inter.response.send_message(embed=embed, ephemeral=True)
            return False

        return True

    @commands.slash_command(
        name="clear",
        description="Удалить указанное количество сообщений"
    )
    @commands.has_permissions(manage_messages=True)
    async def clear(
        self, 
        inter: disnake.ApplicationCommandInteraction, 
        amount: int
    ):
        """Слэш-команда для очистки чата."""
        await inter.response.defer(ephemeral=True)
        
        deleted = await inter.channel.purge(limit=amount)
        
        embed = create_embed(
            title="Очистка чата",
            description=f"Успешно удалено сообщений: `{len(deleted)}`",
            color=disnake.Color.orange()
        )
        
        await inter.edit_original_message(embed=embed)


def setup(bot: commands.Bot):
    """Обязательная функция для регистрации кога в disnake."""
    bot.add_cog(ModerationCog(bot))