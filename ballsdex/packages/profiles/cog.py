import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional
from datetime import datetime, timedelta
from ballsdex.core.utils.transformers import (
    BallTransform,
    SpecialTransform,
)
from ballsdex.core.models import (
    Ball,
    balls,
    BallInstance,
    Player,
    Trade,
    Special,
)
from ballsdex.settings import settings
from ballsdex.core.bot import BallsDexBot
import ballsdex.packages.config.components as Components
from collections import defaultdict
from discord.ui import View, button, Button
from tortoise import fields
from tortoise.models import Model


class ProfileView(discord.ui.View):
    def __init__(self, bot, target_user_id, profile, interactor_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.target_user_id = target_user_id
        self.profile = profile
        self.interactor_id = interactor_id

    @discord.ui.button(label="Like", style=discord.ButtonStyle.success, emoji="👍")
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.target_user_id:
            return await interaction.response.send_message("You can't like your own profile!", ephemeral=True)

        voted = self.profile.setdefault("voted_users", {})
        if voted.get(interaction.user.id) == "like":
            del voted[interaction.user.id]
            self.profile["likes"] -= 1
            await interaction.response.send_message("👍 Like removed!", ephemeral=True)
        else:
            if voted.get(interaction.user.id) == "down":
                self.profile["downvotes"] -= 1
            voted[interaction.user.id] = "like"
            self.profile["likes"] += 1
            await interaction.response.send_message("👍 Profile liked!", ephemeral=True)
        await self.update_embed(interaction)

    @discord.ui.button(label="Downvote", style=discord.ButtonStyle.danger, emoji="👎")
    async def dislike(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.target_user_id:
            return await interaction.response.send_message("You can't dislike your own profile!", ephemeral=True)

        voted = self.profile.setdefault("voted_users", {})
        if voted.get(interaction.user.id) == "down":
            del voted[interaction.user.id]
            self.profile["downvotes"] -= 1
            await interaction.response.send_message("👎 Downvote removed!", ephemeral=True)
        else:
            if voted.get(interaction.user.id) == "like":
                self.profile["likes"] -= 1
            voted[interaction.user.id] = "down"
            self.profile["downvotes"] += 1
            await interaction.response.send_message("👎 Profile downvoted!", ephemeral=True)
        await self.update_embed(interaction)

    async def update_embed(self, interaction):
        embed = interaction.message.embeds[0]
        embed.set_footer(text=f"👍 {self.profile['likes']} | 👎 {self.profile['downvotes']}")
        await interaction.message.edit(embed=embed, view=self)


class Profiles(commands.GroupCog, name="profile"):
    def __init__(self, bot):
        self.bot = bot
        self.profiles = {}
        self.blocked_users = {}
        self.tutorial_viewed = set()

    def get_profile(self, user_id: int):
        return self.profiles.setdefault(user_id, {
            "avatar": None,
            "banner": None,
            "bio": "This user hasn't set a bio yet.",
            "specials": 0,
            "balls_total": 0,
            "balls_collected": 0,
            "likes": 0,
            "downvotes": 0,
            "voted_users": {},  # interactor_id: "like" or "down"
        })

    def is_blocked(self, viewer: int, target: int):
        return target in self.blocked_users.get(viewer, set())


    @app_commands.command(name="view", description="View a user's profile")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def view_profile(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        await interaction.response.defer()
        user = user or interaction.user

        # Blocking check
        if self.is_blocked(user.id, interaction.user.id):
            return await interaction.followup.send("You are blocked from viewing this profile.", ephemeral=True)

        player = await Player.get_or_none(discord_id=user.id)
        if not player:
            return await interaction.followup.send("That user does not have a profile yet.", ephemeral=True)

        profile = self.get_profile(user.id)
        data = await BallInstance.filter(player=player)

        days = 7 
        data_recent = await BallInstance.filter(
            player=player,
            catch_date__gte=datetime.now() - timedelta(days=days)
        )

        special_count = await BallInstance.filter(player=player, special=True).count()

        # Determine rank based on total number of balls
        total_count = len(data)
        if total_count >= 3000:
            rank = "🐐 GOAT"
        elif total_count >= 2000:
            rank = "🌟 Legend"
        elif total_count >= 1500:
            rank = "🔥 Elite"
        elif total_count >= 1000:
            rank = "🧠 Master"
        elif total_count >= 750:
            rank = "💎 Diamond"
        elif total_count >= 500:
            rank = "🏅 Gold"
        elif total_count >= 250:
            rank = "🪙 Silver"
        elif total_count >= 100:
            rank = "🥉 Bronze"
        else:
            rank = "None"

        embed = discord.Embed(
            title=f"{user.display_name}'s Profile",
            description=profile["bio"],
            color=discord.Color.green()
        )
        embed.add_field(
            name=f"🎉 Footballers Caught ({days}d)",
            value=str(len(data_recent)),
            inline=True
        )
        embed.add_field(
            name=f"🌍 Servers Caught In ({days}d)",
            value=len(set(ball.server_id for ball in data_recent if ball.server_id)),
            inline=True
        )
        embed.add_field(
            name=f"📈 Total Foootballers Caught",
            value=str(len(data)),
            inline=True
        )
        embed.add_field(
            name=f"💎 Special Footballers",
            value=special_count,
            inline=True
        )
        embed.add_field(
            name=f"🏆 Rank",
            value=rank,
            inline=True
        )
        embed.add_field(
            name=f"📅 Joined Discord",
            value=user.created_at.strftime("%B %d, %Y"),
            inline=False
        )

        embed.set_footer(text=f"👍 {profile['likes']} | 👎 {profile['downvotes']}")

        def is_valid_image_url(url: str) -> bool:
            return url.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))

        banner_url = profile["banner"]
        if banner_url and is_valid_image_url(banner_url):
            embed.set_image(url=banner_url)

        avatar_url = profile["avatar"]
        if avatar_url and is_valid_image_url(avatar_url):
            embed.set_thumbnail(url=avatar_url)
        else:
            embed.set_thumbnail(url=user.display_avatar.url)

        if not hasattr(self.bot, "profile_tutorial_shown"):
            self.bot.profile_tutorial_shown = set()
        if interaction.user.id not in self.bot.profile_tutorial_shown:
            embed.add_field(
                name="📘 First Time?",
                value=(
                    "Welcome to your profile! Here's what you can do:\n"
                    "• Customize your bio and avatar\n"
                    "• Earn likes from others\n"
                    "• View stats!\n"
                ),
                inline=False
            )
            self.bot.profile_tutorial_shown.add(interaction.user.id)

        view = ProfileView(self.bot, user.id, profile, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="change", description="Change your profile's avatar and banner")
    @app_commands.describe(avatar="URL to your profile avatar", banner="URL to your profile banner")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def change_profile(self, interaction: discord.Interaction, avatar: Optional[str] = None, banner: Optional[str] = None):
        profile = self.get_profile(interaction.user.id)

        if avatar:
            profile["avatar"] = avatar
        if banner:
            profile["banner"] = banner

        embed = discord.Embed(title="✅ Profile Updated!", color=discord.Color.blue())
        if avatar:
            embed.add_field(name="🖼️ New Avatar", value=avatar, inline=False)
        if banner:
            embed.add_field(name="🌄 New Banner", value=banner, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bio", description="Change your profile bio")
    @app_commands.describe(text="Your new bio")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def set_bio(self, interaction: discord.Interaction, text: str):
        profile = self.get_profile(interaction.user.id)
        profile["bio"] = text[:250]
        embed = discord.Embed(
            title="📝 Bio Updated!",
            description=f"New bio: {text}",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="block", description="Block a user from viewing your profile")
    async def block_user(self, interaction: discord.Interaction, user: discord.User):
        blocked = self.blocked_users.setdefault(interaction.user.id, set())
        if user.id in blocked:
            return await interaction.response.send_message("That user is already blocked.", ephemeral=True)
        blocked.add(user.id)
        await interaction.response.send_message(f"🚫 {user.mention} has been blocked from viewing your profile.", ephemeral=True)

    @app_commands.command(name="unblock", description="Unblock a user from viewing your profile")
    async def unblock_user(self, interaction: discord.Interaction, user: discord.User):
        blocked = self.blocked_users.get(interaction.user.id, set())
        if user.id not in blocked:
            return await interaction.response.send_message("That user isn't blocked.", ephemeral=True)
        blocked.remove(user.id)
        await interaction.response.send_message(f"✅ {user.mention} has been unblocked.", ephemeral=True)