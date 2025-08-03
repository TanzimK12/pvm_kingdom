import discord
from discord import app_commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("discord_token")

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
gs_client = gspread.authorize(creds)

tiles_activities_sheet = gs_client.open("Bingo Example").worksheet("TilesActivities")
submission_sheet = gs_client.open("Bingo Example").worksheet("Submissions")

def load_tile_activities():
    rows = tiles_activities_sheet.get_all_values()
    tile_map = defaultdict(list)
    for row in rows[1:]:  # Skip header
        if len(row) < 2:
            continue
        tile, activity = row[0].strip(), row[1].strip()
        if activity not in tile_map[tile]:
            tile_map[tile].append(activity)
    return dict(tile_map)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

tile_activities = load_tile_activities()

async def tile_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=tile, value=tile)
        for tile in tile_activities.keys()
        if current.lower() in tile.lower()
    ][:25]

async def activity_autocomplete(interaction: discord.Interaction, current: str):
    tile_name = None
    for option in interaction.data.get("options", []):
        if option["name"] == "tile_name":
            tile_name = option["value"]
            break
    if not tile_name or tile_name not in tile_activities:
        return []
    return [
        app_commands.Choice(name=activity, value=activity)
        for activity in tile_activities[tile_name]
        if current.lower() in activity.lower()
    ][:25]

@bot.event
async def on_ready():
    global tile_activities
    tile_activities = load_tile_activities()
    await tree.sync()  # Global command sync
    print("Synced commands globally")
    print(f"Bot is online as {bot.user}")

@tree.command(
    name="submit",
    description="Submit a tile completion with required image"
)
@app_commands.describe(
    tile_name="Choose the tile",
    activity_name="Choose the activity",
    amount="Amount completed",
    image="Attach an image (required)"
)
@app_commands.autocomplete(tile_name=tile_autocomplete, activity_name=activity_autocomplete)
async def submit(
    interaction: discord.Interaction,
    tile_name: str,
    activity_name: str,
    amount: int,
    image: discord.Attachment,
):
    if tile_name not in tile_activities:
        await interaction.response.send_message(
            f"❌ Invalid tile name. Choose from: {', '.join(tile_activities.keys())}",
            ephemeral=True,
        )
        return
    if activity_name not in tile_activities[tile_name]:
        await interaction.response.send_message(
            f"❌ Invalid activity for {tile_name}. Choose from: {', '.join(tile_activities[tile_name])}",
            ephemeral=True,
        )
        return
    if amount <= 0:
        await interaction.response.send_message(
            "❌ Amount must be greater than 0.",
            ephemeral=True,
        )
        return
    if not image:
        await interaction.response.send_message(
            "❌ You must attach an image.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_display = interaction.user.nick or interaction.user.name
    guild_id = str(interaction.guild.id) if interaction.guild else "DM"
    image_url = image.url

    try:
        submission_sheet.append_row(
            [timestamp, guild_id, user_display, tile_name, activity_name, amount, image_url]
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to record submission: {e}", ephemeral=True)
        return

    await interaction.followup.send(
        f"✅ Submission complete!\n"
        f"**Tile:** {tile_name}\n"
        f"**Activity:** {activity_name}\n"
        f"**Amount:** {amount}\n"
        f"**Image URL:** {image_url}\n"
        f"**Server ID:** {guild_id}",
        ephemeral=True,
    )

bot.run(DISCORD_TOKEN)
