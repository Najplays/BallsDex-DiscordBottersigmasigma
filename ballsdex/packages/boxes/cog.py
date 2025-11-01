import discord
from discord.ext import commands
from discord import app_commands
from collections import Counter
from datetime import datetime, timedelta, timezone
import io
from tortoise.expressions import RawSQL  
import random
from discord import Embed, Color, File
from tortoise import models, fields
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont
from discord.ui import View
import asyncio
from tortoise.exceptions import DoesNotExist
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
from ballsdex.core.image_generator. image_gen import draw_card
from io import BytesIO
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)

# Credits
# -------
# - crashtestalex
# - hippopotis
# - dot_zz
# -------

# Track last claim times
last_daily_times = {}
last_weekly_times = {}
wallet_balance = defaultdict(int)
packly_pool = defaultdict(int)

# Custom daily usage tracking - stores {user_id: {'count': int, 'first_use': datetime}}
daily_usage_tracking = {}

# Owners who can give packs
ownersid = {
    1079166030166896711,
    917048116115542016,
    784414771993903125,
    749658746535280771,
    767663084890226689,
    1184739489315299339
}

# Cooldowns
DAILY_COOLDOWN = timedelta(hours=24)
WEEKLY_COOLDOWN = timedelta(days=7)
gamble_cooldowns = {} 

#Dictionaries
packly_pool = defaultdict(int)


class SkipView(View):
    """View for skip button during multipack opening"""
    def __init__(self):
        super().__init__(timeout=60)
        self.skipped = False

    @discord.ui.button(label="Skip Animation", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.skipped = True
        button.label = "Skipped ✓"
        button.disabled = True
        button.style = discord.ButtonStyle.success
        
        # Update embed to show skip confirmation
        current_embed = interaction.message.embeds[0]
        current_embed.description = "⏩ **Animation skipped! Showing final results...**"
        current_embed.color = Color.green()
        
        await interaction.response.edit_message(embed=current_embed, view=self)
        self.stop()


class Claim(commands.GroupCog, name="packs"):
    """
    A little simple daily pack!
    """

    def __init__(self, bot: BallsDexBot):
        self.bot = bot
        self.bot_tutorial_seen = set()
        self.bot_walletturorial_seen = set()
        self.pack_queue = asyncio.Queue()
        self.active_users = set()
        self.bot.loop.create_task(self._start_worker_manager())
        super().__init__()

    async def get_random_special(self) -> Special | None:
        """
        Get a random special based on rarity probability and date restrictions.
        Returns None if no special is selected or available.
        """
        now = datetime.now(timezone.utc)
        
        # Get all active specials that respect date restrictions
        try:
            from tortoise.expressions import Q
            active_specials = await Special.filter(
                # Check start_date and end_date constraints
                Q(start_date__isnull=True) | Q(start_date__lte=now),
                Q(end_date__isnull=True) | Q(end_date__gte=now),
                # Only include specials that are not hidden
                hidden=False
            ).all()
        except:
            # Fallback if Q import fails
            active_specials = await Special.all()
        
        if not active_specials:
            return None
        
        # Apply rarity probability for each special
        for special in active_specials:
            if random.random() < special.rarity:
                return special
        
        return None

    async def _start_worker_manager(self):
        """
        Manages the queue and passes tasks to concurrent workers.
        """
        while True:
            # Get the task from the async queue
            user_id, packs, interaction = await self.pack_queue.get()
            
            # Create a new task to process this request concurrently.
            asyncio.create_task(
                self._process_multipackly_and_clean_up(user_id, packs, interaction)
            )
            
            # Mark the queue task as done, allowing it to move to the next item
            self.pack_queue.task_done()
    
    async def _process_multipackly_and_clean_up(self, user_id, packs, interaction):
        """
        This is a wrapper function that runs the main process and ensures cleanup.
        """
        try:
            await self._process_multipackly(user_id, packs, interaction)
        except Exception as e:
            logger.error(f"Error processing multipackly for user {user_id}: {e}")
            try:
                # On error, send an ephemeral message
                await interaction.followup.send(
                    f"❌ An unexpected error occurred while opening your packs.",
                    ephemeral=True
                )
            except discord.NotFound:
                logger.warning(f"Could not send error message for user {user_id} - interaction not found.")
        finally:
            # This is the crucial cleanup step for both success and failure
            self.active_users.discard(user_id)

    async def get_random_ball(self, player: Player) -> Ball | None:
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.03, rarity__lte=30.0, enabled=True).all()

        if not all_balls:
            return None

        weighted_choices = []
        for ball in all_balls:
            # base weight based on ownership
            base_weight = 1 if ball.id in owned_ids else 1

            # rarity weight according to your tiers
            if 5.0 <= ball.rarity <= 30.0:
                rarity_weight = 1600  # common
            elif 2.5 <= ball.rarity < 5.0:
                rarity_weight = 600   # decent
            elif 1.5 <= ball.rarity < 2.5:
                rarity_weight = 300  # rare
            elif 0.5 < ball.rarity < 1.5:
                rarity_weight = 100  # very rare
            elif 0.1 < ball.rarity < 0.5:
                rarity_weight = 30 # very very rare 
            elif 0.03 <= ball.rarity <= 0.1:
                rarity_weight = 20  # ultra rare

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
            return True, 3
        
        user_data = daily_usage_tracking[user_id]
        time_since_first_use = now - user_data['first_use']
        
        # Reset if 24 hours have passed since first use
        if time_since_first_use >= DAILY_COOLDOWN:
            daily_usage_tracking[user_id] = {
                'count': 0,
                'first_use': now
            }
            return True, 3
        
        # Check if user has used all 3 attempts
        if user_data['count'] >= 3:
            return False, 0
        
        remaining = 3 - user_data['count']
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
        if user_data['count'] < 3:
            return None
        
        now = datetime.now(timezone.utc)
        cooldown_end = user_data['first_use'] + DAILY_COOLDOWN
        
        if now >= cooldown_end:
            return None
        
        return cooldown_end - now

    async def getdasigmaballmate(self, player: Player) -> Ball | None:
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.03, rarity__lte=5.0, enabled=True).all()

        if not all_balls:
            return None

        weighted_choices = []
        for ball in all_balls:
            if ball.id in owned_ids:
                base_weight = 1
            else:
                base_weight = 1

            # Explicit rarity weighting
            if ball.rarity >= 4.5:  # very common
                rarity_weight = 900
            elif ball.rarity >= 1.5:  # common
                rarity_weight = 500
            elif ball.rarity >= 0.5:  # uncommon
                rarity_weight = 200
            else:  # rare (below 0.5 rarity)
                rarity_weight = 20

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return None

        return random.choice(choices)

    def format_special_emoji(self, special: Special | None) -> str:
        """Format special emoji for display"""
        if not special:
            return ""
        
        if special.emoji:
            try:
                emoji_id = int(special.emoji)
                emoji = self.bot.get_emoji(emoji_id) or "⚡"
                return str(emoji)
            except ValueError:
                return special.emoji
        return "⚡"

    @app_commands.command(name="daily", description="Claim your daily Footballer! (3 uses per day)")
    async def daily(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = str(interaction.user.id)
        username = interaction.user.name

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
                    f"⏰ You've used all 3 daily packs! Come back in {hours}h {minutes}m for your next set of daily packs.",
                    ephemeral=True
                )
                return

        await interaction.response.defer()
        
        # Increment usage count
        self.increment_daily_usage(user_id)
        
        # Get updated remaining uses after incrementing
        _, new_remaining = self.check_daily_usage(user_id)
        player, _ = await Player.get_or_create(discord_id=str(user_id))
        ball = await self.get_random_ball(player)

        if not ball:
            await interaction.followup.send("No balls are available.", ephemeral=True)
            return

        # Get random special for this pack
        special = await self.get_random_special()

        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        # Walkout starts here
        walkout_embed = Embed(title="🎉 Daily Pack Opening...", color=Color.dark_gray())
        remaining_text = f"Remaining daily uses: {new_remaining}/3" if new_remaining > 0 else "All daily uses consumed! Come back tomorrow."
        walkout_embed.set_footer(text=remaining_text)
        msg = await interaction.followup.send(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        # Add special information to walkout if special exists
        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"
            
            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎁 You got **{ball.country}**{special_text}"
        walkout_embed.color = Color.gold()

        # Generate image card
        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()

        # ✅ Log it
        log_channel_id = 1361522228021297404  # <- Replace with your logging channel ID
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        special_info = f" | Special: {special.name}" if special else ""

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Daily pack and got **{ball.country}** (Use {3-new_remaining}/3){special_info}\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"• Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[DAILY PACK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} | "
            f"Daily use {3-new_remaining}/3 | Footballer ID: `#{ball.pk:0X}`{special_info}"
        )


    @app_commands.command(name="weekly", description="Claim your weekly Footballer!")
    @app_commands.checks.cooldown(1, 604800, key=lambda i: i.user.id)
    async def weekly(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        now = datetime.now()
        last_claim = last_weekly_times.get(user_id)


        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        ball = await self.getdasigmaballmate(player)

        if not ball:
            await interaction.response.send_message("No balls are available.", ephemeral=True)
            return

        # Get random special for this pack
        special = await self.get_random_special()

        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        # Walkout-style embed animation
        walkout_embed = discord.Embed(title="🎉 Weekly Pack Opening...", color=discord.Color.dark_gray())
        walkout_embed.set_footer(text="Come back in 7 days for your next claim!")
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        # Add special information to walkout if special exists
        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"
            
            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎁 You got **{ball.country}**{special_text}"
        walkout_embed.color = discord.Color.from_rgb(229, 255, 0)  # You can randomize if you want

        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()


        # ✅ Log the weekly pack grant to a specific channel and the bot's logger
        log_channel_id = 1361522228021297404  # <- Replace with your logging channel ID
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        special_info = f" | Special: {special.name}" if special else ""

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Weekly pack and got **{ball.country}**{special_info}\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[WEEKLY PACK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} | "
            f"Footballer ID: `#{ball.pk:0X}`{special_info}"
        )





    # Main /packly command to claim a ball after using a pack
    @app_commands.command(name="packly", description="Claim your footballer from the packly!")
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
    async def packly(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return
        
        # Ensure user starts with 1 pack if no balance is set
        if user_id not in wallet_balance:
            wallet_balance[user_id] = 1  # Initialize with 1 pack

        # Check if the user has enough packs to claim
        if wallet_balance[user_id] < 1:
            await interaction.response.send_message(
                "You don't have enough packs!",
                ephemeral=True
            )
            return

        # Deduct 1 pack from user's wallet for claiming a ball
        wallet_balance[user_id] -= 1

        # Assign a random ball to the user
        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        ball = await self.get_random_ball(player)

        if not ball:
            await interaction.response.send_message("No footballers are available.", ephemeral=True)
            return

        # Get random special for this pack
        special = await self.get_random_special()

        # Create an instance of the ball for the user
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        # Walkout-style embed animation
        walkout_embed = discord.Embed(title="🎁 Opening Packly...", color=discord.Color.dark_gray())
        walkout_embed.set_footer(text="FootballDex Packly")
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=walkout_embed)


        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        # Add special information to walkout if special exists
        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"
            
            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎉 You claimed **{ball.country}** from Packly{special_text}"
        walkout_embed.color = discord.Color.gold()

        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()

    @app_commands.command(
        name="multipackly",
        description="Claim multiple footballers from the multipackly!"
    )
    @app_commands.describe(packs="Number of packs to open (1-75)")
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
    async def multipackly(self, interaction, packs: int):
        user_id = str(interaction.user.id)

        # ----------------- ERROR CHECKS -----------------
        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        if user_id not in wallet_balance:
            wallet_balance[user_id] = 1

        if packs < 1 or packs > 75:
            await interaction.response.send_message(
                "You can only open between 1 and 75 packs!",
                ephemeral=True
            )
            return

        if wallet_balance[user_id] < packs:
            await interaction.response.send_message(
                "You don't have enough packs!",
                ephemeral=True
            )
            return

        if user_id in self.active_users:
            await interaction.response.send_message(
                "You already have a multipackly opening in progress!",
                ephemeral=True
            )
            return

        # Deduct packs immediately
        wallet_balance[user_id] -= packs

        # Add user to queue and mark active
        self.active_users.add(user_id)
        await self.pack_queue.put((user_id, packs, interaction))

        # ephemeral confirmation only
        await interaction.response.send_message(
            f"✅ Your {packs} packs have been added to the queue! You will receive an embed when your packs open.",
            ephemeral=True
        )

    # ---------------- THE INSTANCE METHOD (must be here) ----------------
    async def _process_multipackly(self, user_id, packs, interaction):
        """
        This method is an instance method on Claim. Workers call self._process_multipackly(...)
        so it *must* live inside the Claim class (indented).
        """
        # quick config
        DETAILED_LIMIT = 75
        BATCH_SIZE = 5
        EMBED_SLEEP = 1.5
        SUMMARY_ATTACHMENT_NAME = "multipackly_results.txt"

        # 1) send a visible queued message in channel (we'll edit this)
        queued_embed = discord.Embed(
            title="⏳ Multipackly Queue",
            description="You're in the queue! Your packs will open automatically. Feel free to do other commands.",
            color=discord.Color.orange()
        )
        queued_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        queued_embed.set_footer(text="FootballDex MultiPacklys")
        message = await interaction.channel.send(embed=queued_embed)

        # 2) short ready embed
        ready_embed = discord.Embed(
            title="✅ Ready to Open Multipackly!",
            description=f"You can now open **{packs} packs**!",
            color=discord.Color.green()
        )
        ready_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        ready_embed.set_footer(text="FootballDex MultiPacklys")
        await message.edit(embed=ready_embed)
        await asyncio.sleep(2)

        # 3) choose balls
        player, _ = await Player.get_or_create(discord_id=str(user_id))
        balls = []
        for _ in range(packs):
            ball = await self.get_random_ball(player)
            if not ball:
                await interaction.followup.send("No footballers are available.", ephemeral=True)
                return
            balls.append(ball)

        # 4) prepare valid specials once
        now = datetime.now(timezone.utc)
        specials_raw = await Special.filter(hidden=False).all()
        valid_specials = [
            s for s in specials_raw
            if (s.start_date is None or s.start_date <= now) and (s.end_date is None or s.end_date >= now)
        ]

        # 5) decide special per ball using special.rarity probability
        specials_for_instances = []
        if valid_specials:
            for _ in balls:
                candidate = random.choice(valid_specials)
                if random.random() < (candidate.rarity or 0):
                    specials_for_instances.append(candidate)
                else:
                    specials_for_instances.append(None)
        else:
            specials_for_instances = [None] * len(balls)

        # 6) bulk create BallInstance objects
        instances = [
            BallInstance(
                ball=ball,
                player=player,
                special=specials_for_instances[idx],
                attack_bonus=random.randint(-20, 20),
                health_bonus=random.randint(-20, 20)
            ) for idx, ball in enumerate(balls)
        ]
        await BallInstance.bulk_create(instances)

        # 7) detailed vs compact
        compact_mode = packs > DETAILED_LIMIT

        if not compact_mode:
            # detailed animation
            pulled = []
            for i in range(0, len(balls), BATCH_SIZE):
                batch = balls[i:i + BATCH_SIZE]
                pulled.extend([b.country for b in batch])

                description_lines = []
                for j, ball in enumerate(batch):
                    idx = i + j
                    regime_name = getattr(ball.cached_regime, "name", "Unknown")
                    special_inst = specials_for_instances[idx]
                    special_text = f"✨ Special: {special_inst.name}" if special_inst else ""

                    single_block = (
                        f"🌟 **Rarity:** {ball.rarity}\n"
                        f"⚽ **Footballer:** {ball.country}\n"
                        f"💳 **Card:** {regime_name}\n"
                        f"⚔ {ball.attack} | ❤️ {ball.health}\n"
                        f"{special_text}\n"
                        f"━━━━━━━━━━"
                    )
                    description_lines.append(single_block)

                embed = discord.Embed(
                    title=f"🏆 Packs {i+1}-{i+len(batch)} revealed!",
                    description="\n".join(description_lines),
                    color=discord.Color.random()
                )
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                embed.set_footer(text="FootballDex Pack Opening")
                await message.edit(embed=embed)
                await asyncio.sleep(EMBED_SLEEP)

            balance = wallet_balance.get(user_id, 0)
            final_embed = discord.Embed(
                title="🎉 All Footballers Revealed!",
                description=f"Your Multi-Packly has been done!\n\n**{', '.join(pulled)}**\nNew Balance: {balance}",
                color=discord.Color.green()
            )
            final_embed.set_footer(text="FootballDex MultiPacklys")
            await message.edit(embed=final_embed)

        else:
            # compact summary + attachment
            rarity_counts = Counter(str(b.rarity) for b in balls)
            specials_list = [s.name for s in specials_for_instances if s]
            try:
                sorted_balls = sorted(balls, key=lambda b: float(b.rarity), reverse=True)
            except Exception:
                sorted_balls = balls[:]
            HIGHLIGHT_LIMIT = 5
            highlights = [f"{b.country} ({b.rarity})" for b in sorted_balls[:HIGHLIGHT_LIMIT]]

            desc = ["📦 **Compact Multipackly Summary**", f"Opened: **{packs}** packs"]
            desc.append("\n**Rarity counts:**")
            for r, cnt in rarity_counts.most_common():
                desc.append(f"- {r}: {cnt}")
            if specials_list:
                desc.append(f"\n✨ **Specials obtained ({len(specials_list)}):** {', '.join(specials_list[:HIGHLIGHT_LIMIT])}")
            if highlights:
                desc.append(f"\n🏅 **Highlights (top {min(HIGHLIGHT_LIMIT, len(highlights))}):**")
                desc.extend(highlights)

            compact_embed = discord.Embed(
                title="🧾 Multipackly Summary (Compact)",
                description="\n".join(desc),
                color=discord.Color.blurple()
            )
            compact_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            compact_embed.set_footer(text="FootballDex MultiPacklys")
            await message.edit(embed=compact_embed)

            lines = []
            for idx, ball in enumerate(balls):
                regime_name = getattr(ball.cached_regime, "name", "Unknown")
                sp = specials_for_instances[idx]
                sp_text = sp.name if sp else ""
                lines.append(f"{idx+1}. {ball.country} | rarity: {ball.rarity} | card: {regime_name} | atk: {ball.attack} | hp: {ball.health} | special: {sp_text}")

            content = "\n".join(lines)
            file_bytes = io.BytesIO(content.encode("utf-8"))
            file_bytes.seek(0)
            discord_file = discord.File(fp=file_bytes, filename=SUMMARY_ATTACHMENT_NAME)

            dm_sent = False
            try:
                dm = await interaction.user.create_dm()
                dm_bytes = io.BytesIO(content.encode("utf-8"))
                dm_bytes.seek(0)
                dm_file = discord.File(fp=dm_bytes, filename=SUMMARY_ATTACHMENT_NAME)
                await dm.send(embed=compact_embed, file=dm_file)
                dm_sent = True
            except Exception:
                dm_sent = False

            if dm_sent:
                # DM succeeded, short ephemeral confirmation
                try:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="✅ Multipackly complete and results sent to your DMs",
                            description="Alfie worked to send your full results to your DMs. Highlights are shown above.",
                            color=discord.Color.green()
                        ),
                        ephemeral=True
                    )
                except Exception:
                    pass
            else:
                # DM failed -> send chunked ephemeral text (reliable & private)
                try:
                    # chunk into safe-size codeblocks for ephemeral messages
                    CHUNK_SIZE = 1900
                    MAX_CHUNKS = 8   # avoid spamming many messages; tune this as needed
                    chunks = [content[i:i+CHUNK_SIZE] for i in range(0, len(content), CHUNK_SIZE)]

                    if len(chunks) <= MAX_CHUNKS:
                        for chunk in chunks:
                            await interaction.followup.send(content=f"```{chunk}```", ephemeral=True)
                    else:
                        # too many chunks: send a preview ephemerally and post ONE public file as a last resort
                        preview = "".join(chunks[:MAX_CHUNKS-1])
                        await interaction.followup.send(
                            content=f"✅ Multipackly complete, results are long. Showing a preview (ephemeral):\n```{preview}```\nFull results attached publicly below (single message).",
                            ephemeral=True
                        )

                        # single public attachment
                        public_bytes = io.BytesIO(content.encode("utf-8"))
                        public_bytes.seek(0)
                        public_file = discord.File(fp=public_bytes, filename=SUMMARY_ATTACHMENT_NAME)
                        await interaction.channel.send("Full multipackly results (public):", file=public_file)

                except Exception:
                    # single public file if ephemeral fails for some reason
                    try:
                        public_bytes = io.BytesIO(content.encode("utf-8"))
                        public_bytes.seek(0)
                        public_file = discord.File(fp=public_bytes, filename=SUMMARY_ATTACHMENT_NAME)
                        await interaction.channel.send("Full multipackly results:", file=public_file)
                    except Exception:
                        # give up. nothing else we can do...
                        pass


    # Command to add packs to a user's wallet
    @app_commands.command(name="owners-add", description="Add packs to another user's wallet")
    async def ownerspacklyadd(self, interaction: discord.Interaction, user: discord.User, packs: int):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Check if the user issuing the command is allowed to add packs
        if interaction.user.id not in ownersid:
            await interaction.response.send_message(
                "You are not allowed to add packly's to other people or youself ❌",
                ephemeral=True
            )
            return

        # Ensure the target user has a wallet entry
        target_user_id = str(user.id)
        if target_user_id not in wallet_balance:
            wallet_balance[target_user_id] = 1  # Initialize with 1 pack if no balance exists

        # Add packs to the target user's wallet
        wallet_balance[target_user_id] += packs

        embed = discord.Embed(
            title="FootballDex Packs Added!",
            description=(
                f"{interaction.user.mention} has added **{packs}** pack(s) to {user.mention}'s wallet.\n"
                f"🪙 **{user.name}'s New Balance**: `{wallet_balance[target_user_id]} packs`"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="Packly System")
        embed.set_thumbnail(url=user.display_avatar.url)

        await interaction.response.send_message(embed=embed)
        
            # Command to remove packs from a user's wallet
    @app_commands.command(name="owners-remove", description="Remove packs from another user's wallet")
    async def ownerspacklyremove(self, interaction: discord.Interaction, user: discord.User, packs: int):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Check if the user issuing the command is allowed to remove packs
        if interaction.user.id not in ownersid:
            await interaction.response.send_message(
                "You are not allowed to remove packly's from other people or youself ❌",
                ephemeral=True
            )
            return

        # Ensure the target user has a wallet entry
        target_user_id = str(user.id)
        if target_user_id not in wallet_balance:
            wallet_balance[target_user_id] = 0  # Initialize with 0 packs if no balance exists

        # Remove packs from the target user's wallet (ensure it doesn't go below 0)
        wallet_balance[target_user_id] = max(0, wallet_balance[target_user_id] - packs)

        embed = discord.Embed(
            title="FootballDex Packs Removed!",
            description=(
                f"{interaction.user.mention} has removed **{packs}** pack(s) from {user.mention}'s wallet.\n"
                f"🪙 **{user.name}'s New Balance**: `{wallet_balance[target_user_id]} packs`"
            ),
            color=discord.Color.red()
        )
        embed.set_footer(text="Packly System")
        embed.set_thumbnail(url=user.display_avatar.url)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="gamblepack", description="Gamble your packlys for a chance to win double – or lose it all!")
    @app_commands.describe(amount="How many packs to gamble (fixed 50/50 chance)")
    async def gamblepack(self, interaction: discord.Interaction, amount: int = 1):
        user_id = str(interaction.user.id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        now = datetime.utcnow()

        if amount < 1:
            await interaction.response.send_message("You must gamble at least 1 pack.", ephemeral=True)
            return

        if amount > 100:
            await interaction.response.send_message("❌ You can only gamble up to 100 packlys at once.", ephemeral=True)
            return


        # Ensure user has balance
        if user_id not in wallet_balance:
            wallet_balance[user_id] = 0

        if wallet_balance[user_id] < amount:
            await interaction.response.send_message("❌ You don't have enough packlys to gamble that many.", ephemeral=True)
            return

        # Deduct packs immediately
        wallet_balance[user_id] -= amount

        await interaction.response.defer()

        suspense = discord.Embed(
            title=f"🎲 Gambling {amount} packly{'s' if amount > 1 else ''}...",
            description="Rolling the dice...",
            color=discord.Color.dark_grey()
        )
        suspense.set_footer(text="Good luck...")
        msg = await interaction.followup.send(embed=suspense)

        await asyncio.sleep(2)

        # Always 50/50 win chance
        result = "win" if random.choice([True, False]) else "lose"

        if result == "win":
            reward = amount * 2
            wallet_balance[user_id] += reward
            suspense.title = f"🎉 You WON {reward} packlys!"
            suspense.color = discord.Color.green()
            suspense.description = f"Luck is on your side. You risked {amount}, and won {reward}!"
        else:
            suspense.title = f"💀 You LOST your {amount} packly{'s' if amount > 1 else ''}!"
            suspense.color = discord.Color.red()
            suspense.description = "Bad luck... you lost it all."

        await msg.edit(embed=suspense)

        # Optional log
        log_channel_id = 1341228457417248940
        log_channel = self.bot.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(
                f"🎲 **{interaction.user.mention}** gambled `{amount}` packlys and **{result.upper()}**.\n"
                f"🎯 Win chance: `50%`\n"
                f"📦 New balance: `{wallet_balance[user_id]}`"
            )

    
    # Command to check wallet balance
    @app_commands.command(name="wallet", description="Check your wallet balance")
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def wallet(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Show tutorial once per user
        if user_id not in self.bot_walletturorial_seen:
            tutorial_embed = discord.Embed(
                title="Welcome To The Packlys Wallet Command!",
                description=(
                    "Use `/packs wallet` to check your packlys balance.\n"
                    "- You start with 0 Packlys.\n"
                    "- To get more packlys, you have to ask the owners of FootballDex to add them!\n"
                    "- Join **[FootballDex](https://discord.gg/footballdex) to get free packlys!**\n"
                    "- These packlys can be used for `/packs packlys` `/packs multipackly` and `/packs gamblepack`\n"
                    "Enjoy!"
                ),
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=tutorial_embed, ephemeral=True)
            self.bot_walletturorial_seen.add(user_id)
            return  # Stop here, so user reads tutorial first
        
        # Get the user's pack balance (defaults to 0 if they haven't added any packs)
        balance = wallet_balance.get(user_id, 0)
        
        embed = discord.Embed(
            title=f"{username}'s Wallet",
            description=f"You currently have **{balance}** packly(s).",
            color=discord.Color.green()
        )
        embed.set_footer(text="FootballDex Wallet")
        
        # Send the wallet balance as an embed
        await interaction.response.send_message(embed=embed, ephemeral=False)
