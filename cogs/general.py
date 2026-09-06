import discord
from discord.ext import commands
from translations import t, resolve_user_locale, DEFAULT_LANGUAGE


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def get_server_lang(self, guild_id: int | None) -> str:
        if not guild_id:
            return DEFAULT_LANGUAGE
        qotd = self.bot.get_cog("Qotd")
        if qotd and hasattr(qotd, "get_server_language"):
            return qotd.get_server_language(guild_id)
        return DEFAULT_LANGUAGE

    def get_user_lang(self, interaction: discord.Interaction) -> str:
        user_lang = resolve_user_locale(getattr(interaction, "locale", None))
        if user_lang:
            return user_lang
        return self.get_server_lang(interaction.guild_id)

    @discord.app_commands.command(name="ping", description="Replies with bot latency / Zobrazí odezvu bota")
    async def ping(self, interaction: discord.Interaction):
        lang = self.get_user_lang(interaction)
        latency = self.bot.latency
        if latency is None or latency == float("inf") or latency != latency:
            ms = "N/A"
        else:
            ms = f"{round(latency * 1000)}ms"
        await interaction.response.send_message(t(lang, "ping_response", latency=ms))

    @discord.app_commands.command(name="hello", description="Says hello / Pozdraví uživatele")
    async def hello(self, interaction: discord.Interaction):
        lang = self.get_user_lang(interaction)
        await interaction.response.send_message(
            t(lang, "hello_response", mention=interaction.user.mention)
        )


async def setup(bot):
    await bot.add_cog(General(bot))
