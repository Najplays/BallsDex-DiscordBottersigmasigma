from typing import TYPE_CHECKING

from ballsdex.packages.profiles.cog import Profiles

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


async def setup(bot: "BallsDexBot"):
    await bot.add_cog(Profiles(bot))
