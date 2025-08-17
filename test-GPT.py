import discord
from discord import app_commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv
import os
import aiohttp
import base64
import re
import json
from rapidfuzz import fuzz
from openai import OpenAI

# -------------------- ENV --------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("discord_token")
OPENAI_API_KEY = os.getenv("openai_api_key") or os.getenv("OPENAI_API_KEY")
TEST_GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # used for instant per-guild sync

# OpenAI client (v1.x)
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------- Google Sheets --------------------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
gs_client = gspread.authorize(creds)

# Cached spreadsheet references
spreadsheet = gs_client.open("MainBingoSheet")
tiles_activities_sheet = spreadsheet.worksheet("TilesActivities")
submission_sheet = spreadsheet.worksheet("Submissions")
team_sheet = spreadsheet.worksheet("TeamDetails")
competition_info_sheet = spreadsheet.worksheet("CompetitionInformation")

# Ensure/attach APICostLog sheet
def get_or_create_cost_log_sheet():
    try:
        return spreadsheet.worksheet("APICostLog")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="APICostLog", rows=1000, cols=10)
        ws.append_row(["Timestamp", "User", "Model", "Images", "PromptTokens", "CompletionTokens", "CostUSD", "Notes"])
        return ws

api_cost_log_sheet = get_or_create_cost_log_sheet()

# -------------------- Pricing (Feb 2025) --------------------
MODEL_NAME = "gpt-4o-mini"
PRICE_IMAGE_PER_IMAGE = 0.00255   # USD per image
PRICE_INPUT_PER_1K = 0.0003       # USD per 1K input tokens
PRICE_OUTPUT_PER_1K = 0.0006      # USD per 1K output tokens

# -------------------- Data loaders --------------------
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

def is_server_mode():
    try:
        mode_value = competition_info_sheet.acell("B1").value
        return mode_value.strip().lower() == "true"
    except Exception as e:
        print(f"Error reading competition mode: {e}")
        return True  # default to server mode

def get_channel_ids_for_submission(submission_id: str):
    try:
        rows = team_sheet.get_all_values()
        for row in rows[1:]:
            if len(row) >= 5 and row[1] == submission_id:
                return {
                    "submission_id": row[1],  # guild ID (server mode) or channel ID (channel mode)
                    "approval": int(row[2]),
                    "approved": int(row[3]),
                    "denied": int(row[4])
                }
    except Exception as e:
        print(f"❌ Error retrieving channel IDs from TeamDetails: {e}")
    return None

# -------------------- OpenAI Vision helper (items + RSN in ONE call) --------------------
async def analyze_image_with_openai(image_url: str):
    """
    Fetch the image, ask OpenAI to return JSON:
      {"items": ["Item A", "Item B", ...], "rsn": "Player Name or UNKNOWN"}
    Returns: (items: list[str], prompt_tokens: int, completion_tokens: int, rsn: str)
    """
    # Download image bytes
    async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as resp:
            if resp.status != 200:
                print(f"[OpenAI] failed to fetch image ({resp.status})")
                return [], 0, 0, "UNKNOWN"
            image_bytes = await resp.read()

    # Encode as data URL
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/png;base64,{b64}"

    prompt = (
        "You are analyzing a RuneLite screenshot from Old School RuneScape.\n"
        "TASKS:\n"
        "1) Extract the exact in-game player name (RSN) of the person who received the drop/loot.\n"
        "   - Prefer names shown in the kill/loot line in the chatbox or the loot interface.\n"
        "   - If no clear player name is present, set RSN to \"UNKNOWN\".\n"
        "2) List only the names of all unique RuneScape items visible (no duplicates, no brands, no generic words).\n"
        "OUTPUT STRICTLY AS JSON with keys 'rsn' and 'items', like:\n"
        "{\"rsn\":\"Exact Name\", \"items\":[\"Item 1\",\"Item 2\"]}\n"
        "Do not include any extra text outside the JSON."
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=350,
        )
        content = (resp.choices[0].message.content or "").strip()

        # Parse JSON safely
        rsn = "UNKNOWN"
        items = []
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                rsn = str(data.get("rsn", "UNKNOWN")).strip() or "UNKNOWN"
                raw_items = data.get("items", [])
                if isinstance(raw_items, list):
                    # De-dupe (case-insensitive)
                    seen = set()
                    for p in raw_items:
                        if not isinstance(p, str):
                            continue
                        k = p.strip().lower()
                        if k and k not in seen:
                            items.append(p.strip())
                            seen.add(k)
        except Exception:
            # Fallback: try comma split if model didn't obey JSON (rare)
            parts = [p.strip() for p in content.split(",") if p.strip()]
            for p in parts:
                if p.lower().startswith("rsn:"):
                    rsn = p.split(":", 1)[1].strip() or "UNKNOWN"
            for p in parts:
                if "rsn:" in p.lower():
                    continue
                if p:
                    items.append(p)

        # usage fields
        usage = getattr(resp, "usage", None) or {}
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or usage.get("completion_tokens", 0) or 0)

        return items, prompt_tokens, completion_tokens, rsn

    except Exception as e:
        print(f"GPT error: {e}")
        msg = str(e).lower()
        if "insufficient_quota" in msg or "429" in msg or "quota" in msg:
            raise RuntimeError("OPENAI_QUOTA")
        return [], 0, 0, "UNKNOWN"

# -------------------- Fuzzy/Name helpers --------------------
ALIAS_HINTS = {
    "toa": "tombs of amascut",
    "cox": "chambers of xeric",
    "tob": "theatre of blood",
    "zcb": "zaryte crossbow",
    "dwh": "dragon warhammer",
    "sra": "soulreaper axe",
}

def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def best_match_tile_activity(detected_items, tile_activities, score_threshold=88):
    """
    Fuzzy match detected item strings to the whitelist of tile names + activities.
    Returns (matched_tile, matched_activity, debug_rows)
    """
    candidates = []
    for tile, acts in tile_activities.items():
        candidates.append((_norm(tile), tile, None))
        for act in acts:
            candidates.append((_norm(act), tile, act))

    expanded = set()
    for raw in detected_items:
        n = _norm(raw)
        if not n:
            continue
        expanded.add(n)
        if n in ALIAS_HINTS:
            expanded.add(_norm(ALIAS_HINTS[n]))
        for key, val in ALIAS_HINTS.items():
            if key in n:
                expanded.add(_norm(val))

    best = None
    debug_rows = []
    for det in expanded:
        for cand_label, tile, act in candidates:
            score = fuzz.token_set_ratio(det, cand_label)
            if score >= 60:
                debug_rows.append((score, det, cand_label, tile, act))
            if score >= score_threshold:
                if best is None or score > best[0]:
                    best = (score, tile, act, det, cand_label)

    debug_rows.sort(reverse=True, key=lambda x: x[0])
    debug_rows = debug_rows[:10]

    if best:
        chosen_tile = best[1]
        chosen_act = best[2] or (tile_activities[best[1]][0] if tile_activities[best[1]] else None)
        return chosen_tile, chosen_act, debug_rows
    return None, None, debug_rows

def _norm_name_for_compare(s: str) -> str:
    # Lower, remove non-alphanumerics, collapse spaces/underscores
    s = s.lower()
    s = s.replace("_", " ")
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def names_match(rsn: str, discord_name: str, threshold: int = 90) -> bool:
    """
    Returns True if names are effectively the same.
    Uses strict normalize + a fuzzy backup in case of tiny typos.
    """
    a = _norm_name_for_compare(rsn)
    b = _norm_name_for_compare(discord_name)
    if not a or not b:
        return False
    if a == b:
        return True
    return fuzz.ratio(a, b) >= threshold

# -------------------- Discord bot setup --------------------
intents = discord.Intents.default()
intents.message_content = True  # not required for slash, but fine
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

tile_activities = load_tile_activities()
submission_message_guild_map = {}  # Tracks which submission came from which source

# -------- Autocomplete (manual command) --------
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

    # ---------- Per-guild resync patch (instant) ----------
    if TEST_GUILD_ID:
        try:
            guild_obj = discord.Object(id=int(TEST_GUILD_ID))
            tree.clear_commands(guild=guild_obj)
            tree.copy_global_to(guild=guild_obj)
            await tree.sync(guild=guild_obj)
            print(f"✅ Per-guild commands resynced for {TEST_GUILD_ID}")
        except Exception as e:
            print(f"❌ Per-guild sync failed: {e}")

    # ---------- Global sync (may take a few minutes to propagate) ----------
    try:
        await tree.sync()
        print("✅ Global commands synced")
    except Exception as e:
        print(f"❌ Global sync failed: {e}")

    print(f"Bot is online as {bot.user}")

# -------- Manual submit (server-mode any channel; channel-mode exact channel) --------
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
    if interaction.guild is None:
        await interaction.response.send_message("❌ Submissions must be made in a server channel, not DMs.", ephemeral=True)
        return

    # Mode & lookup rules
    if is_server_mode():
        submission_lookup_id = str(interaction.guild.id)   # SERVER MODE: use guild ID
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
        if not channel_info:
            await interaction.response.send_message("❌ This server is not registered for submissions.", ephemeral=True)
            return
        # In server mode: allow ANY channel (no exact-channel check)
    else:
        submission_lookup_id = str(interaction.channel.id) # CHANNEL MODE: use channel ID
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
        if not channel_info:
            await interaction.response.send_message("❌ This channel is not registered for submissions.", ephemeral=True)
            return
        # Enforce exact channel in channel mode
        expected_channel_id = int(channel_info["submission_id"])
        if interaction.channel.id != expected_channel_id:
            await interaction.response.send_message("❌ You can only submit from your designated team channel.", ephemeral=True)
            return

    # Validate inputs
    if tile_name not in tile_activities:
        await interaction.response.send_message(
            f"❌ Invalid tile name. Choose from: {', '.join(tile_activities.keys())}",
            ephemeral=True,
        ); return
    if activity_name not in tile_activities[tile_name]:
        await interaction.response.send_message(
            f"❌ Invalid activity for {tile_name}. Choose from: {', '.join(tile_activities[tile_name])}",
            ephemeral=True,
        ); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True); return
    if not image:
        await interaction.response.send_message("❌ You must attach an image.", ephemeral=True); return

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
        await interaction.followup.send(f"❌ Failed to record submission: {e}", ephemeral=True); return

    # Send to approval channel
    approval_channel = bot.get_channel(channel_info["approval"])
    if not approval_channel:
        await interaction.followup.send("❌ Approval channel not found or bot missing access.", ephemeral=True); return

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

    submission_message_guild_map[approval_message.id] = submission_lookup_id
    await interaction.followup.send("✅ Submission received and sent for approval.", ephemeral=True)

# -------- Auto submit (OpenAI Vision + RSN filter + Cost Logging) --------
@tree.command(
    name="auto_submit",
    description="Auto-detect tile/activity from the image and submit (RSN check + logs API cost)"
)
@app_commands.describe(
    amount="Amount completed",
    image="Attach an image (required)"
)
async def auto_submit(
    interaction: discord.Interaction,
    amount: int,
    image: discord.Attachment,
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Submissions must be made in a server channel, not DMs.", ephemeral=True)
        return

    # Mode & lookup rules
    if is_server_mode():
        submission_lookup_id = str(interaction.guild.id)
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
        if not channel_info:
            await interaction.response.send_message("❌ This server is not registered for submissions.", ephemeral=True)
            return
    else:
        submission_lookup_id = str(interaction.channel.id)
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
        if not channel_info:
            await interaction.response.send_message("❌ This channel is not registered for submissions.", ephemeral=True)
            return
        expected_channel_id = int(channel_info["submission_id"])
        if interaction.channel.id != expected_channel_id:
            await interaction.response.send_message("❌ You can only submit from your designated team channel.", ephemeral=True)
            return

    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True); return
    if not image:
        await interaction.response.send_message("❌ You must attach an image.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)

    image_url = image.url
    user_display = interaction.user.nick or interaction.user.name
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Analyze image with OpenAI (items + rsn in one call)
    try:
        detected_items, prompt_tokens, completion_tokens, rsn = await analyze_image_with_openai(image_url)
    except RuntimeError as e:
        if str(e) == "OPENAI_QUOTA":
            await interaction.followup.send(
                "⚠ OpenAI vision analysis is temporarily unavailable (API quota exceeded). "
                "Please try again later or use `/submit` to enter the tile/activity manually.",
                ephemeral=True
            ); return
        raise

    if not detected_items:
        await interaction.followup.send(
            "❌ I couldn't confidently detect any OSRS items in that image. "
            "Try a clearer screenshot, or use `/submit` to specify the tile/activity.",
            ephemeral=True
        ); return

    # -------- RSN vs Discord name filter --------
    discord_name = user_display
    if rsn and rsn.upper() != "UNKNOWN":
        if not names_match(rsn, discord_name, threshold=90):
            await interaction.followup.send(
                f"❌ This drop looks credited to **{rsn}**, which doesn’t match your Discord name **{discord_name}**.\n"
                "If this is your alt, please change your Discord nickname to that RSN or ask an admin to add a mapping.",
                ephemeral=True
            ); return
    else:
        await interaction.followup.send(
            "❌ I couldn't find a clear RuneScape name (RSN) in the screenshot to verify ownership.",
            ephemeral=True
        ); return

    # Fuzzy match to TilesActivities
    matched_tile, matched_activity, dbg = best_match_tile_activity(detected_items, tile_activities, score_threshold=88)
    print("[DEBUG] Fuzzy candidates (top 10):")
    for row in dbg:
        score, det, cand_label, tile, act = row
        print(f"  score={score} | det='{det}' -> cand='{cand_label}' | tile='{tile}' | act='{act}'")

    if not matched_tile or not matched_activity:
        await interaction.followup.send(
            "❌ Could not auto-match this image to a known tile/activity.\n\n"
            f"**Detected:** {', '.join(detected_items)}\n"
            "Tip: Adjust your `TilesActivities` names or add common aliases.",
            ephemeral=True
        ); return

    # Record in Google Sheets (Submissions)
    try:
        submission_sheet.append_row(
            [timestamp, submission_lookup_id, user_display, matched_tile, matched_activity, amount, image_url]
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to record submission: {e}", ephemeral=True); return

    # -------- Cost logging as NUMBERS (no apostrophes) --------
    images_used = 1
    cost_images = images_used * PRICE_IMAGE_PER_IMAGE  # float
    cost_prompt = (prompt_tokens / 1000.0) * PRICE_INPUT_PER_1K  # float
    cost_completion = (completion_tokens / 1000.0) * PRICE_OUTPUT_PER_1K  # float
    total_cost = round(cost_images + cost_prompt + cost_completion, 6)  # float

    notes = f"RSN: {rsn} | Detected: {', '.join(detected_items)}"
    try:
        api_cost_log_sheet.append_row([
            timestamp,                 # Timestamp
            user_display,              # User
            MODEL_NAME,                # Model
            images_used,               # Images (int)
            int(prompt_tokens),        # PromptTokens (int)
            int(completion_tokens),    # CompletionTokens (int)
            total_cost,                # CostUSD (float, numeric)
            notes                      # Notes
        ])
    except Exception as e:
        print(f"[WARN] Failed to write APICostLog row: {e}")

    # Send to approval
    approval_channel = bot.get_channel(channel_info["approval"])
    if not approval_channel:
        await interaction.followup.send("❌ Approval channel not found or bot missing access.", ephemeral=True); return

    embed = discord.Embed(title=f"📝 Auto Submission from {user_display}", color=discord.Color.blurple())
    embed.add_field(name="Tile", value=matched_tile, inline=True)
    embed.add_field(name="Activity", value=matched_activity, inline=True)
    embed.add_field(name="Amount", value=str(amount), inline=True)
    embed.add_field(name="Submitted By", value=user_display, inline=True)
    embed.add_field(name="🧠 Detected Items", value=", ".join(detected_items) or "None", inline=False)
    embed.add_field(name="👤 RSN", value=rsn, inline=True)
    embed.add_field(name="💵 Cost (est.)", value=f"${total_cost:.4f}", inline=True)
    embed.set_image(url=image_url)
    embed.set_footer(text=f"Submitted at {timestamp}")

    approval_message = await approval_channel.send(embed=embed)
    await approval_message.add_reaction("✅")
    await approval_message.add_reaction("❌")

    submission_message_guild_map[approval_message.id] = submission_lookup_id
    await interaction.followup.send(
        f"✅ Auto-matched to `{matched_tile}` / `{matched_activity}` and sent for approval.",
        ephemeral=True
    )

# -------- Admin command to force resync --------
@tree.command(
    name="resync",
    description="Force a manual sync of all slash commands"
)
@app_commands.checks.has_permissions(administrator=True)
async def resync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        # Per-guild sync (instant, if configured)
        if TEST_GUILD_ID:
            try:
                guild_obj = discord.Object(id=int(TEST_GUILD_ID))
                tree.clear_commands(guild=guild_obj)
                tree.copy_global_to(guild=guild_obj)
                await tree.sync(guild=guild_obj)
                await interaction.followup.send(f"✅ Per-guild commands resynced for `{TEST_GUILD_ID}`", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ Per-guild sync failed: {e}", ephemeral=True)
                return

        # Global sync
        try:
            await tree.sync()
            await interaction.followup.send("✅ Global commands synced", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Global sync failed: {e}", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Resync error: {e}", ephemeral=True)

# -------- Reaction handler --------
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
