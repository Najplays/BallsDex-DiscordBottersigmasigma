from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Set, cast

import discord
from discord.ui import Button, View, button
from discord.utils import format_dt, utcnow

from ballsdex.core.models import BallInstance, Player, TradeCooldownPolicy
from ballsdex.core.utils import menus
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import Pages
from ballsdex.packages.balls.countryballs_paginator import CountryballsViewer
from ballsdex.packages.bet.bet_user import BettingUser
from ballsdex.packages.bet.display import fill_bet_embed_fields
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import ballsdexBot
    from ballsdex.packages.bet.cog import Bet as BetCog

log = logging.getLogger("ballsdex.packages.bet.menu")


class InvalidBetOperation(Exception):
    pass


class BetView(View):
    def __init__(self, bet: BetMenu):
        super().__init__(timeout=60 * 30)
        self.bet = bet

    async def interaction_check(self, interaction: discord.Interaction["ballsdexBot"], /) -> bool:
        try:
            self.bet._get_bettor(interaction.user)
        except RuntimeError:
            await interaction.response.send_message(
                "You are not allowed to interact with this bet.", ephemeral=True
            )
            return False
        else:
            return True

    @button(label="Lock proposal", emoji="\N{LOCK}", style=discord.ButtonStyle.primary)
    async def lock(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        bettor = self.bet._get_bettor(interaction.user)
        if bettor.locked:
            await interaction.response.send_message(
                "You have already locked your proposal!", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.bet.lock(bettor)
        if self.bet.bettor1.locked and self.bet.bettor2.locked:
            await interaction.followup.send(
                "Your proposal has been locked. Now confirm again to end the bet.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Your proposal has been locked. "
                "You can wait for the other user to lock their proposal.",
                ephemeral=True,
            )

    @button(label="Reset", emoji="\N{DASH SYMBOL}", style=discord.ButtonStyle.secondary)
    async def clear(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        bettor = self.bet._get_bettor(interaction.user)
        await interaction.response.defer(thinking=True, ephemeral=True)

        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return

        view = ConfirmChoiceView(
            interaction,
            accept_message="Clearing your proposal...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to clear your proposal?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return

        for countryball in bettor.proposal:
            await countryball.unlock()

        bettor.clear_proposal()
        await interaction.followup.send("Proposal cleared.", ephemeral=True)

    @button(
        label="Cancel bet",
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
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

        await self.bet.user_cancel(self.bet._get_bettor(interaction.user))
        await interaction.followup.send("Bet has been cancelled.", ephemeral=True)


class ConfirmView(View):
    def __init__(self, bet: BetMenu):
        super().__init__(timeout=90)
        self.bet = bet
        self.cooldown_duration = timedelta(seconds=10)

    async def interaction_check(self, interaction: discord.Interaction["ballsdexBot"], /) -> bool:
        try:
            self.bet._get_bettor(interaction.user)
        except RuntimeError:
            await interaction.response.send_message(
                "You are not allowed to interact with this bet.", ephemeral=True
            )
            return False
        else:
            return True

    @discord.ui.button(
        style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}"
    )
    async def accept_button(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        bettor = self.bet._get_bettor(interaction.user)
        if bettor.player.trade_cooldown_policy == TradeCooldownPolicy.COOLDOWN:
            if self.bet.cooldown_start_time is None:
                return

            elapsed = datetime.now(timezone.utc) - self.bet.cooldown_start_time
            if elapsed < self.cooldown_duration:
                remaining_time = datetime.now(timezone.utc) + (self.cooldown_duration - elapsed)
                remaining = format_dt(remaining_time, style="R")
                await interaction.response.send_message(
                    f"This bet can only be approved {remaining}, please use this "
                    "time to double check the items to prevent any unwanted bets.",
                    ephemeral=True,
                )
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if bettor.accepted:
            await interaction.followup.send(
                "You have already accepted this bet.", ephemeral=True
            )
            return
        result = await self.bet.confirm(bettor)
        if self.bet.bettor1.accepted and self.bet.bettor2.accepted:
            if result:
                await interaction.followup.send("The FootballDex Bet is now concluded.", ephemeral=True)
            else:
                await interaction.followup.send(
                    ":warning: An error occurred while concluding the bet.", ephemeral=True
                )
        else:
            await interaction.followup.send(
                "You have accepted the bet, waiting for the other user...", ephemeral=True
            )

    @discord.ui.button(
        style=discord.ButtonStyle.danger,
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
    )
    async def deny_button(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
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

        await self.bet.user_cancel(self.bet._get_bettor(interaction.user))
        await interaction.followup.send("Bet has been cancelled.", ephemeral=True)


class BetMenu:
    def __init__(
        self,
        cog: BetCog,
        interaction: discord.Interaction["ballsdexBot"],
        bettor1: BettingUser,
        bettor2: BettingUser,
    ):
        self.cog = cog
        self.bot = interaction.client
        self.channel: discord.TextChannel = cast(discord.TextChannel, interaction.channel)
        self.bettor1 = bettor1
        self.bettor2 = bettor2
        self.embed = discord.Embed()
        self.task: asyncio.Task | None = None
        self.current_view: BetView | ConfirmView = BetView(self)
        self.message: discord.Message
        self.cooldown_start_time: datetime | None = None

    def _get_bettor(self, user: discord.User | discord.Member) -> BettingUser:
        if user.id == self.bettor1.user.id:
            return self.bettor1
        elif user.id == self.bettor2.user.id:
            return self.bettor2
        raise RuntimeError(f"User with ID {user.id} cannot be found in the bet")

    def _generate_embed(self):
        add_command = self.cog.add.extras.get("mention", "`/bet add`")
        remove_command = self.cog.remove.extras.get("mention", "`/bet remove`")
        view_command = self.cog.view.extras.get("mention", "`/bet view`")

        self.embed.title = f"{settings.plural_collectible_name.title()} FootballDex Betting"
        self.embed.color = discord.Colour.gold()
        self.embed.description = (
            f"Add or remove {settings.plural_collectible_name} and packs you want to bet "
            f"using the {add_command} and {remove_command} commands.\n"
            "Once you're finished, click the lock button below to confirm your proposal.\n"
            "You can also lock with nothing if you're receiving a gift.\n\n"
            "*This bet will timeout "
            f"{format_dt(utcnow() + timedelta(minutes=30), style='R')}.*\n\n"
            f"Use the {view_command} command to see the full"
            f" list of {settings.plural_collectible_name} and packs."
        )
        self.embed.set_footer(
            text="This message is updated every 15 seconds, "
            "but you can keep on editing your proposal."
        )

    async def update_message_loop(self):
        """
        A loop task that updates each 15 second the menu with the new content.
        """

        assert self.task
        start_time = datetime.utcnow()

        while True:
            await asyncio.sleep(15)
            if datetime.utcnow() - start_time > timedelta(minutes=15):
                self.embed.colour = discord.Colour.dark_red()
                await self.cancel("The bet timed out")
                return

            try:
                fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
                await self.message.edit(embed=self.embed)
            except Exception:
                log.exception(
                    "Failed to refresh the bet menu "
                    f"guild={self.message.guild.id} "  # type: ignore
                    f"bettor1={self.bettor1.user.id} bettor2={self.bettor2.user.id}"
                )
                self.embed.colour = discord.Colour.dark_red()
                await self.cancel("The bet timed out")
                return

    async def start(self):
        """
        Start the bet by sending the initial message and opening up the proposals.
        """
        self._generate_embed()
        fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
        self.message = await self.channel.send(
            content=f"Hey {self.bettor2.user.mention}, {self.bettor1.user.name} "
            "is proposing a FootballDex Bet with you!",
            embed=self.embed,
            view=self.current_view,
            allowed_mentions=discord.AllowedMentions(users=self.bettor2.player.can_be_mentioned),
        )
        self.task = self.bot.loop.create_task(self.update_message_loop())

    async def cancel(self, reason: str = "The bet has been cancelled."):
        """
        Cancel the bet immediately.
        """
        if self.task:
            self.task.cancel()

        for countryball in self.bettor1.proposal + self.bettor2.proposal:
            await countryball.unlock()

        self.current_view.stop()
        for item in self.current_view.children:
            item.disabled = True  # type: ignore

        fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
        self.embed.description = f"**{reason}**"
        if getattr(self, "message", None):
            await self.message.edit(content=None, embed=self.embed, view=self.current_view)

        # Remove from active bets
        if self.channel.id in self.cog.bets:
            del self.cog.bets[self.channel.id]

    async def lock(self, bettor: BettingUser):
        """
        Lock a bettor's proposal.
        """
        bettor.locked = True

        if self.bettor1.locked and self.bettor2.locked:
            self.current_view.stop()
            self.current_view = ConfirmView(self)
            self.cooldown_start_time = datetime.now(timezone.utc)

            fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
            self.embed.description = (
                "Both users have locked their proposals. "
                "**Please confirm to start the FootballDex Bet!**\n\n"
                "*Winner takes all balls and packs from both players.*"
            )
            await self.message.edit(embed=self.embed, view=self.current_view)

    async def confirm(self, bettor: BettingUser) -> bool:
        """
        Confirm the bet. If both players confirm, execute the FootballDex Bet.
        """
        bettor.accepted = True

        if self.bettor1.accepted and self.bettor2.accepted:
            # Execute the FootballDex Bet
            if self.task:
                self.task.cancel()

            # Random winner selection (50/50)
            winner = random.choice([self.bettor1, self.bettor2])
            loser = self.bettor2 if winner == self.bettor1 else self.bettor1
            
            winner.won = True

            try:
                # Transfer all balls to winner
                all_balls = self.bettor1.proposal + self.bettor2.proposal
                for ball in all_balls:
                    ball.player = winner.player
                    await ball.save()
                    await ball.unlock()

                # Transfer packs to winner
                total_packs = self.bettor1.pack_amount + self.bettor2.pack_amount
                if total_packs > 0:
                    # Update wallet balance using the packs module
                    try:
                        # Import from the packs module where wallet_balance is defined
                        from ballsdex.packages.packs.cog import wallet_balance
                        
                        winner_id = winner.user.id
                        loser_id = loser.user.id
                        
                        # Winner gets all packs
                        wallet_balance[winner_id] += total_packs
                        # Loser loses their bet packs
                        wallet_balance[loser_id] -= loser.pack_amount
                        
                        # Ensure loser doesn't go negative
                        if wallet_balance[loser_id] < 0:
                            wallet_balance[loser_id] = 0
                            
                    except (ImportError, ModuleNotFoundError):
                        # Fallback: search through loaded modules
                        import sys
                        wallet_balance = None
                        for module_name, module in sys.modules.items():
                            if 'packs' in module_name and hasattr(module, 'wallet_balance'):
                                wallet_balance = getattr(module, 'wallet_balance')
                                break
                        
                        if wallet_balance is not None:
                            winner_id = winner.user.id
                            loser_id = loser.user.id
                            wallet_balance[winner_id] += total_packs
                            wallet_balance[loser_id] -= loser.pack_amount
                            if wallet_balance[loser_id] < 0:
                                wallet_balance[loser_id] = 0
                        else:
                            log.warning("Could not find wallet_balance system for pack transfer")
                    except Exception as e:
                        log.warning(f"Failed to transfer packs in bet: {e}")
                        pass

                # Clear proposals
                self.bettor1.clear_proposal()
                self.bettor2.clear_proposal()

                # Update embed
                fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
                self.embed.description = (
                    f"**FootballDex Bet concluded!**\n\n"
                    f"🏆 **{winner.user.name}** won the bet!\n"
                    f"💸 **{loser.user.name}** lost the bet.\n\n"
                    f"**Winner takes all {len(all_balls)} balls"
                    f"{f' and {total_packs} packs' if total_packs > 0 else ''}!**"
                )
                self.embed.color = discord.Colour.green()

                # Log to channel
                log_channel = self.bot.get_channel(1341228457417248940)
                if log_channel:
                    log_embed = discord.Embed(
                        title="FootballDex Bet Result",
                        color=discord.Colour.gold(),
                        timestamp=datetime.utcnow()
                    )
                    log_embed.add_field(
                        name="Winner", 
                        value=f"{winner.user.mention} ({winner.user.id})", 
                        inline=True
                    )
                    log_embed.add_field(
                        name="Loser", 
                        value=f"{loser.user.mention} ({loser.user.id})", 
                        inline=True
                    )
                    log_embed.add_field(
                        name="Items Won", 
                        value=f"{len(all_balls)} balls" + (f", {total_packs} packs" if total_packs > 0 else ""), 
                        inline=True
                    )
                    await log_channel.send(embed=log_embed)

                # Disable view
                self.current_view.stop()
                for item in self.current_view.children:
                    item.disabled = True  # type: ignore

                await self.message.edit(embed=self.embed, view=self.current_view)
                
                # Remove from active bets
                if self.channel.id in self.cog.bets:
                    del self.cog.bets[self.channel.id]
                
                return True

            except Exception as e:
                log.exception(f"Error executing FootballDex Bet: {e}")
                # Unlock all balls on error
                for ball in self.bettor1.proposal + self.bettor2.proposal:
                    await ball.unlock()
                return False

        return True

    async def user_cancel(self, bettor: BettingUser):
        """
        Cancel the bet from a user action.
        """
        bettor.cancelled = True
        await self.cancel(f"**{bettor.user.name}** cancelled the bet.")


class CountryballsSource(menus.ListPageSource):
    def __init__(self, entries: List[BallInstance]):
        super().__init__(entries, per_page=25)

    async def format_page(self, menu: CountryballsSelector, balls: List[BallInstance]):
        menu.set_options(balls)
        return True  # signal to edit the page


class CountryballsSelector(Pages):
    def __init__(
        self,
        interaction: discord.Interaction["ballsdexBot"],
        balls: List[BallInstance],
        cog,
    ):
        self.bot = interaction.client
        self.interaction = interaction
        source = CountryballsSource(balls)
        super().__init__(source, interaction=interaction)
        self.add_item(self.select_ball_menu)
        self.add_item(self.confirm_button)
        self.add_item(self.select_all_button)
        self.add_item(self.clear_button)
        self.balls_selected: Set[BallInstance] = set()
        self.cog = cog

    def set_options(self, balls: List[BallInstance]):
        options: List[discord.SelectOption] = []
        for ball in balls:
            if ball.is_tradeable is False:
                continue
            emoji = self.bot.get_emoji(int(ball.countryball.emoji_id))
            favorite = f"{settings.favorited_collectible_emoji} " if ball.favorite else ""
            special = ball.special_emoji(self.bot, True)
            options.append(
                discord.SelectOption(
                    label=f"{favorite}{special}#{ball.pk:0X} {ball.countryball.country}",
                    description=f"ATK: {ball.attack_bonus:+d}% • HP: {ball.health_bonus:+d}% • "
                    f"Caught on {ball.catch_date.strftime('%d/%m/%y %H:%M')}",
                    emoji=emoji,
                    value=f"{ball.pk}",
                    default=ball in self.balls_selected,
                )
            )
        self.select_ball_menu.options = options
        self.select_ball_menu.max_values = len(options)

    @discord.ui.select(min_values=1, max_values=25)
    async def select_ball_menu(
        self, interaction: discord.Interaction["ballsdexBot"], item: discord.ui.Select
    ):
        for value in item.values:
            ball_instance = await BallInstance.get(id=int(value)).prefetch_related(
                "ball", "player"
            )
            self.balls_selected.add(ball_instance)
        await interaction.response.defer()

    @discord.ui.button(label="Select Page", style=discord.ButtonStyle.secondary)
    async def select_all_button(
        self, interaction: discord.Interaction["ballsdexBot"], button: Button
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        for ball in self.select_ball_menu.options:
            ball_instance = await BallInstance.get(id=int(ball.value)).prefetch_related(
                "ball", "player"
            )
            if ball_instance not in self.balls_selected:
                self.balls_selected.add(ball_instance)
        await interaction.followup.send(
            (
                f"All {settings.plural_collectible_name} on this page have been selected.\n"
                "Note that the menu may not reflect this change until you change page."
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm_button(
        self, interaction: discord.Interaction["ballsdexBot"], button: Button
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        bet, bettor = self.cog.get_bet(interaction)
        if bet is None or bettor is None:
            return await interaction.followup.send(
                "The bet has been cancelled or the user is not part of the bet.",
                ephemeral=True,
            )
        if bettor.locked:
            return await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
        if any(ball in bettor.proposal for ball in self.balls_selected):
            return await interaction.followup.send(
                "You have already added some of the "
                f"{settings.plural_collectible_name} you selected.",
                ephemeral=True,
            )

        if len(self.balls_selected) == 0:
            return await interaction.followup.send(
                f"You have not selected any {settings.plural_collectible_name} "
                "to add to your proposal.",
                ephemeral=True,
            )
        for ball in self.balls_selected:
            if ball.is_tradeable is False:
                return await interaction.followup.send(
                    f"{settings.collectible_name.title()} #{ball.pk:0X} is not tradeable.",
                    ephemeral=True,
                )
            if await ball.is_locked():
                return await interaction.followup.send(
                    f"{settings.collectible_name.title()} #{ball.pk:0X} is locked "
                    "for bet and won't be added to the proposal.",
                    ephemeral=True,
                )
            view = ConfirmChoiceView(interaction)
            if ball.favorite:
                await interaction.followup.send(
                    f"One or more of the {settings.plural_collectible_name} is favorited, "
                    "are you sure you want to add it to the bet?",
                    view=view,
                    ephemeral=True,
                )
                await view.wait()
                if not view.value:
                    return
            bettor.proposal.append(ball)
            await ball.lock_for_trade()
        grammar = (
            f"{settings.collectible_name}"
            if len(self.balls_selected) == 1
            else f"{settings.plural_collectible_name}"
        )
        await interaction.followup.send(
            f"{len(self.balls_selected)} {grammar} added to your proposal.", ephemeral=True
        )
        self.balls_selected.clear()

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger)
    async def clear_button(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        self.balls_selected.clear()
        await interaction.followup.send(
            f"You have cleared all currently selected {settings.plural_collectible_name}."
            f"This does not affect {settings.plural_collectible_name} within your bet.\n"
            f"There may be an instance where it shows {settings.plural_collectible_name} on the"
            " current page as selected, this is not the case - "
            "changing page will show the correct state.",
            ephemeral=True,
        )


class BulkAddView(CountryballsSelector):
    async def on_timeout(self) -> None:
        return await super().on_timeout()
        
        
class BetViewSource(menus.ListPageSource):
    def __init__(self, entries: List[BettingUser]):
        super().__init__(entries, per_page=25)

    async def format_page(self, menu, players: List[BettingUser]):
        menu.set_options(players)
        return True  # signal to edit the page


class BetViewMenu(Pages):
    def __init__(
        self,
        interaction: discord.Interaction["ballsdexBot"],
        proposal: List[BettingUser],
        cog: BetCog,
    ):
        self.bot = interaction.client
        source = BetViewSource(proposal)
        super().__init__(source, interaction=interaction)
        self.add_item(self.select_player_menu)
        self.cog = cog

    def set_options(self, players: List[BettingUser]):
        options: List[discord.SelectOption] = []
        for player in players:
            user_obj = player.user
            plural_check = (
                f"{settings.collectible_name}"
                if len(player.proposal) == 1
                else f"{settings.plural_collectible_name}"
            )
            
            description_parts = []
            if player.proposal:
                description_parts.append(f"{len(player.proposal)} {plural_check}")
            if player.pack_amount > 0:
                pack_text = f"{player.pack_amount} pack{'s' if player.pack_amount != 1 else ''}"
                description_parts.append(pack_text)
            
            description = f"ID: {user_obj.id}"
            if description_parts:
                description += f" | {' + '.join(description_parts)}"
            
            options.append(
                discord.SelectOption(
                    label=f"{user_obj.display_name}",
                    description=description,
                    value=f"{user_obj.id}",
                )
            )
        self.select_player_menu.options = options

    @discord.ui.select()
    async def select_player_menu(
        self, interaction: discord.Interaction["ballsdexBot"], item: discord.ui.Select
    ):
        await interaction.response.defer(thinking=True)
        player = await Player.get(discord_id=int(item.values[0]))
        bet, bettor = self.cog.get_bet(interaction)
        if bet is None or bettor is None:
            return await interaction.followup.send(
                "The bet has been cancelled or the user is not part of the bet.",
                ephemeral=True,
            )
        bet_player = (
            bet.bettor1 if bet.bettor1.user.id == player.discord_id else bet.bettor2
        )
        ball_instances = bet_player.proposal
        
        if len(ball_instances) == 0 and bet_player.pack_amount == 0:
            return await interaction.followup.send(
                f"{bet_player.user} has not added any {settings.plural_collectible_name} or packs.",
                ephemeral=True,
            )
        elif len(ball_instances) == 0:
            return await interaction.followup.send(
                f"{bet_player.user} has only added {bet_player.pack_amount} packs to their bet.",
                ephemeral=True,
            )

        paginator = CountryballsViewer(interaction, ball_instances)
        await paginator.start()