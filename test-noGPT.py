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

# Cached spreadsheet references
spreadsheet = gs_client.open("SeptemberPVMEvent")
tiles_activities_sheet = spreadsheet.worksheet("TilesActivities")
submission_sheet = spreadsheet.worksheet("Submissions")
team_sheet = spreadsheet.worksheet("TeamDetails")
competition_info_sheet = spreadsheet.worksheet("CompetitionInformation")

# Load tile/activity data
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

# Read mode from CompetitionInformation!B1
def is_server_mode():
    try:
        mode_value = competition_info_sheet.acell("B1").value
        return mode_value.strip().lower() == "true"
    except Exception as e:
        print(f"Error reading competition mode: {e}")
        return True  # default to server mode

# Get approval, approved, and denied channel IDs based on guild/channel ID
def get_channel_ids_for_submission(submission_id: str):
    try:
        rows = team_sheet.get_all_values()
        for row in rows[1:]:
            if len(row) >= 5 and row[1] == submission_id:
                return {
                    "submission_id": row[1],  # guild ID in server mode, channel ID in channel mode
                    "approval": int(row[2]),
                    "approved": int(row[3]),
                    "denied": int(row[4])
                }
    except Exception as e:
        print(f"❌ Error retrieving channel IDs from TeamDetails: {e}")
    return None

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

tile_activities = load_tile_activities()
submission_message_guild_map = {}  # Tracks which submission came from which source

# Autocomplete handlers
async def tile_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=tile, value=tile)
        for tile in tile_activities.keys()
        if current.lower() in tile.lower()
    ][:25]

async def activity_autocomplete(interaction: discord.Interaction, current: str):
    focused = interaction.namespace
    tile_name = getattr(focused, "tile_name", None)

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
    await tree.sync()
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
    # ❌ Reject DMs
    if interaction.guild is None:
        await interaction.response.send_message("❌ Submissions must be made in a server channel, not DMs.", ephemeral=True)
        return

    # Determine mode and lookup ID
    is_server = is_server_mode()
    if is_server:
        submission_lookup_id = str(interaction.guild.id)   # SERVER MODE: use guild ID
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
        if not channel_info:
            await interaction.response.send_message("❌ This server is not registered for submissions.", ephemeral=True)
            return
        # ✅ In server mode, allow ANY channel in the guild (no exact-channel check)
    else:
        submission_lookup_id = str(interaction.channel.id) # CHANNEL MODE: use channel ID
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
        if not channel_info:
            await interaction.response.send_message("❌ This channel is not registered for submissions.", ephemeral=True)
            return
        # ⛔ In channel mode, enforce exact designated channel
        expected_channel_id = int(channel_info["submission_id"])
        if interaction.channel.id != expected_channel_id:
            await interaction.response.send_message("❌ You can only submit from your designated team channel.", ephemeral=True)
            return

    # Validate inputs
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
        await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)
        return
    if not image:
        await interaction.response.send_message("❌ You must attach an image.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_display = interaction.user.nick or interaction.user.name
    image_url = image.url

    # Record in Google Sheets
    try:
        submission_sheet.append_row(
            [timestamp, submission_lookup_id, user_display, tile_name, activity_name, amount, image_url]
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to record submission: {e}", ephemeral=True)
        return

    # Fetch dynamic channel IDs
    approval_channel = bot.get_channel(channel_info["approval"])
    if not approval_channel:
        await interaction.followup.send("❌ Approval channel not found or bot missing access.", ephemeral=True)
        return

    # Send to approval channel
    embed = discord.Embed(title=f"📝 Submission from {user_display}", color=discord.Color.orange())
    embed.add_field(name="Tile", value=tile_name, inline=True)
    embed.add_field(name="Activity", value=activity_name, inline=True)
    embed.add_field(name="Amount", value=str(amount), inline=True)
    embed.add_field(name="Submitted By", value=user_display, inline=True)
    embed.set_image(url=image_url)
    embed.set_footer(text=f"Submitted at {timestamp}")

    approval_message = await approval_channel.send(embed=embed)
    await approval_message.add_reaction("✅")
    await approval_message.add_reaction("❌")

    # Store mapping
    submission_message_guild_map[approval_message.id] = submission_lookup_id

    await interaction.followup.send("✅ Submission received and sent for approval.", ephemeral=True)

@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    message_id = payload.message_id
    submission_id = submission_message_guild_map.get(message_id)
    if not submission_id:
        return

    channel_info = get_channel_ids_for_submission(submission_id)
    if not channel_info:
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return

    if not message.embeds:
        return

    embed = message.embeds[0]
    emoji = str(payload.emoji)

    if emoji == "✅":
        approved_channel = bot.get_channel(channel_info["approved"])
        if approved_channel:
            await approved_channel.send(embed=embed)
        await message.delete()

    elif emoji == "❌":
        denied_channel = bot.get_channel(channel_info["denied"])
        if denied_channel:
            await denied_channel.send(embed=embed)
        await message.delete()

bot.run(DISCORD_TOKEN)
