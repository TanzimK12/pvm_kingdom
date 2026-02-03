from PIL import Image, ImageDraw, ImageFont
import os
from datetime import datetime
import subprocess

# ---------------- SETTINGS ----------------

OSRS_FONT_PATH = os.path.join("fonts", "osrs.ttf")

# Base map (no colours) and coloured reference map
BASE_MAP_PATH = "PvM_kingdom_nocolor.png"
COLORED_MAP_PATH = "PvM_kingdom_w22.png"

OUTPUT_PATH = "pvm_static_output.png"

REGION_NAMES = [
    "Kourend",
    "Varlamore",
    "Fremennik",
    "Asgarnia",
    "Wilderness",
    "Morytania",
    "Desert",
    "Misthalin",
    "Karamja",
    "Kandarin",
    "Tirannwn",
]

# Region hex colours from your painted map
REGION_HEX = {
    "Kourend":   "#8035f0",
    "Varlamore": "#c3c3c3",
    "Fremennik": "#995f40",
    "Asgarnia":  "#ff7f27",
    "Wilderness": "#000000",
    "Morytania": "#3f9456",
    "Desert":    "#fff200",
    "Misthalin": "#72c111",
    "Karamja":   "#b5e61d",
    "Kandarin":  "#ff5151",
    "Tirannwn":  "#22b14c",
}

# Scoreboard / text colours
COLOR_BG_BAR = (15, 23, 42, 220)
COLOR_TEXT = (240, 240, 240, 255)
COLOR_GRAY = (160, 160, 160, 255)
COLOR_BLUE = (37, 99, 235, 255)
COLOR_RED = (220, 38, 38, 255)

# Region fill colours by controlling team (with transparency)
TEAM_FILL = {
    "Blue": (37, 99, 235, 140),
    "Red":  (220, 38, 38, 140),
    "None": (75, 85, 99, 140),
}

# ---------------- FONT LOADER ----------------

def load_font(size: int):
    if os.path.exists(OSRS_FONT_PATH):
        try:
            return ImageFont.truetype(OSRS_FONT_PATH, size)
        except Exception:
            pass

    possible = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/tahomabd.ttf",
    ]
    for path in possible:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    return ImageFont.load_default()

# ---------------- TEXT SIZE HELPER ----------------

def text_size(draw: ImageDraw.ImageDraw, text: str, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]

# ---------------- FAKE DATA ----------------

def get_example_region_totals():
    fake = {}
    i = 0
    for region in REGION_NAMES:
        fake[region] = {
            "blue": (i * 2) % 7,
            "red": (i * 3) % 5,
            "control": "Blue" if i % 3 == 0 else ("Red" if i % 3 == 1 else "None"),
        }
        i += 1
    return fake

# ---------------- UTILS ----------------

def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

# ---------------- IMAGE GENERATION ----------------

def generate_static_map():
    base = Image.open(BASE_MAP_PATH).convert("RGBA")
    colored = Image.open(COLORED_MAP_PATH).convert("RGB")

    if base.size != colored.size:
        raise ValueError("Base and coloured maps must be same size")

    width, height = base.size

    region_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_pixels = region_overlay.load()
    colored_pixels = colored.load()

    data = get_example_region_totals()

    color_to_fill = {}
    for region, hex_color in REGION_HEX.items():
        region_rgb = hex_to_rgb(hex_color)
        control = data.get(region, {}).get("control", "None")
        color_to_fill[region_rgb] = TEAM_FILL.get(control, TEAM_FILL["None"])

    for y in range(height):
        for x in range(width):
            c = colored_pixels[x, y]
            if c in color_to_fill:
                overlay_pixels[x, y] = color_to_fill[c]

    combined = Image.alpha_composite(base, region_overlay)
    final = combined.convert("RGB")
    final.save(OUTPUT_PATH)

    print(f"Generated: {OUTPUT_PATH}")

# ---------------- GITHUB UPLOAD ----------------

def upload_to_github():
    try:
        subprocess.run(["git", "add", OUTPUT_PATH], check=True)
        subprocess.run(
            ["git", "commit", "-m", "Auto-update PvM static map"],
            check=True
        )
        subprocess.run(["git", "push"], check=True)
        print("Uploaded image to GitHub.")
    except subprocess.CalledProcessError as e:
        print("Git upload failed:", e)

# ---------------- MAIN ----------------

if __name__ == "__main__":
    generate_static_map()
    upload_to_github()
