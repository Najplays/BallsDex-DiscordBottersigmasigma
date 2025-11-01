import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, timezone
import random
from discord import Embed, Color, File
from tortoise import models, fields
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont
from discord.ui import View, Button
import asyncio
import logging
logger = logging.getLogger(__name__)
from ballsdex.core.utils.transformers import (
    BallTransform,
    SpecialTransform,
)
from ballsdex.core.models import (
    Ball,
    balls,
    BallInstance,
    BlacklistedGuild,
    BlacklistedID,
    GuildConfig,
    Player,
    Trade,
    TradeObject,
    Special,
)
from ballsdex.settings import settings
from ballsdex.core.bot import BallsDexBot
import ballsdex.packages.config.components as Components
from collections import defaultdict
from ballsdex.core.image_generator.image_gen import draw_card
from io import BytesIO
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)

# Credits
# -------
# - paqueta
# -------

# Pick wallet storage - stores {user_id: number_of_picks}
pick_wallet = defaultdict(int)

# Custom daily usage tracking - stores {user_id: {'count': int, 'first_use': datetime}}
daily_usage_tracking = {}

# Command execution cooldown tracking - stores {user_id: datetime}
command_cooldowns = {}

# Ongoing pick sessions tracking - stores {user_id: {'daily': bool, 'weekly': bool, 'picks': bool}}
ongoing_pick_sessions = defaultdict(lambda: {'daily': False, 'weekly': False, 'picks': False})

# Owners who can give picks
ownersid = {
    749658746535280771
}

# Cooldowns
DAILY_COOLDOWN = timedelta(hours=24)
WEEKLY_COOLDOWN = timedelta(days=7)
COMMAND_COOLDOWN = timedelta(seconds=5)  # 5-second cooldown between commands
gamble_cooldowns = {} 


def check_command_cooldown(user_id: int) -> tuple[bool, timedelta | None]:
    """
    Check if user can execute a command based on the 5-second cooldown.
    Returns (can_execute, remaining_cooldown)
    """
    now = datetime.now(timezone.utc)
    
    if user_id not in command_cooldowns:
        return True, None
    
    last_command_time = command_cooldowns[user_id]
    time_since_last = now - last_command_time
    
    if time_since_last >= COMMAND_COOLDOWN:
        return True, None
    
    remaining = COMMAND_COOLDOWN - time_since_last
    return False, remaining


def set_command_cooldown(user_id: int):
    """Set the command cooldown timestamp for a user"""
    command_cooldowns[user_id] = datetime.now(timezone.utc)


def start_pick_session(user_id: int, pick_type: str):
    """Start a pick session for a user"""
    ongoing_pick_sessions[user_id][pick_type] = True


def end_pick_session(user_id: int, pick_type: str):
    """End a pick session for a user"""
    ongoing_pick_sessions[user_id][pick_type] = False


def has_ongoing_session(user_id: int, pick_type: str) -> bool:
    """Check if user has an ongoing session of the specified type"""
    return ongoing_pick_sessions[user_id][pick_type]


class PickSelectionView(View):
    """View for 5-button pick selection"""
    
    def __init__(self, balls_list, user_id, pick_type, timeout=45):
        super().__init__(timeout=timeout)
        self.balls_list = balls_list
        self.user_id = user_id
        self.pick_type = pick_type  # 'daily', 'weekly', or 'picks'
        self.selected_ball = None
        
        # Create 5 numbered buttons
        for i in range(5):
            button = Button(
                label=str(i + 1),
                style=discord.ButtonStyle.primary,
                custom_id=f"pick_{i}"
            )
            button.callback = self.create_callback(i)
            self.add_item(button)
    
    def create_callback(self, index):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("This is not your pick!", ephemeral=True)
                return
            
            self.selected_ball = self.balls_list[index]
            
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            
            # End the pick session
            end_pick_session(self.user_id, self.pick_type)
            
            await interaction.response.edit_message(view=self)
            self.stop()
        
        return callback
    
    async def on_timeout(self):
        # Disable all buttons on timeout and end the pick session
        for item in self.children:
            item.disabled = True
        
        # End the pick session on timeout
        end_pick_session(self.user_id, self.pick_type)


class Picks(commands.GroupCog, name="picks"):
    """
    A picks system for claiming footballers!
    """

    def __init__(self, bot: BallsDexBot):
        self.bot = bot
        self.bot_tutorial_seen = set()
        self.bot_walletturorial_seen = set()
        super().__init__()

    async def get_random_balls_for_daily(self, player: Player, count: int = 5) -> list[Ball]:
        """Get random balls for daily picks with rarity range 0.1-30.0"""
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.1, rarity__lte=30.0, enabled=True).all()

        if not all_balls:
            return []

        weighted_choices = []
        for ball in all_balls:
            # base weight based on ownership
            base_weight = 1 if ball.id in owned_ids else 5

            # rarity weight according to your tiers
            if 5.0 <= ball.rarity <= 30.0:
                rarity_weight = 16  # common
            elif 2.5 <= ball.rarity < 5.0:
                rarity_weight = 6   # decent
            elif 1.5 <= ball.rarity < 2.5:
                rarity_weight = 3   # rare
            elif 0.5 < ball.rarity < 1.5:
                rarity_weight = 1   # very rare
            else:  # ball.rarity == 0.5 exactly (very very rare)
                rarity_weight = 0.2

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return []

        # Get random sample of unique balls
        selected_balls = []
        available_balls = choices.copy()
        
        for _ in range(min(count, len(set(choices)))):
            if not available_balls:
                break
            ball = random.choice(available_balls)
            if ball not in selected_balls:
                selected_balls.append(ball)
            # Remove all instances of this ball to ensure uniqueness
            available_balls = [b for b in available_balls if b.id != ball.id]
        
        # If we need more balls and don't have enough unique ones, fill with duplicates
        while len(selected_balls) < count and choices:
            ball = random.choice(choices)
            selected_balls.append(ball)
        
        return selected_balls

    async def get_random_balls_for_weekly(self, player: Player, count: int = 5) -> list[Ball]:
        """Get random balls for weekly picks with rarity range 0.03-2.5"""
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.03, rarity__lte=2.5, enabled=True).all()

        if not all_balls:
            return []

        weighted_choices = []
        for ball in all_balls:
            if ball.id in owned_ids:
                base_weight = 1
            else:
                base_weight = 5

            # Explicit rarity weighting for weekly range
            if ball.rarity >= 1.5:  # common in this range
                rarity_weight = 5
            elif ball.rarity >= 0.5:  # uncommon
                rarity_weight = 2
            else:  # rare (below 0.5 rarity)
                rarity_weight = 0.2

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return []

        # Get random sample of unique balls
        selected_balls = []
        available_balls = choices.copy()
        
        for _ in range(min(count, len(set(choices)))):
            if not available_balls:
                break
            ball = random.choice(available_balls)
            if ball not in selected_balls:
                selected_balls.append(ball)
            # Remove all instances of this ball to ensure uniqueness
            available_balls = [b for b in available_balls if b.id != ball.id]
        
        # If we need more balls and don't have enough unique ones, fill with duplicates
        while len(selected_balls) < count and choices:
            ball = random.choice(choices)
            selected_balls.append(ball)
        
        return selected_balls

    async def get_random_balls_for_picks(self, player: Player, count: int = 5) -> list[Ball]:
        """Get random balls for picks pick with rarity range 0.1-30.0 (0.1 very hard to get)"""
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.1, rarity__lte=30.0, enabled=True).all()

        if not all_balls:
            return []

        weighted_choices = []
        for ball in all_balls:
            # base weight based on ownership
            base_weight = 1 if ball.id in owned_ids else 5

            # Modified rarity weight for picks pick - making 0.1 very hard to get
            if 10.0 <= ball.rarity <= 30.0:
                rarity_weight = 20  # very common
            elif 5.0 <= ball.rarity < 10.0:
                rarity_weight = 15  # common
            elif 2.0 <= ball.rarity < 5.0:
                rarity_weight = 8   # decent
            elif 1.0 <= ball.rarity < 2.0:
                rarity_weight = 3   # rare
            elif 0.5 <= ball.rarity < 1.0:
                rarity_weight = 1   # very rare
            elif 0.2 <= ball.rarity < 0.5:
                rarity_weight = 0.3 # extremely rare
            else:  # 0.1 <= ball.rarity < 0.2 (ultra rare)
                rarity_weight = 0.05  # extremely hard to get

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * max(1, int(weight)))

        if not choices:
            return []

        # Get random sample of unique balls
        selected_balls = []
        available_balls = choices.copy()
        
        for _ in range(min(count, len(set(choices)))):
            if not available_balls:
                break
            ball = random.choice(available_balls)
            if ball not in selected_balls:
                selected_balls.append(ball)
            # Remove all instances of this ball to ensure uniqueness
            available_balls = [b for b in available_balls if b.id != ball.id]
        
        # If we need more balls and don't have enough unique ones, fill with duplicates
        while len(selected_balls) < count and choices:
            ball = random.choice(choices)
            selected_balls.append(ball)
        
        return selected_balls

    async def get_random_ball_any(self, player: Player) -> Ball | None:
        """Get any random ball for wallet picks (no rarity restrictions)"""
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(enabled=True).all()

        if not all_balls:
            return None

        weighted_choices = []
        for ball in all_balls:
            # base weight based on ownership
            base_weight = 1 if ball.id in owned_ids else 5
            
            # Simple rarity weight
            rarity_weight = max(0.1, 10 - ball.rarity)
            
            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return None

        return random.choice(choices)

    def check_daily_usage(self, user_id: str) -> tuple[bool, int]:
        """
        Check if user can use daily command and return remaining uses.
        Returns (can_use, remaining_uses)
        """
        now = datetime.now(timezone.utc)
        
        if user_id not in daily_usage_tracking:
            # First time using daily command
            daily_usage_tracking[user_id] = {
                'count': 0,
                'first_use': now
            }
            return True, 1
        
        user_data = daily_usage_tracking[user_id]
        time_since_first_use = now - user_data['first_use']
        
        # Reset if 24 hours have passed since first use
        if time_since_first_use >= DAILY_COOLDOWN:
            daily_usage_tracking[user_id] = {
                'count': 0,
                'first_use': now
            }
            return True, 1
        
        # Check if user has used their 1 daily attempt
        if user_data['count'] >= 1:
            return False, 0
        
        remaining = 1 - user_data['count']
        return True, remaining

    def increment_daily_usage(self, user_id: str):
        """Increment the daily usage count for a user"""
        if user_id in daily_usage_tracking:
            daily_usage_tracking[user_id]['count'] += 1

    def get_daily_cooldown_remaining(self, user_id: str) -> timedelta | None:
        """Get remaining cooldown time for daily command"""
        if user_id not in daily_usage_tracking:
            return None
        
        user_data = daily_usage_tracking[user_id]
        if user_data['count'] < 1:
            return None
        
        now = datetime.now(timezone.utc)
        cooldown_end = user_data['first_use'] + DAILY_COOLDOWN
        
        if now >= cooldown_end:
            return None
        
        return cooldown_end - now

    @app_commands.command(name="daily", description="Pick your daily Footballer!")
    async def daily(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = interaction.user.id
        
        # Check if user has an ongoing daily pick session
        if has_ongoing_session(user_id, 'daily'):
            await interaction.response.send_message(
                "🚫 You already have an ongoing daily pick! Please complete it first.",
                ephemeral=True
            )
            return
        
        # Check command cooldown first
        can_execute, remaining_cooldown = check_command_cooldown(user_id)
        if not can_execute:
            seconds_remaining = int(remaining_cooldown.total_seconds())
            await interaction.response.send_message(
                f"⏰ Please wait {seconds_remaining} seconds before using another pick command!",
                ephemeral=True
            )
            return
        
        # Set command cooldown
        set_command_cooldown(user_id)

        user_id_str = str(user_id)

        # Check account age requirement
        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        # Check daily usage limits
        can_use, remaining_uses = self.check_daily_usage(user_id)
        
        if not can_use:
            cooldown_remaining = self.get_daily_cooldown_remaining(user_id)
            if cooldown_remaining:
                hours = int(cooldown_remaining.total_seconds() // 3600)
                minutes = int((cooldown_remaining.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"⏰ You've already used your daily pick! Come back in {hours}h {minutes}m for your next daily pick.",
                    ephemeral=True
                )
                return

        await interaction.response.defer()
        
        player, _ = await Player.get_or_create(discord_id=str(user_id))
        balls = await self.get_random_balls_for_daily(player, 5)

        if len(balls) < 5:
            await interaction.followup.send("Not enough balls are available.", ephemeral=True)
            return

        # Create pick selection embed
        pick_embed = Embed(title="Pick a footballer", color=Color.blue())
        
        # Add ball options with their actual emojis
        description = ""
        for i, ball in enumerate(balls):
            emoji = self.bot.get_emoji(ball.emoji_id) if self.bot.get_emoji(ball.emoji_id) else "⚽"
            description += f"{i+1}. {emoji} **{ball.country}** (Rarity: {ball.rarity})\n"
        
        pick_embed.description = description
        pick_embed.set_footer(text="Your daily pick - choose wisely!")
        
        # Start the pick session
        start_pick_session(user_id, 'daily')
        
        # Create view with buttons
        view = PickSelectionView(balls, interaction.user.id, 'daily')
        
        msg = await interaction.followup.send(embed=pick_embed, view=view)
        
        # Wait for user selection
        await view.wait()
        
        if view.selected_ball is None:
            timeout_embed = Embed(title="⏰ Pick timed out!", color=Color.red())
            timeout_embed.description = "You didn't make a selection in time."
            await msg.edit(embed=timeout_embed, view=None)
            return
        
        # Increment usage count
        self.increment_daily_usage(user_id)
        
        ball = view.selected_ball
        
        # Walkout animation starts here
        walkout_embed = Embed(title="🎉 Daily Pick Opening...", color=Color.dark_gray())
        walkout_embed.set_footer(text="Come back tomorrow for your next daily pick!")
        await msg.edit(embed=walkout_embed, view=None)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
        )
        
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.title = f"🎁 You got **{ball.country}**!"
        walkout_embed.color = Color.gold()

        # Generate image card
        content, file, view_new = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view_new)
        file.close()

        # Log it
        log_channel_id = 1361522228021297404
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Daily pick and got **{ball.country}**\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"• Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[DAILY PICK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} | "
            f"Daily pick used | Footballer ID: `#{ball.pk:0X}`"
        )

    @app_commands.command(name="weekly", description="Pick your weekly Footballer!")
    @app_commands.checks.cooldown(1, 604800, key=lambda i: i.user.id)
    async def weekly(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = interaction.user.id
        
        # Check if user has an ongoing weekly pick session
        if has_ongoing_session(user_id, 'weekly'):
            await interaction.response.send_message(
                "🚫 You already have an ongoing weekly pick! Please complete it first.",
                ephemeral=True
            )
            return
        
        # Check command cooldown first
        can_execute, remaining_cooldown = check_command_cooldown(user_id)
        if not can_execute:
            seconds_remaining = int(remaining_cooldown.total_seconds())
            await interaction.response.send_message(
                f"⏰ Please wait {seconds_remaining} seconds before using another pick command!",
                ephemeral=True
            )
            return
        
        # Set command cooldown
        set_command_cooldown(user_id)

        user_id_str = str(user_id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        
        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        balls = await self.get_random_balls_for_weekly(player, 5)

        if len(balls) < 5:
            await interaction.followup.send("Not enough balls are available.", ephemeral=True)
            return

        # Create pick selection embed
        pick_embed = Embed(title="Pick a footballer", color=Color.purple())
        
        # Add ball options with their actual emojis
        description = ""
        for i, ball in enumerate(balls):
            emoji = self.bot.get_emoji(ball.emoji_id) if self.bot.get_emoji(ball.emoji_id) else "⚽"
            description += f"{i+1}. {emoji} **{ball.country}** (Rarity: {ball.rarity})\n"
        
        pick_embed.description = description
        pick_embed.set_footer(text="Come back in 7 days for your next weekly pick!")
        
        # Start the pick session
        start_pick_session(user_id, 'weekly')
        
        # Create view with buttons
        view = PickSelectionView(balls, interaction.user.id, 'weekly')
        
        msg = await interaction.followup.send(embed=pick_embed, view=view)
        
        # Wait for user selection
        await view.wait()
        
        if view.selected_ball is None:
            timeout_embed = Embed(title="⏰ Pick timed out!", color=Color.red())
            timeout_embed.description = "You didn't make a selection in time."
            await msg.edit(embed=timeout_embed, view=None)
            return
        
        ball = view.selected_ball
        
        # Walkout animation starts here
        walkout_embed = Embed(title="🎉 Weekly Pick Opening...", color=Color.dark_gray())
        walkout_embed.set_footer(text="Come back in 7 days for your next weekly pick!")
        await msg.edit(embed=walkout_embed, view=None)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
        )
        
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.title = f"🎁 You got **{ball.country}**!"
        walkout_embed.color = Color.from_rgb(229, 255, 0)  # Yellow-green like reference

        # Generate image card
        content, file, view_new = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view_new)
        file.close()

        # Log it
        log_channel_id = 1361522228021297404
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Weekly pick and got **{ball.country}**\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[WEEKLY PICK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} "
            f"Footballer ID: `#{ball.pk:0X}`"
        )

    @app_commands.command(name="wallet", description="Check your pick wallet balance")
    async def wallet(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = str(interaction.user.id)
        balance = pick_wallet[user_id]
        
        embed = Embed(
            title=f"{interaction.user.display_name}'s Wallet",
            description=f"You currently have **{balance}** pick(s).",
            color=Color.green()
        )
        embed.set_footer(text="FootballDex Wallet")
        
        await interaction.response.send_message(embed=embed, ephemeral=False)

    async def get_random_balls_for_wallet(self, player: Player, count: int = 5) -> list[Ball]:
        """Get random balls for wallet picks (no rarity restrictions)"""
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(enabled=True).all()

        if not all_balls:
            return []

        weighted_choices = []
        for ball in all_balls:
            # base weight based on ownership
            base_weight = 1 if ball.id in owned_ids else 5
            
            # Simple rarity weight
            rarity_weight = max(0.1, 10 - ball.rarity)
            
            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return []

        # Get random sample of unique balls
        selected_balls = []
        available_balls = choices.copy()
        
        for _ in range(min(count, len(set(choices)))):
            if not available_balls:
                break
            ball = random.choice(available_balls)
            if ball not in selected_balls:
                selected_balls.append(ball)
            # Remove all instances of this ball to ensure uniqueness
            available_balls = [b for b in available_balls if b.id != ball.id]
        
        # If we need more balls and don't have enough unique ones, fill with duplicates
        while len(selected_balls) < count and choices:
            ball = random.choice(choices)
            selected_balls.append(ball)
        
        return selected_balls

    @app_commands.command(name="pick", description="Open a pick from your wallet")
    async def pick(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = interaction.user.id
        
        # Check if user has an ongoing picks pick session
        if has_ongoing_session(user_id, 'picks'):
            await interaction.response.send_message(
                "🚫 You already have an ongoing pick selection! Please complete it first.",
                ephemeral=True
            )
            return
        
        # Check command cooldown first
        can_execute, remaining_cooldown = check_command_cooldown(user_id)
        if not can_execute:
            seconds_remaining = int(remaining_cooldown.total_seconds())
            await interaction.response.send_message(
                f"⏰ Please wait {seconds_remaining} seconds before using another pick command!",
                ephemeral=True
            )
            return
        
        # Set command cooldown
        set_command_cooldown(user_id)

        user_id_str = str(user_id)
        
        if pick_wallet[user_id_str] <= 0:
            await interaction.response.send_message("You don't have any picks in your wallet!", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        balls = await self.get_random_balls_for_picks(player, 5)

        if len(balls) < 5:
            await interaction.followup.send("Not enough balls are available.", ephemeral=True)
            return

        # Create pick selection embed
        pick_embed = Embed(title="Pick a footballer", color=Color.orange())
        
        # Add ball options with their actual emojis
        description = ""
        for i, ball in enumerate(balls):
            emoji = self.bot.get_emoji(ball.emoji_id) if self.bot.get_emoji(ball.emoji_id) else "⚽"
            description += f"{i+1}. {emoji} **{ball.country}** (Rarity: {ball.rarity})\n"
        
        pick_embed.description = description
        pick_embed.set_footer(text=f"Picks remaining: {pick_wallet[user_id_str]-1} after this pick")
        
        # Start the pick session
        start_pick_session(user_id, 'picks')
        
        # Create view with buttons
        view = PickSelectionView(balls, interaction.user.id, 'picks')
        
        msg = await interaction.followup.send(embed=pick_embed, view=view)
        
        # Wait for user selection
        await view.wait()
        
        if view.selected_ball is None:
            timeout_embed = Embed(title="⏰ Pick timed out!", color=Color.red())
            timeout_embed.description = "You didn't make a selection in time."
            await msg.edit(embed=timeout_embed, view=None)
            return
        
        # Deduct pick from wallet
        pick_wallet[user_id_str] -= 1
        
        ball = view.selected_ball
        
        # Walkout animation starts here
        walkout_embed = Embed(title="🎁 Opening Pick...", color=Color.dark_gray())
        walkout_embed.set_footer(text="FootballDex Picks")
        await msg.edit(embed=walkout_embed, view=None)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
        )
        
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.title = f"🎉 You picked **{ball.country}**!"
        walkout_embed.color = Color.gold()

        # Generate image card
        content, file, view_new = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view_new)
        file.close()

        logger.info(f"[WALLET PICK] {interaction.user} ({interaction.user.id}) received {ball.country} (Rarity: {ball.rarity})")

    @app_commands.command(name="gamble", description="Gamble your picks for a chance to win more!")
    async def gamble(self, interaction: discord.Interaction[BallsDexBot], amount: int):
        user_id = interaction.user.id
        
        # Check command cooldown first
        can_execute, remaining_cooldown = check_command_cooldown(user_id)
        if not can_execute:
            seconds_remaining = int(remaining_cooldown.total_seconds())
            await interaction.response.send_message(
                f"⏰ Please wait {seconds_remaining} seconds before using another pick command!",
                ephemeral=True
            )
            return
        
        # Set command cooldown
        set_command_cooldown(user_id)

        user_id_str = str(user_id)
        
        if amount <= 0:
            await interaction.response.send_message("You must gamble at least 1 pick!", ephemeral=True)
            return
        
        if amount > 100:
            await interaction.response.send_message("You can only gamble a maximum of 100 picks at once!", ephemeral=True)
            return
        
        if pick_wallet[user_id_str] < amount:
            await interaction.response.send_message(f"You only have {pick_wallet[user_id_str]} picks in your wallet!", ephemeral=True)
            return
        
        # Check gamble cooldown (30 seconds)
        now = datetime.now(timezone.utc)
        if user_id in gamble_cooldowns:
            time_diff = now - gamble_cooldowns[user_id]
            if time_diff < timedelta(seconds=30):
                remaining = timedelta(seconds=30) - time_diff
                seconds = int(remaining.total_seconds())
                await interaction.response.send_message(f"You can gamble again in {seconds} seconds!", ephemeral=True)
                return
        
        # Deduct picks immediately
        pick_wallet[user_id_str] -= amount

        await interaction.response.defer()

        suspense = Embed(
            title=f"🎲 Gambling {amount} pick{'s' if amount > 1 else ''}...",
            description="Rolling the dice...",
            color=Color.dark_grey()
        )
        suspense.set_footer(text="Good luck...")
        msg = await interaction.followup.send(embed=suspense)

        await asyncio.sleep(2)

        # Set cooldown
        gamble_cooldowns[user_id] = now

        # Always 50/50 win chance
        result = "win" if random.choice([True, False]) else "lose"

        if result == "win":
            reward = amount * 2
            pick_wallet[user_id_str] += reward
            suspense.title = f"🎉 You WON {reward} picks!"
            suspense.color = Color.green()
            suspense.description = f"Luck is on your side. You risked {amount}, and won {reward}!"
        else:
            suspense.title = f"💀 You LOST your {amount} pick{'s' if amount > 1 else ''}!"
            suspense.color = Color.red()
            suspense.description = "Bad luck... you lost it all."

        await msg.edit(embed=suspense)

    @app_commands.command(name="owners-add-pick", description="Add picks to a user's wallet (Owners only)")
    async def owners_add_pick(self, interaction: discord.Interaction[BallsDexBot], user: discord.Member, amount: int):
        if interaction.user.id not in ownersid:
            await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("Amount must be positive!", ephemeral=True)
            return
        
        user_id = str(user.id)
        pick_wallet[user_id] += amount
        
        embed = Embed(
            title="FootballDex Picks Added!",
            description=(
                f"{interaction.user.mention} has added **{amount}** pick(s) to {user.mention}'s wallet.\n"
                f"🪙 **{user.name}'s New Balance**: `{pick_wallet[user_id]} picks`"
            ),
            color=Color.green()
        )
        embed.set_footer(text="Pick System")
        embed.set_thumbnail(url=user.display_avatar.url)
        
        await interaction.response.send_message(embed=embed)
        
        logger.info(f"[OWNER ADD] {interaction.user} added {amount} picks to {user} (New balance: {pick_wallet[user_id]})")

    @app_commands.command(name="owners-remove-pick", description="Remove picks from a user's wallet (Owners only)")
    async def owners_remove_pick(self, interaction: discord.Interaction[BallsDexBot], user: discord.Member, amount: int):
        if interaction.user.id not in ownersid:
            await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
            return
        
        if amount <= 0:
            await interaction.response.send_message("Amount must be positive!", ephemeral=True)
            return
        
        user_id = str(user.id)
        
        if pick_wallet[user_id] < amount:
            await interaction.response.send_message(f"{user.mention} only has {pick_wallet[user_id]} picks!", ephemeral=True)
            return
        
        pick_wallet[user_id] -= amount
        
        embed = Embed(
            title="FootballDex Picks Removed!",
            description=(
                f"{interaction.user.mention} has removed **{amount}** pick(s) from {user.mention}'s wallet.\n"
                f"🪙 **{user.name}'s New Balance**: `{pick_wallet[user_id]} picks`"
            ),
            color=Color.red()
        )
        embed.set_footer(text="Pick System")
        embed.set_thumbnail(url=user.display_avatar.url)
        
        await interaction.response.send_message(embed=embed)
        
        logger.info(f"[OWNER REMOVE] {interaction.user} removed {amount} picks from {user} (New balance: {pick_wallet[user_id]})")


async def setup(bot):
    await bot.add_cog(Picks(bot))
