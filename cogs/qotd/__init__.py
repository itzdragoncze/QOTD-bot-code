from .cog import Qotd

async def setup(bot):
    await bot.add_cog(Qotd(bot))
