import datetime
from collections import defaultdict
from typing import TYPE_CHECKING, Optional, cast

import discord
from cachetools import TTLCache
from discord import app_commands
from discord.ext import commands
from discord.utils import MISSING
from tortoise.expressions import Q

from ballsdex.core.models import BallInstance, Player
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import Pages
from ballsdex.core.utils.sorting import SortingChoices, sort_balls
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from ballsdex.packages.bet.bet_user import BettingUser

from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import ballsdexBot


@app_commands.guild_only()
class Bet(commands.GroupCog):
    """
    Bet countryballs with other players in FootballDex Bet games.
    """

    def __init__(self, bot: "ballsdexBot"):
        self.bot = bot
        self.bets: TTLCache[int, dict[int, list]] = TTLCache(maxsize=999999, ttl=1800)

    bulk = app_commands.Group(name="bulk", description="Bulk Commands")

    def get_bet(
        self,
        interaction: discord.Interaction["ballsdexBot"] | None = None,
        *,
        channel: discord.TextChannel | None = None,
        user: discord.User | discord.Member = MISSING,
    ) -> tuple[any, BettingUser] | tuple[None, None]:
        """
        Find an ongoing bet for the given interaction.

        Parameters
        ----------
        interaction: discord.Interaction["ballsdexBot"]
            The current interaction, used for getting the guild, channel and author.

        Returns
        -------
        tuple[BetMenu, BettingUser] | tuple[None, None]
            A tuple with the `BetMenu` and `BettingUser` if found, else `None`.
        """
        guild: discord.Guild
        if interaction:
            guild = cast(discord.Guild, interaction.guild)
            channel = cast(discord.TextChannel, interaction.channel)
            user = interaction.user
        elif channel:
            guild = channel.guild
        else:
            raise TypeError("Missing interaction or channel")

        if guild.id not in self.bets:
            self.bets[guild.id] = defaultdict(list)
        if channel.id not in self.bets[guild.id]:
            return (None, None)
        to_remove: list[BetMenu] = []
        for bet in self.bets[guild.id][channel.id]:
            if (
                bet.current_view.is_finished()
                or bet.bettor1.cancelled
                or bet.bettor2.cancelled
            ):
                # remove what was supposed to have been removed
                to_remove.append(bet)
                continue
            try:
                bettor = bet._get_bettor(user)
            except RuntimeError:
                continue
            else:
                break
        else:
            for bet in to_remove:
                self.bets[guild.id][channel.id].remove(bet)
            return (None, None)

        for bet in to_remove:
            self.bets[guild.id][channel.id].remove(bet)
        return (bet, bettor)

    @app_commands.command()
    async def begin(self, interaction: discord.Interaction["ballsdexBot"], user: discord.User):
        """
        Begin a FootballDex Bet with the chosen user.

        Parameters
        ----------
        user: discord.User
            The user you want to bet with
        """
        if user.bot:
            await interaction.response.send_message("You cannot bet with bots.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot bet with yourself.", ephemeral=True
            )
            return
        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)
        blocked = await player1.is_blocked(player2)
        if blocked:
            await interaction.response.send_message(
                "You cannot begin a bet with a user that you have blocked.", ephemeral=True
            )
            return
        blocked2 = await player2.is_blocked(player1)
        if blocked2:
            await interaction.response.send_message(
                "You cannot begin a bet with a user that has blocked you.", ephemeral=True
            )
            return

        bet1, bettor1 = self.get_bet(interaction)
        bet2, bettor2 = self.get_bet(channel=interaction.channel, user=user)  # type: ignore
        if bet1 or bettor1:
            await interaction.response.send_message(
                "You already have an ongoing bet.", ephemeral=True
            )
            return
        if bet2 or bettor2:
            await interaction.response.send_message(
                "The user you are trying to bet with is already in a bet.", ephemeral=True
            )
            return

        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)
        if player2.discord_id in self.bot.blacklist:
            await interaction.response.send_message(
                "You cannot bet with a blacklisted user.", ephemeral=True
            )
            return

        # Import BetMenu dynamically to avoid circular import
        from ballsdex.packages.bet.menu import BetMenu
        menu = BetMenu(
            self, interaction, BettingUser(interaction.user, player1), BettingUser(user, player2)
        )
        self.bets[interaction.guild.id][interaction.channel.id].append(menu)  # type: ignore
        await menu.start()
        await interaction.response.send_message("FootballDex Bet started!", ephemeral=True)

    @app_commands.command(extras={"trade": TradeCommandType.PICK})
    async def add(
        self,
        interaction: discord.Interaction["ballsdexBot"],
        countryball: BallInstanceTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        packs: int | None = None,
    ):
        """
        Add a countryball and/or packs to the ongoing bet.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to add to your proposal
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        packs: int
            Number of packs to add to your proposal
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message("You do not have an ongoing bet.", ephemeral=True)
            return
        if bettor.locked:
            await interaction.response.send_message(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return

        # Handle pack addition
        if packs is not None:
            if packs <= 0:
                await interaction.response.send_message(
                    "Pack amount must be greater than 0.", ephemeral=True
                )
                return

            # Check if user has enough packs in wallet
            current_pack_bet = getattr(bettor, 'pack_amount', 0)
            
            # Access the wallet_balance from the packs module
            try:
                # Import from the packs module where wallet_balance is defined
                from ballsdex.packages.packs.cog import wallet_balance
                available_packs = wallet_balance[interaction.user.id]
            except (ImportError, ModuleNotFoundError):
                # Fallback: try to find wallet_balance in loaded modules
                import sys
                available_packs = 0
                for module_name, module in sys.modules.items():
                    if 'packs' in module_name and hasattr(module, 'wallet_balance'):
                        wallet_balance = getattr(module, 'wallet_balance')
                        available_packs = wallet_balance[interaction.user.id]
                        break
            
            if current_pack_bet + packs > available_packs:
                await interaction.response.send_message(
                    f"You don't have enough packs! You have {available_packs} packs, "
                    f"already betting {current_pack_bet}, and trying to add {packs} more.",
                    ephemeral=True
                )
                return

            await interaction.response.defer(thinking=True, ephemeral=True)
            if not hasattr(bettor, 'pack_amount'):
                bettor.pack_amount = 0
            bettor.pack_amount += packs
            await interaction.followup.send(
                f"Added {packs} packs to your proposal. Total packs: {bettor.pack_amount}",
                ephemeral=True
            )
            return

        # Handle ball addition
        if not countryball:
            await interaction.response.send_message(
                f"Please specify a {settings.collectible_name} or pack amount to add.", ephemeral=True
            )
            return
        if not countryball.is_tradeable:
            await interaction.response.send_message(
                f"You cannot bet this {settings.collectible_name}.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if countryball.favorite:
            view = ConfirmChoiceView(
                interaction,
                accept_message=f"{settings.collectible_name.title()} added.",
                cancel_message="This request has been cancelled.",
            )
            await interaction.followup.send(
                f"This {settings.collectible_name} is a favorite, "
                "are you sure you want to bet it?",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.value:
                return

        if countryball in bettor.proposal:
            await interaction.followup.send(
                f"You already have this {settings.collectible_name} in your proposal.",
                ephemeral=True,
            )
            return
        if await countryball.is_locked():
            await interaction.followup.send(
                f"This {settings.collectible_name} is currently in an active bet or donation, "
                "please try again later.",
                ephemeral=True,
            )
            return

        await countryball.lock_for_trade()
        bettor.proposal.append(countryball)
        await interaction.followup.send(
            f"{countryball.countryball.country} added.", ephemeral=True
        )

    @bulk.command(name="add", extras={"trade": TradeCommandType.PICK})
    async def bulk_add(
        self,
        interaction: discord.Interaction["ballsdexBot"],
        countryball: BallEnabledTransform | None = None,
        sort: SortingChoices | None = None,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Bulk add countryballs to the ongoing bet, with parameters to aid with searching.

        Parameters
        ----------
        countryball: Ball
            The countryball you would like to filter the results to
        sort: SortingChoices
            Choose how countryballs are sorted. Can be used to show duplicates.
        special: Special
            Filter the results to a special event
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.followup.send("You do not have an ongoing bet.", ephemeral=True)
            return
        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return
        query = BallInstance.filter(player__discord_id=interaction.user.id)
        if countryball:
            query = query.filter(ball=countryball)
        if special:
            query = query.filter(special=special)
        if sort:
            query = sort_balls(sort, query)
        balls = await query
        if not balls:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} found.", ephemeral=True
            )
            return
        balls = [x for x in balls if x.is_tradeable]

        from ballsdex.packages.bet.menu import BulkAddView
        view = BulkAddView(interaction, balls, self)  # type: ignore
        await view.start(
            content=f"Select the {settings.plural_collectible_name} you want to add "
            "to your bet proposal, note that the display will wipe on pagination however "
            f"the selected {settings.plural_collectible_name} will remain."
        )

    @app_commands.command(extras={"trade": TradeCommandType.REMOVE})
    async def remove(
        self,
        interaction: discord.Interaction["ballsdexBot"],
        countryball: BallInstanceTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        packs: int | None = None,
    ):
        """
        Remove a countryball and/or packs from what you proposed in the ongoing bet.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to remove from your proposal
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        packs: int
            Number of packs to remove from your proposal
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return
        if bettor.locked:
            await interaction.response.send_message(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return

        # Handle pack removal
        if packs is not None:
            if packs <= 0:
                await interaction.response.send_message(
                    "Pack amount must be greater than 0.", ephemeral=True
                )
                return

            current_pack_bet = getattr(bettor, 'pack_amount', 0)
            if packs > current_pack_bet:
                await interaction.response.send_message(
                    f"You only have {current_pack_bet} packs in your proposal.", ephemeral=True
                )
                return

            bettor.pack_amount -= packs
            await interaction.response.send_message(
                f"Removed {packs} packs from your proposal. Remaining packs: {bettor.pack_amount}",
                ephemeral=True
            )
            return

        # Handle ball removal
        if not countryball:
            await interaction.response.send_message(
                f"Please specify a {settings.collectible_name} or pack amount to remove.", ephemeral=True
            )
            return

        if countryball not in bettor.proposal:
            await interaction.response.send_message(
                f"That {settings.collectible_name} is not in your proposal.", ephemeral=True
            )
            return
        bettor.proposal.remove(countryball)
        await interaction.response.send_message(
            f"{countryball.countryball.country} removed.", ephemeral=True
        )
        await countryball.unlock()

    @app_commands.command()
    async def cancel(self, interaction: discord.Interaction["ballsdexBot"]):
        """
        Cancel the ongoing bet.
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        view = ConfirmChoiceView(
            interaction,
            accept_message="Cancelling the bet...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to cancel this bet?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        await bet.user_cancel(bettor)
        await interaction.followup.send("Bet has been cancelled.", ephemeral=True)

    @app_commands.command()
    async def view(
        self,
        interaction: discord.Interaction["ballsdexBot"],
        user: discord.User | None = None,
        sort: SortingChoices = SortingChoices.alphabetic,
        reverse: bool = False,
    ):
        """
        View a user's balls proposed in the current bet.

        Parameters
        ----------
        user: discord.User
            The user whose proposal you want to view. If not specified, shows your own.
        sort: SortingChoices
            How to sort the balls
        reverse: bool
            Reverse the sorting order
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return

        if user is None:
            target_bettor = bettor
        else:
            try:
                target_bettor = bet._get_bettor(user)
            except RuntimeError:
                await interaction.response.send_message(
                    "That user is not part of this bet.", ephemeral=True
                )
                return

        target_pack_amount = getattr(target_bettor, 'pack_amount', 0)
        if not target_bettor.proposal and target_pack_amount == 0:
            await interaction.response.send_message(
                f"{target_bettor.user.name} has no items in their proposal.", ephemeral=True
            )
            return

        sorted_balls = sort_balls(sort, target_bettor.proposal, reverse=reverse)
        
        # Use the CountryballsViewer if available, otherwise create a simple embed
        try:
            from ballsdex.packages.balls.countryballs_paginator import CountryballsViewer
            paginator = CountryballsViewer(
                interaction,
                sorted_balls,
                title=f"{target_bettor.user.name}'s bet proposal"
            )
            await paginator.start(ephemeral=True)
        except ImportError:
            # Fallback to simple embed if CountryballsViewer is not available
            embed = discord.Embed(
                title=f"{target_bettor.user.name}'s bet proposal",
                color=discord.Colour.gold()
            )
            
            description_lines = []
            for i, ball in enumerate(sorted_balls[:20], 1):  # Limit to 20 for embed limits
                description_lines.append(f"{i}. {ball.description(short=True, include_emoji=True, bot=self.bot)}")
            
            if len(sorted_balls) > 20:
                description_lines.append(f"... and {len(sorted_balls) - 20} more")
            
            embed.description = "\n".join(description_lines) if description_lines else "*Empty*"
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command()
    async def info(self, interaction: discord.Interaction["ballsdexBot"]):
        """
        Get information about the current bet.
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="FootballDex Bet Information",
            color=discord.Colour.gold()
        )
        
        bettor1_packs = getattr(bet.bettor1, 'pack_amount', 0)
        bettor2_packs = getattr(bet.bettor2, 'pack_amount', 0)
        
        embed.add_field(
            name="Bettor 1",
            value=f"{bet.bettor1.user.mention}\n"
                  f"Balls: {len(bet.bettor1.proposal)}\n"
                  f"Packs: {bettor1_packs}\n"
                  f"Status: {'🔒 Locked' if bet.bettor1.locked else '✏️ Editing'}"
                  f"{'✅ Accepted' if bet.bettor1.accepted else ''}",
            inline=True
        )
        
        embed.add_field(
            name="Bettor 2", 
            value=f"{bet.bettor2.user.mention}\n"
                  f"Balls: {len(bet.bettor2.proposal)}\n"
                  f"Packs: {bettor2_packs}\n"
                  f"Status: {'🔒 Locked' if bet.bettor2.locked else '✏️ Editing'}"
                  f"{'✅ Accepted' if bet.bettor2.accepted else ''}",
            inline=True
        )
        
        total_balls = len(bet.bettor1.proposal) + len(bet.bettor2.proposal)
        total_packs = bettor1_packs + bettor2_packs
        embed.add_field(
            name="Total Items at Stake",
            value=f"{total_balls} balls" + (f", {total_packs} packs" if total_packs > 0 else "") + "\n*Winner takes all!*",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)