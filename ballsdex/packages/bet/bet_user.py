from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ballsdex.core.models import BlacklistedID

if TYPE_CHECKING:
    import discord

    from ballsdex.core.bot import ballsdexBot
    from ballsdex.core.models import BallInstance, Player


@dataclass(slots=True)
class BettingUser:
    user: "discord.User | discord.Member"
    player: "Player"
    proposal: list["BallInstance"] = field(default_factory=list)
    pack_amount: int = field(default=0)  # Number of packs being bet
    locked: bool = False
    cancelled: bool = False
    accepted: bool = False
    blacklisted: bool | None = None
    won: bool = False  # Track if user won the FootballDex Bet

    def clear_proposal(self):
        """Clear all items from the proposal."""
        self.proposal.clear()
        self.pack_amount = 0

    @property
    def total_value(self) -> str:
        """Get a string representation of the total bet value."""
        parts = []
        
        if self.proposal:
            ball_count = len(self.proposal)
            parts.append(f"{ball_count} ball{'s' if ball_count != 1 else ''}")
        
        if self.pack_amount > 0:
            parts.append(f"{self.pack_amount} pack{'s' if self.pack_amount != 1 else ''}")
        
        if not parts:
            return "Nothing"
        
        return " + ".join(parts)

    def has_items(self) -> bool:
        """Check if the user has any items in their proposal."""
        return len(self.proposal) > 0 or self.pack_amount > 0

    @classmethod
    async def from_player(
        cls, player: "Player", bot: "ballsdexBot", is_admin: bool = False
    ):
        user = await bot.fetch_user(player.discord_id)
        blacklisted = (
            await BlacklistedID.exists(discord_id=player.discord_id) if is_admin else None
        )
        return cls(user, player, blacklisted=blacklisted)