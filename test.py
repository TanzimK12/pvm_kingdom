import discord
from discord import app_commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from collections import defaultdict

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

GUILD_ID = 1399914354769399911  # Your provided guild ID (int)

tile_activities = load_tile_activities()

def get_tile_choices():
    return [app_commands.Choice(name=tile, value=tile) for tile in tile_activities.keys()]

async def activity_autocomplete(interaction: discord.Interaction, current: str):
    tile_name = None
    for option in interaction.data.get("options", []):
        if option["name"] == "tile_name":
            tile_name = option["value"]
            break
    if tile_name is None or tile_name not in tile_activities:
        return []
    activities = tile_activities[tile_name]
    return [
        app_commands.Choice(name=act, value=act)
        for act in activities if current.lower() in act.lower()
    ][:25]

@bot.event
async def on_ready():
    global tile_activities
    tile_activities = load_tile_activities()
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Synced commands to guild {GUILD_ID}")
    print(f"Bot is online as {bot.user}")

@tree.command(name="submit", description="Submit a tile completion with optional image attachment", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    tile_name="Choose the tile",
    activity_name="Choose the activity",
    amount="Amount completed",
    image="Attach an image"
)
@app_commands.choices(tile_name=get_tile_choices())
@app_commands.autocomplete(activity_name=activity_autocomplete)
async def submit(
    interaction: discord.Interaction,
    tile_name: str,
    activity_name: str,
    amount: int,
    image: discord.Attachment = None
):
    if tile_name not in tile_activities:
        await interaction.response.send_message(
            f"❌ Invalid tile name. Choose from: {', '.join(tile_activities.keys())}", ephemeral=True)
        return
    if activity_name not in tile_activities[tile_name]:
        await interaction.response.send_message(
            f"❌ Invalid activity for {tile_name}. Choose from: {', '.join(tile_activities[tile_name])}", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    image_url = image.url if image else "None"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    submission_sheet.append_row([
        timestamp,
        str(interaction.user),
        tile_name,
        activity_name,
        amount,
        image_url,
    ])

    await interaction.followup.send(
        f"✅ Submission complete!\n"
        f"**Tile:** {tile_name}\n"
        f"**Activity:** {activity_name}\n"
        f"**Amount:** {amount}\n"
        f"**Image URL:** {image_url}",
        ephemeral=True
    )

bot.run("MTM5OTkxNDUwNjI1MTE0MTE2Mg.GLsB4B.soi6qDxnSLQDDr2IN4vvVrqC43SOa6EOpp81HQ")
