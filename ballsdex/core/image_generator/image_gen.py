import os
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

if TYPE_CHECKING:
    from ballsdex.core.models import BallInstance


SOURCES_PATH = Path(os.path.dirname(os.path.abspath(__file__)), "./src")
WIDTH = 1428
HEIGHT = 2000

RECTANGLE_WIDTH = WIDTH - 40
RECTANGLE_HEIGHT = (HEIGHT // 5) * 2

CORNERS = ((0, 181), (1428, 948))
artwork_size = [b - a for a, b in zip(*CORNERS)]

# ===== TIP =====
#
# If you want to quickly test the image generation, there is a CLI tool to quickly generate
# test images locally, without the bot or the admin panel running:
#
# With Docker: "docker compose run admin-panel python3 manage.py preview > image.png"
# Without: "cd admin_panel && poetry run python3 manage.py preview"
#
# This will either create a file named "image.png" or directly display it using your system's
# image viewer. There are options available to specify the ball or the special background,
# use the "--help" flag to view all options.

title_font = ImageFont.truetype(str(SOURCES_PATH / "Ethnocentric Rg.otf"), 80)
capacity_name_font = ImageFont.truetype(str(SOURCES_PATH / "Akira Jimbo.ttf"), 110)
capacity_description_font = ImageFont.truetype(str(SOURCES_PATH / "TypoGraphica_demo.otf"), 60)
stats_font = ImageFont.truetype(str(SOURCES_PATH / "TypoGraphica_demo.otf"), 130)
credits_font = ImageFont.truetype(str(SOURCES_PATH / "demarunregular-ovpgo.ttf"), 40)

credits_color_cache = {}


def get_credit_color(image: Image.Image, region: tuple) -> tuple:
    image = image.crop(region)
    brightness = sum(image.convert("L").getdata()) / image.width / image.height  # type: ignore
    return (0, 0, 0, 255) if brightness > 100 else (255, 255, 255, 255)

    
def draw_card(ball_instance: "BallInstance", media_path: str = "./admin_panel/media/", frame_overlay: Image.Image = None):
    ball = ball_instance.countryball
    ball_health = (237, 115, 101, 255)
    ball_credits = ball.credits
    
    if special_image := ball_instance.special_card:
        image = Image.open(media_path + special_image)
        if ball_instance.specialcard and ball_instance.specialcard.credits:
            ball_credits += f" • {ball_instance.specialcard.credits}"
    else:
        image = Image.open(media_path + ball.cached_regime.background)
    image = image.convert("RGBA")
    if frame_overlay:
        frame_overlay = frame_overlay.resize(image.size)
        image = Image.alpha_composite(image, frame_overlay)
    icon = (
        Image.open(media_path + ball.cached_economy.icon).convert("RGBA")
        if ball.cached_economy
        else None
    )

    draw = ImageDraw.Draw(image)
    draw.text(
        (30, 30),
        ball.short_name or ball.country,
        font=title_font,
        stroke_width=3,
        stroke_fill=(0, 0, 0, 255),
    )
    for i, line in enumerate(textwrap.wrap(f"CODE: {ball.capacity_name}", width=30)):
        draw.text(
            (100, 1050 + 100 * i),
            line,
            font=capacity_name_font,
            fill=(230, 230, 230, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 255),
        )
    for i, line in enumerate(textwrap.wrap(ball.capacity_description, width=44)):
        draw.text(
            (80, 1160 + 60 * i),
            line,
            font=capacity_description_font,
            stroke_width=1,
            stroke_fill=(0, 0, 0, 255),
        )
    rarity = ball_instance.countryball.rarity
    draw.text(
    (1280, 10),
    str(rarity),
    font=stats_font,
    fill=(255, 191, 0),
    stroke_width=5,
    stroke_fill=(0, 0, 0, 255),
    anchor="ra",
    )
    draw.text(
        (301, 1615),
        str(ball_instance.health),
        font=stats_font,
        fill=ball_health,
        stroke_width=1,
        stroke_fill=(0, 0, 0, 255),
    )
    draw.text(
        (1142, 1615),
        str(ball_instance.attack),
        font=stats_font,
        fill=(252, 194, 76, 255),
        stroke_width=1,
        stroke_fill=(0, 0, 0, 255),
        anchor="ra",
    )
    draw.text(
        (30, 1870),
        # Modifying the line below is breaking the licence as you are removing credits
        # If you don't want to receive a DMCA, just don't
        "Property & Licensed by El Laggron\n" f"Owners: Alfie,Snape",
        font=credits_font,
        fill=(230, 230, 230, 255),
        stroke_width=0,
        stroke_fill=(255, 255, 255, 255),
    )

    artwork = Image.open(media_path + ball.collection_card).convert("RGBA")
    image.paste(ImageOps.fit(artwork, artwork_size), CORNERS[0])  # type: ignore

    if icon:
        icon = ImageOps.fit(icon, (170, 170))
        image.paste(icon, (1142, 1030), mask=icon)
        icon.close()
    artwork.close()

    return image, {"format": "WEBP"}