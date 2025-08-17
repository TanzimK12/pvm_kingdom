import sys, asyncio, os, logging
import discord
from dotenv import load_dotenv

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smoke")

load_dotenv()
TOKEN = os.getenv("discord_token")
if not TOKEN:
    raise SystemExit("Missing discord_token in .env")

intents = discord.Intents.none()
intents.guilds = True  # minimal

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    log.info(f"✅ Connected as {client.user} (id={client.user.id})")
    # stay alive for 10s then quit so you can see it worked
    await asyncio.sleep(10)
    await client.close()

client.run(TOKEN)
