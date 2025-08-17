# prune_commands_once.py
import sys, asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
from dotenv import load_dotenv
import discord

load_dotenv()
TOKEN = os.getenv("discord_token")

intents = discord.Intents.default()
intents.guilds = True  # only need guild list to wipe per-guild commands

bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    try:
        app_id = bot.user.id
        print(f"Logged in as {bot.user} ({app_id}). Wiping commands...")

        # 1) Wipe GLOBAL commands
        await bot.http.bulk_upsert_global_commands(app_id, [])
        print(" - Global commands wiped")

        # 2) Wipe GUILD commands for every guild the bot is in
        wiped = 0
        for g in bot.guilds:
            try:
                await bot.http.bulk_upsert_guild_commands(app_id, g.id, [])
                wiped += 1
            except Exception as e:
                print(f"   ⚠ Couldn't wipe guild {g.id}: {e}")
        print(f" - Guild commands wiped in {wiped} guild(s)")

        print("✅ Done. Close this script, then run your main bot to re-register only the new set.")
    finally:
        await bot.close()

bot.run(TOKEN)
