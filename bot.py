import os
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set in .env — cannot start bot.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("bot")


class Bot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("cogs.general")
        await self.load_extension("cogs.qotd")
        # Sync commands globally once — works for all guilds including future ones
        synced = await self.tree.sync()
        logger.info("Synced %d global slash commands.", len(synced))


intents = discord.Intents.default()

bot = Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    logger.info("Logged in as %s (%s) in %d guilds.", bot.user.name, bot.user.id, len(bot.guilds))
    # Ensure no stale guild-specific slash commands cause duplicates with global commands
    for guild in bot.guilds:
        try:
            guild_cmds = await bot.tree.fetch_commands(guild=guild)
            if guild_cmds:
                logger.info("Found %d stale guild-specific command(s) in %s (%s). Clearing to prevent duplicates...", len(guild_cmds), guild.name, guild.id)
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
                logger.info("Cleared stale guild-specific commands from %s.", guild.name)
        except Exception as err:
            logger.debug("Could not check guild commands for %s: %s", guild.name, err)


bot.run(TOKEN)
