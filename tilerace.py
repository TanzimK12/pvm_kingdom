# ==================== Windows asyncio fix ====================
import sys, asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==================== Imports ====================
import logging
import os
import re
from datetime import datetime
from collections import defaultdict

import discord
from discord import app_commands, ui

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# ==================== Logging ====================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bingo-bot")

# ==================== ENV ====================
load_dotenv()
DISCORD_TOKEN = os.getenv("discord_token")
SHEET_NAME = os.getenv("BINGO_SHEET_NAME", "SeptemberPVMEvent")
TEST_GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional: per-guild fast sync

# ==================== DISCORD (no privileged intents) ====================
intents = discord.Intents.default()
intents.guilds = True  # minimal
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ==================== Runtime state / health ====================
HEALTH = {
    "sheets_ready": False,
    "last_error": "",
    "boot_started_at": datetime.now().strftime("%H:%M:%S"),
}

# ==================== Google Sheets (lazy-initialized) ====================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

gs_client = None
spreadsheet = None
tiles_activities_sheet = None
submission_sheet = None
team_sheet = None
competition_info_sheet = None
compiled_messages_sheet = None  # NEW

# Data caches
tile_names: list[str] = []            # unique tile names (column A)
tile_items: dict[str, list[str]] = {} # tile -> items (from column C)
compiled_tile_numbers: list[str] = [] # NEW: column A of Compiled Messages by Team
SUBMISSION_HEADER: list[str] | None = None  # cached header for Submissions

def split_items_field(s: str) -> list[str]:
    """Split on comma/semicolon/real newline; trim & dedupe case-insensitively."""
    if not s:
        return []
    parts = re.split(r"\s*(?:[,;]|\r?\n)\s*", s.strip())
    out, seen = [], set()
    for p in parts:
        if p and p.lower() not in seen:
            out.append(p)
            seen.add(p.lower())
    return out

def _load_from_tiles_sheet():
    """
    TilesActivities columns:
      A: Tile
      B: Activity (ignored)
      C: Items (comma/semicolon/newline separated)
    """
    rows = tiles_activities_sheet.get_all_values()
    names_ordered: list[str] = []
    seen_names: set[str] = set()
    items_map: dict[str, list[str]] = defaultdict(list)

    for row in rows[1:]:
        if not row:
            continue
        tile = (row[0] if len(row) >= 1 else "").strip()
        items_raw = (row[2] if len(row) >= 3 else "").strip()
        if not tile:
            continue

        key = tile.lower()
        if key not in seen_names:
            names_ordered.append(tile)
            seen_names.add(key)

        for it in split_items_field(items_raw):
            if it.lower() not in {x.lower() for x in items_map[tile]}:
                items_map[tile].append(it)

    # Cap to Discord select max 25
    for t, arr in list(items_map.items()):
        items_map[t] = arr[:25]

    return names_ordered, dict(items_map)

def _load_compiled_tile_numbers() -> list[str]:
    """Load unique tile numbers from column A of 'Compiled Messages by Team'."""
    try:
        rows = compiled_messages_sheet.get_all_values()
    except Exception:
        return []
    out, seen = [], set()
    for row in rows[1:]:
        if not row:
            continue
        t = (row[0] if len(row) >= 1 else "").strip()
        if t and t.lower() not in seen:
            out.append(t)
            seen.add(t.lower())
    return out

async def init_sheets_with_retry(max_tries: int = 5):
    """Initialize gspread + worksheets + caches (non-blocking)."""
    global gs_client, spreadsheet, tiles_activities_sheet, submission_sheet, team_sheet, competition_info_sheet
    global tile_names, tile_items, SUBMISSION_HEADER
    global compiled_messages_sheet, compiled_tile_numbers  # NEW
    wait = 2
    for attempt in range(1, max_tries + 1):
        try:
            log.info(f"[Sheets] Initializing (attempt {attempt}/{max_tries})...")
            creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
            gs_client = await asyncio.to_thread(gspread.authorize, creds)
            spreadsheet = await asyncio.to_thread(gs_client.open, SHEET_NAME)
            tiles_activities_sheet = await asyncio.to_thread(spreadsheet.worksheet, "TilesActivities")
            submission_sheet = await asyncio.to_thread(spreadsheet.worksheet, "Submissions")
            team_sheet = await asyncio.to_thread(spreadsheet.worksheet, "TeamDetails")
            competition_info_sheet = await asyncio.to_thread(spreadsheet.worksheet, "CompetitionInformation")
            compiled_messages_sheet = await asyncio.to_thread(spreadsheet.worksheet, "Compiled Messages by Team")  # NEW

            tile_names, tile_items = await asyncio.to_thread(_load_from_tiles_sheet)
            compiled_tile_numbers = await asyncio.to_thread(_load_compiled_tile_numbers)  # NEW

            try:
                SUBMISSION_HEADER = await asyncio.to_thread(submission_sheet.row_values, 1)
            except Exception:
                SUBMISSION_HEADER = None

            HEALTH["sheets_ready"] = True
            HEALTH["last_error"] = ""
            log.info("[Sheets] Ready.")
            return
        except Exception as e:
            HEALTH["last_error"] = f"Sheets init failed (attempt {attempt}): {e}"
            log.warning(HEALTH["last_error"])
            if attempt == max_tries:
                log.error("[Sheets] Giving up after retries.")
                return
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30)

def sheets_ready() -> bool:
    return HEALTH["sheets_ready"] and all([
        gs_client, spreadsheet, tiles_activities_sheet,
        submission_sheet, team_sheet, competition_info_sheet, compiled_messages_sheet
    ])

def is_server_mode() -> bool:
    """CompetitionInformation!B1: 'true' = server mode (guild id), 'false' = channel mode (channel id)."""
    try:
        value = competition_info_sheet.acell("B1").value
        return str(value).strip().lower() == "true"
    except Exception as e:
        HEALTH["last_error"] = f"is_server_mode error: {e}"
        log.warning(HEALTH["last_error"])
        return True

def get_channel_ids_for_submission(submission_id: str):
    """
    Lookup TeamDetails by column B (submission_id) and return channel IDs or None.
    Expected row: [TeamName, submission_id, approval, approved, denied, ...]
    """
    try:
        rows = team_sheet.get_all_values()
        for row in rows[1:]:
            if len(row) >= 5 and row[1] == submission_id:
                return {
                    "submission_id": row[1],
                    "approval": int(row[2]),
                    "approved": int(row[3]),
                    "denied": int(row[4]),
                }
    except Exception as e:
        HEALTH["last_error"] = f"TeamDetails lookup error: {e}"
        log.warning(HEALTH["last_error"])
    return None

async def get_team_index_for_channel(channel_id: int) -> int | None:
    """
    For /tileprogress: Only allow from TeamDetails column F channels.
    Team 1 = A2 (row index 2), Team 2 = A3 (row index 3).
    """
    try:
        rows = await asyncio.to_thread(team_sheet.get_all_values)
    except Exception:
        return None

    # Ensure we have at least rows 2 & 3
    # Column F is index 5 (0-based)
    def parse_chan(val: str) -> int | None:
        try:
            return int(str(val).strip())
        except Exception:
            return None

    # Row 2 -> Team 1
    if len(rows) >= 2:
        row2 = rows[1]
        if len(row2) >= 6:
            chan = parse_chan(row2[5])
            if chan and chan == channel_id:
                return 1

    # Row 3 -> Team 2
    if len(rows) >= 3:
        row3 = rows[2]
        if len(row3) >= 6:
            chan = parse_chan(row3[5])
            if chan and chan == channel_id:
                return 2

    # Fallback: scan all rows and infer from column A text "Team 1"/"Team 2"
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= 6 and parse_chan(row[5]) == channel_id:
            name = (row[0] or "").strip()
            m = re.search(r"(\d+)", name)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    pass
            return 1
    return None

def parse_submission_from_embed(embed: discord.Embed):
    """Extract Tile, user, image, timestamp, SID from the approval embed."""
    vals = {"tile": None, "user_display": None, "image_url": None,
            "timestamp_str": None, "submission_id": None}
    for f in embed.fields:
        n = (f.name or "").lower()
        if n == "tile":
            vals["tile"] = f.value
        elif n == "submitted by":
            vals["user_display"] = f.value
    if embed.image and embed.image.url:
        vals["image_url"] = embed.image.url
    ft = (embed.footer.text or "")
    m_sid = re.search(r"SID\s+(\d+)", ft)
    if m_sid:
        vals["submission_id"] = m_sid.group(1)
    m_ts = re.search(r"Submitted at ([0-9:\- ]+)", ft)
    if m_ts:
        vals["timestamp_str"] = m_ts.group(1)
    return vals

def archive_embed(embed: discord.Embed, status: str):
    archived = discord.Embed(
        title=f"{'✅' if status=='Approved' else '❌'} {status} • {embed.title}",
        color=discord.Color.dark_grey(),
        description="Processed."
    )
    for f in embed.fields:
        archived.add_field(name=f.name, value=f.value, inline=f.inline)
    if embed.image and embed.image.url:
        archived.set_image(url=embed.image.url)
    archived.set_footer(text=(embed.footer.text or "") + " • Archived")
    return archived

# ==================== Permissions ====================
REQUIRE_APPROVER_PERMS = True
def approver_check(user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    p = user.guild_permissions
    return bool(p.administrator or p.manage_messages)

# ==================== Approval flow state ====================
# Store ephemeral approver choices per (approval_message_id, approver_id)
APPROVAL_STATE: dict[tuple[int, int], dict] = {}

# ==================== UI Components ====================
class AmountModal(ui.Modal, title="Enter Amount"):
    amount = ui.TextInput(label="Amount", placeholder="Enter a positive integer", required=True, max_length=12)

    def __init__(self, original_message: discord.Message, invoker_id: int):
        super().__init__(timeout=180)
        self.original_message = original_message
        self.invoker_id = invoker_id

    async def on_submit(self, interaction: discord.Interaction):
        # Defer first so followups are valid (fixes Unknown Webhook)
        await interaction.response.defer(ephemeral=True)

        if REQUIRE_APPROVER_PERMS and not approver_check(interaction.user):
            await interaction.followup.send("❌ You don't have permission to approve.", ephemeral=True)
            return
        if not sheets_ready():
            await interaction.followup.send("⚠ Sheets are still starting up. Try again in a moment.", ephemeral=True)
            return
        if not self.original_message.embeds:
            await interaction.followup.send("❌ Missing submission embed.", ephemeral=True)
            return

        # Validate amount
        try:
            amt = int(str(self.amount.value).strip())
            if amt <= 0:
                raise ValueError
        except Exception:
            await interaction.followup.send("❌ Amount must be a positive integer.", ephemeral=True)
            return

        # Fetch previously chosen item
        key = (self.original_message.id, self.invoker_id)
        state = APPROVAL_STATE.get(key) or {}
        chosen_item = state.get("item")
        if not chosen_item:
            await interaction.followup.send("❌ Please pick an item first.", ephemeral=True)
            return

        embed = self.original_message.embeds[0]
        await process_approval(interaction, self.original_message, embed, amt, chosen_item)
        APPROVAL_STATE.pop(key, None)

class ItemSelect(ui.Select):
    def __init__(self, items: list[str], original_message: discord.Message, invoker_id: int):
        opts = [discord.SelectOption(label=item[:100], value=item[:100]) for item in (items or ["Unknown"])]
        super().__init__(placeholder="Select item", min_values=1, max_values=1, options=opts[:25], custom_id="bingo:item_select")
        self.original_message = original_message
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This prompt isn’t for you.", ephemeral=True)
            return
        if REQUIRE_APPROVER_PERMS and not approver_check(interaction.user):
            await interaction.response.send_message("❌ You don't have permission to approve.", ephemeral=True)
            return
        if not self.original_message or not self.original_message.embeds:
            await interaction.response.send_message("❌ Missing submission embed.", ephemeral=True)
            return

        selected_item = self.values[0]
        APPROVAL_STATE[(self.original_message.id, self.invoker_id)] = {"item": selected_item}

        # Next: prompt amount
        await interaction.response.send_modal(AmountModal(original_message=self.original_message, invoker_id=self.invoker_id))

class ItemSelectView(ui.View):
    def __init__(self, items: list[str], original_message: discord.Message, invoker_id: int):
        super().__init__(timeout=180)
        self.add_item(ItemSelect(items=items, original_message=original_message, invoker_id=invoker_id))

class ApproveDenyView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="bingo:approve")
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        if REQUIRE_APPROVER_PERMS and not approver_check(interaction.user):
            await interaction.response.send_message("❌ You don't have permission to approve.", ephemeral=True)
            return
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("❌ Missing submission embed.", ephemeral=True)
            return

        embed = interaction.message.embeds[0]
        data = parse_submission_from_embed(embed)
        tile = data["tile"] or ""
        items = tile_items.get(tile) or ["Unknown"]

        view = ItemSelectView(items=items, original_message=interaction.message, invoker_id=interaction.user.id)
        await interaction.response.send_message("Select the item for this submission:", view=view, ephemeral=True)

    @ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="bingo:deny")
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        if REQUIRE_APPROVER_PERMS and not approver_check(interaction.user):
            await interaction.response.send_message("❌ You don't have permission to deny.", ephemeral=True)
            return
        if not sheets_ready():
            await interaction.response.send_message("⚠ Sheets still starting; try again shortly.", ephemeral=True)
            return

        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if not embed:
            await interaction.response.send_message("❌ Missing submission embed.", ephemeral=True)
            return

        data = parse_submission_from_embed(embed)
        submission_id = data["submission_id"]
        if not submission_id:
            await interaction.response.send_message("❌ Missing submission ID.", ephemeral=True)
            return

        channel_info = get_channel_ids_for_submission(submission_id)
        if not channel_info:
            await interaction.response.send_message("❌ Could not find target channels for this submission.", ephemeral=True)
            return

        denied_channel = interaction.client.get_channel(channel_info["denied"])
        if denied_channel:
            try:
                denied_embed = discord.Embed(title=embed.title, color=discord.Color.red())
                for f in embed.fields:
                    denied_embed.add_field(name=f.name, value=f.value, inline=f.inline)
                if embed.image and embed.image.url:
                    denied_embed.set_image(url=embed.image.url)
                denied_embed.set_footer(text=embed.footer.text)
                await denied_channel.send(embed=denied_embed, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass

        try:
            await interaction.message.edit(embed=archive_embed(embed, "Denied"), view=None)
        except Exception:
            pass

        await interaction.response.send_message("🚫 Denied.", ephemeral=True)

# ==================== Shared approval logic ====================
async def process_approval(interaction: discord.Interaction, original_message: discord.Message, embed: discord.Embed, amt: int, item: str):
    """Writes to Sheets (with original timestamp), forwards to approved channel, archives message."""
    if not sheets_ready():
        await interaction.followup.send("⚠ Sheets are still starting up. Try again in a moment.", ephemeral=True)
        return

    data = parse_submission_from_embed(embed)
    submission_id = data["submission_id"]
    if not submission_id:
        await interaction.followup.send("❌ Missing submission ID.", ephemeral=True)
        return

    channel_info = get_channel_ids_for_submission(submission_id)
    if not channel_info:
        await interaction.followup.send("❌ Could not find target channels for this submission.", ephemeral=True)
        return

    # Detect if Submissions sheet still has an "Activity" column
    header_lower = [h.lower() for h in (SUBMISSION_HEADER or [])]
    has_activity_col = "activity" in header_lower

    row = [
        data["timestamp_str"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        submission_id,
        data["user_display"] or interaction.user.display_name,
        data["tile"] or "",
    ]
    if has_activity_col:
        row.append("")  # blank Activity to keep columns aligned
    row.extend([
        item or "",
        int(amt),
        data["image_url"] or "",
    ])

    # Append to sheet
    try:
        await asyncio.to_thread(
            submission_sheet.append_row,
            row,
            value_input_option="USER_ENTERED"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to record in Sheets: {e}", ephemeral=True)
        return

    # Forward to approved channel (Tile + Item + Amount)
    approved_channel = interaction.client.get_channel(channel_info["approved"])
    if approved_channel:
        approved_embed = discord.Embed(title=embed.title, color=discord.Color.green())
        for f in embed.fields:
            if f.name.lower() in ("item", "amount"):
                continue
            approved_embed.add_field(name=f.name, value=f.value, inline=f.inline)
        approved_embed.add_field(name="Item", value=str(item), inline=True)
        approved_embed.add_field(name="Amount", value=str(amt), inline=True)
        if embed.image and embed.image.url:
            approved_embed.set_image(url=embed.image.url)
        approved_embed.set_footer(text=embed.footer.text)
        try:
            await approved_channel.send(embed=approved_embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    # Archive approval message (remove buttons)
    try:
        await original_message.edit(embed=archive_embed(embed, "Approved"), view=None)
    except Exception:
        pass

    await interaction.followup.send("✅ Approved and recorded.", ephemeral=True)

# ==================== Autocomplete ====================
async def tile_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=tile, value=tile)
        for tile in tile_names
        if cur in tile.lower()
    ][:25]

async def compiled_tile_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=t, value=t)
        for t in compiled_tile_numbers
        if cur in t.lower()
    ][:25]

# ==================== Compiled message lookup ====================
async def fetch_compiled_message(tile_number: str, team_index: int) -> str | None:
    """
    In 'Compiled Messages by Team':
      - Column A: Tile number
      - Column B: Team 1 compiled message
      - Column C: Team 2 compiled message
    """
    try:
        rows = await asyncio.to_thread(compiled_messages_sheet.get_all_values)
    except Exception:
        return None

    tile_key = str(tile_number).strip()
    for row in rows[1:]:  # skip header
        if not row:
            continue
        a = (row[0] if len(row) >= 1 else "").strip()
        if a == tile_key:
            if team_index == 1:
                return (row[1] if len(row) >= 2 else "")
            elif team_index == 2:
                return (row[2] if len(row) >= 3 else "")
            else:
                return ""
    return None

# ==================== Events ====================
@bot.event
async def on_ready():
    bot.add_view(ApproveDenyView())              # persistent buttons
    asyncio.create_task(init_sheets_with_retry())# warm Sheets in background
    log.info(f"🤖 Bot is online as {bot.user}")

    # ---------- FORCE SYNC to fix CommandSignatureMismatch ----------
    try:
        if TEST_GUILD_ID:
            guild_obj = discord.Object(id=int(TEST_GUILD_ID))
            tree.clear_commands(guild=guild_obj)
            tree.copy_global_to(guild=guild_obj)
            await tree.sync(guild=guild_obj)
            log.info(f"✅ Per-guild commands resynced for {TEST_GUILD_ID}")
        await tree.sync()
        log.info("✅ Global commands synced")
    except Exception as e:
        log.warning(f"⚠ Command sync failed: {e}")

# ==================== Commands ====================
@tree.command(
    name="submit",
    description="Submit a tile completion with required image (approver selects item and enters amount)."
)
@app_commands.describe(
    tile_name="Choose the tile",
    image="Attach an image (required)",
)
@app_commands.autocomplete(tile_name=tile_autocomplete)
async def submit(
    interaction: discord.Interaction,
    tile_name: str,
    image: discord.Attachment,
):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Submissions must be made in a server channel, not DMs.", ephemeral=True)
        return

    if not sheets_ready():
        await interaction.response.send_message("⚠ Setup is still starting. Try again in ~30s.", ephemeral=True)
        return

    # Resolve server vs channel mode
    if is_server_mode():
        submission_lookup_id = str(interaction.guild.id)
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
    else:
        submission_lookup_id = str(interaction.channel.id)
        channel_info = get_channel_ids_for_submission(submission_lookup_id)
        if channel_info:
            expected = int(channel_info["submission_id"])
            if interaction.channel.id != expected:
                await interaction.response.send_message("❌ You can only submit from your designated team channel.", ephemeral=True)
                return

    if not channel_info:
        await interaction.response.send_message("❌ This server/channel is not registered for submissions.", ephemeral=True)
        return

    if tile_name not in tile_names:
        await interaction.response.send_message(
            f"❌ Invalid tile. Available: {', '.join(tile_names) or '…loading'}",
            ephemeral=True,
        ); return
    if not image:
        await interaction.response.send_message("❌ You must attach an image.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_display = interaction.user.display_name
    image_url = image.url

    approval_channel = bot.get_channel(channel_info["approval"])
    if not approval_channel:
        await interaction.followup.send("❌ Approval channel not found or bot missing access.", ephemeral=True); return

    embed = discord.Embed(title=f"📝 Submission from {user_display}", color=discord.Color.orange())
    embed.add_field(name="Tile", value=tile_name, inline=True)
    embed.add_field(name="Submitted By", value=user_display, inline=True)
    embed.set_image(url=image_url)
    embed.set_footer(text=f"Submitted at {timestamp} • SID {submission_lookup_id}")

    try:
        await approval_channel.send(embed=embed, view=ApproveDenyView(), allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't post to approval channel: {e}", ephemeral=True); return

    await interaction.followup.send("✅ Submission received and sent for approval.", ephemeral=True)

@tree.command(name="tileprogress", description="Show compiled tile progress for your team")
@app_commands.describe(tile_number="Tile number (matches column A in 'Compiled Messages by Team')")
@app_commands.autocomplete(tile_number=compiled_tile_autocomplete)
async def tileprogress(interaction: discord.Interaction, tile_number: str):
    if not sheets_ready():
        await interaction.response.send_message("⚠ Setup is still starting. Try again shortly.", ephemeral=True)
        return

    # Only allowed from the channel listed in TeamDetails!F for Team 1/2
    team_idx = await get_team_index_for_channel(interaction.channel.id)
    if team_idx not in (1, 2):
        await interaction.response.send_message(
            "❌ You can only use this in your team’s designated channel (TeamDetails column F).",
            ephemeral=True
        )
        return

    msg = await fetch_compiled_message(tile_number, team_idx)
    if msg is None:
        await interaction.response.send_message(
            f"❌ Tile `{tile_number}` not found in ‘Compiled Messages by Team’.",
            ephemeral=True
        )
        return
    if not msg:
        await interaction.response.send_message(
            f"ℹ No compiled message for tile `{tile_number}` (Team {team_idx}).",
            ephemeral=True
        )
        return

    # Post publicly; chunk if >2000 chars
    chunks = [msg[i:i+2000] for i in range(0, len(msg), 2000)]
    await interaction.response.send_message(chunks[0], allowed_mentions=discord.AllowedMentions.none())
    for extra in chunks[1:]:
        try:
            await interaction.followup.send(extra, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            break

@tree.command(name="health", description="Show bot health/status")
async def health(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Sheets ready: **{HEALTH['sheets_ready']}**\n"
        f"Boot started: {HEALTH['boot_started_at']}\n"
        f"Last error: `{HEALTH['last_error'] or 'none'}`",
        ephemeral=True
    )

@tree.command(name="resync", description="Sync slash commands (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def resync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if TEST_GUILD_ID:
            guild_obj = discord.Object(id=int(TEST_GUILD_ID))
            tree.clear_commands(guild=guild_obj)
            tree.copy_global_to(guild=guild_obj)
            await tree.sync(guild=guild_obj)
        await tree.sync()
        await interaction.followup.send("✅ Commands resynced.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

# ==================== Run ====================
if not DISCORD_TOKEN:
    raise SystemExit("Missing discord_token in .env")
bot.run(DISCORD_TOKEN)
