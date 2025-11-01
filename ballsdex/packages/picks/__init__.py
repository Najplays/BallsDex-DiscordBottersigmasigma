from typing import TYPE_CHECKING

from ballsdex.packages.picks.cog import Picks

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


async def setup(bot: "BallsDexBot"):
    await bot.add_cog(Picks(bot))