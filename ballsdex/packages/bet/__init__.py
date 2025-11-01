from typing import TYPE_CHECKING

from ballsdex.packages.bet.cog import Bet

if TYPE_CHECKING:
    from ballsdex.core.bot import ballsdexBot


async def setup(bot: "ballsdexBot"):
    await bot.add_cog(Bet(bot))
