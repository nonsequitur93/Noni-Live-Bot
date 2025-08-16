# bot.py — Noni's Pirate Go-Live Bot (Postgres persistence)
# Requirements in requirements.txt:
# discord.py, aiohttp, python-dotenv, psycopg2-binary

import os, time, asyncio, aiohttp
from typing import Optional, Dict
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# ── DB (psycopg2) ──────────────────────────────────────────────────────────────
import psycopg2
import psycopg2.extras

def db_connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Missing DATABASE_URL environment variable.")
    # Ensure SSL on Railway if not already present
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

def db_exec(sql: str, params: tuple = ()):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            try:
                rows = cur.fetchall()
            except psycopg2.ProgrammingError:
                rows = None
        conn.commit()
    return rows

def init_db():
    db_exec("""
    CREATE TABLE IF NOT EXISTS settings (
        guild_id BIGINT PRIMARY KEY,
        notify_channel_id BIGINT
    );
    """)
    db_exec("""
    CREATE TABLE IF NOT EXISTS users (
        discord_user_id BIGINT PRIMARY KEY,
        twitch_login TEXT NOT NULL,
        twitch_id TEXT NOT NULL,
        display_name TEXT
    );
    """)
    db_exec("""
    CREATE TABLE IF NOT EXISTS live_state (
        twitch_id TEXT PRIMARY KEY,
        started_at TIMESTAMPTZ
    );
    """)
    # must come AFTER the table exists
    db_exec("ALTER TABLE live_state ADD COLUMN IF NOT EXISTS stream_id TEXT;")

def db_set_notify_channel(guild_id: int, channel_id: int):
    db_exec("""
    INSERT INTO settings (guild_id, notify_channel_id)
    VALUES (%s, %s)
    ON CONFLICT (guild_id) DO UPDATE SET notify_channel_id=EXCLUDED.notify_channel_id;
    """, (guild_id, channel_id))

def db_get_notify_channel(guild_id: int) -> Optional[int]:
    rows = db_exec("SELECT notify_channel_id FROM settings WHERE guild_id=%s;", (guild_id,))
    return rows[0]["notify_channel_id"] if rows else None

def db_upsert_user(discord_user_id: int, login: str, twitch_id: str, display: Optional[str]):
    db_exec("""
    INSERT INTO users (discord_user_id, twitch_login, twitch_id, display_name)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (discord_user_id) DO UPDATE
    SET twitch_login=EXCLUDED.twitch_login, twitch_id=EXCLUDED.twitch_id, display_name=EXCLUDED.display_name;
    """, (discord_user_id, login, twitch_id, display))

def db_remove_user(discord_user_id: int):
    db_exec("DELETE FROM users WHERE discord_user_id=%s;", (discord_user_id,))

def db_list_users():
    return db_exec("SELECT discord_user_id, twitch_login, twitch_id, display_name FROM users ORDER BY discord_user_id;") or []

def db_all_twitch_ids():
    rows = db_exec("SELECT twitch_id FROM users;") or []
    return [r["twitch_id"] for r in rows]

def db_get_user_by_discord(discord_user_id: int):
    rows = db_exec("SELECT * FROM users WHERE discord_user_id=%s;", (discord_user_id,))
    return rows[0] if rows else None

def db_live_started(twitch_id: str) -> Optional[str]:
    rows = db_exec("SELECT started_at FROM live_state WHERE twitch_id=%s;", (twitch_id,))
    return str(rows[0]["started_at"]) if rows and rows[0]["started_at"] else None

def db_set_live(twitch_id: str, started_at_iso: str):
    db_exec("""
    INSERT INTO live_state (twitch_id, started_at)
    VALUES (%s, %s)
    ON CONFLICT (twitch_id) DO UPDATE SET started_at=EXCLUDED.started_at;
    """, (twitch_id, started_at_iso))

def db_clear_live(twitch_id: str):
    db_exec("DELETE FROM live_state WHERE twitch_id=%s;", (twitch_id,))

def db_get_stream_id(twitch_id: str) -> Optional[str]:
    rows = db_exec("SELECT stream_id FROM live_state WHERE twitch_id=%s;", (twitch_id,))
    return rows[0]["stream_id"] if rows and rows[0]["stream_id"] else None

def db_set_stream_id(twitch_id: str, stream_id: str):
    db_exec("""
    INSERT INTO live_state (twitch_id, stream_id)
    VALUES (%s, %s)
    ON CONFLICT (twitch_id) DO UPDATE SET stream_id=EXCLUDED.stream_id;
    """, (twitch_id, stream_id))

# ── Load secrets ───────────────────────────────────────────────────────────────
load_dotenv()
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID      = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET  = os.getenv("TWITCH_CLIENT_SECRET")
GUILD_ID              = int(os.getenv("GUILD_ID", "0"))
MENTION_ROLE_ID       = os.getenv("MENTION_ROLE_ID")   # optional ping role id
LIVE_ROLE_ID          = os.getenv("LIVE_ROLE_ID")      # optional LIVE role while streaming
# Marvel Rivals forwarding config
TEST_CHANNEL_ID = int(os.getenv("TEST_CHANNEL_ID", "0"))
MR_THREAD_ID    = int(os.getenv("MR_THREAD_ID", "0"))
MR_ROLE_ID      = int(os.getenv("MR_ROLE_ID", "0"))
DELETE_SOURCE   = os.getenv("DELETE_SOURCE", "true").lower() in ("1","true","yes","y")

# ── Twitch API helper ──────────────────────────────────────────────────────────
class TwitchAPI:
    def __init__(self, session: aiohttp.ClientSession):
        self.sess = session
        self._token = None
        self._token_expiry = 0

    async def _get_token(self):
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
        async with self.sess.post(url, params=params) as r:
            j = await r.json()
            if "access_token" not in j:
                raise RuntimeError(f"Twitch token error: {j}")
            self._token = j["access_token"]
            self._token_expiry = now + j.get("expires_in", 3600)
            return self._token

    async def _headers(self):
        token = await self._get_token()
        return {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}

    async def get_user(self, login: str) -> Optional[dict]:
        url = "https://api.twitch.tv/helix/users"
        async with self.sess.get(url, headers=await self._headers(), params={"login": login}) as r:
            j = await r.json()
            return j["data"][0] if j.get("data") else None

    async def get_streams(self, user_ids: list[str]) -> Dict[str, dict]:
        if not user_ids:
            return {}
        url = "https://api.twitch.tv/helix/streams"
        params = [("user_id", uid) for uid in user_ids]
        async with self.sess.get(url, headers=await self._headers(), params=params) as r:
            j = await r.json()
            return {s["user_id"]: s for s in j.get("data", [])}

    # NEW: search by display name / any name
    async def search_channels(self, query: str) -> list[dict]:
        url = "https://api.twitch.tv/helix/search/channels"
        async with self.sess.get(url, headers=await self._headers(), params={"query": query, "first": 10}) as r:
            j = await r.json()
            return j.get("data", []) or []

    async def resolve_any(self, query: str) -> Optional[dict]:
        """Resolve login OR display name → canonical {login,id,display_name}."""
        if not query:
            return None
        # Try as login first
        u = await self.get_user(query.lower())
        if u:
            return {"login": u["login"], "id": u["id"], "display_name": u.get("display_name", u["login"])}
        # Fallback to display name search
        hits = await self.search_channels(query)
        if not hits:
            return None
        q_cf = query.casefold()
        exact = next((h for h in hits if str(h.get("display_name","")).casefold() == q_cf), None)
        best = exact or hits[0]
        login = best.get("broadcaster_login") or best.get("login") or ""
        bid   = best.get("id") or best.get("broadcaster_id") or ""
        dname = best.get("display_name") or login
        if login and bid:
            return {"login": login, "id": bid, "display_name": dname}
        return None

# ── Pretty helpers ─────────────────────────────────────────────────────────────
PIRATE_PURPLE = 0x8B5CF6
PIRATE_EMOJI  = "🏴‍☠️"
MEGAPHONE     = "📣"
SPARKLES      = "✨"

def is_mod(inter: discord.Interaction) -> bool:
    perms = inter.user.guild_permissions
    return perms.administrator or perms.manage_guild

def already_announced(twitch_user_id: str, stream_id: str) -> bool:
    return db_get_stream_id(twitch_user_id) == stream_id

def stream_preview_url(login: str, w=1280, h=720):
    return f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-{w}x{h}.jpg"

def get_role(guild: discord.Guild, role_id: str | None):
    return guild.get_role(int(role_id)) if role_id else None

# ── Discord Bot ────────────────────────────────────────────────────────────────
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # read & forward messages from #test
        intents.members = True           # to give/remove roles
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session: Optional[aiohttp.ClientSession] = None
        self.twitch: Optional[TwitchAPI] = None
        self.guild_obj: Optional[discord.Object] = None

    async def setup_hook(self):
        init_db()
        self.session = aiohttp.ClientSession()
        self.twitch  = TwitchAPI(self.session)
        if GUILD_ID:
            self.guild_obj = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=self.guild_obj)
            await self.tree.sync(guild=self.guild_obj)
        else:
            await self.tree.sync()
        check_live.start()

    async def close(self):
        await super().close()
        if self.session:
            await self.session.close()

bot = Bot()

# ── Forward Marvel Rivals messages from #test → thread ────────────────────────
async def _forward_to_thread(src: discord.Message, thread: discord.Thread):
    # Unarchive if needed
    if isinstance(thread, discord.Thread) and thread.archived:
        try:
            await thread.edit(archived=False, locked=False)
        except Exception as e:
            print("Thread unarchive error:", e)

    # Re-upload attachments
    files = []
    for a in src.attachments:
        try:
            files.append(await a.to_file())
        except Exception as e:
            print("Attachment fetch error:", e)

    # Build content with role ping (no header)
    role_ping = f"<@&{MR_ROLE_ID}>" if MR_ROLE_ID else ""
    original   = src.content or ""
    content    = (role_ping + ("\n" if role_ping and original else "")) + original
    if not content and role_ping:
        content = role_ping  # embeds-only case

    embeds = src.embeds if src.embeds else None
    allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

    await thread.send(content=content, embeds=embeds, files=files, allowed_mentions=allowed)
    print(f"[relay] forwarded {src.id} → thread {thread.id}")

@bot.event
async def on_message(message: discord.Message):
    # ignore DMs & ourselves
    if not message.guild or message.author == bot.user:
        return
    # only watch the #test channel
    if not TEST_CHANNEL_ID or message.channel.id != TEST_CHANNEL_ID:
        return
    # get destination thread
    dest = message.guild.get_thread(MR_THREAD_ID)
    if not isinstance(dest, discord.Thread):
        print("Marvel relay: destination thread not found (check MR_THREAD_ID).")
        return
    try:
        await _forward_to_thread(message, dest)
        if DELETE_SOURCE:
            print(f"[relay] deleting source {message.id} from #{message.channel.name}")
            await message.delete()
    except discord.Forbidden:
        print("Marvel relay: missing perms (Manage Messages in #test; Send/Embed/Attach in thread).")
    except Exception as e:
        print("Marvel relay error:", repr(e))

# ── Slash Commands ─────────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(inter: discord.Interaction, error: Exception):
    print("Slash command error:", repr(error))
    try:
        if inter.response.is_done():
            await inter.followup.send("Oops! I hit an error running that command. The crew is on it. 🛠️", ephemeral=True)
        else:
            await inter.response.send_message("Oops! I hit an error running that command. The crew is on it. 🛠️", ephemeral=True)
    except Exception as e:
        print("Failed to send error message:", e)

@bot.tree.command(name="twitch_set", description="Link your Twitch (login or display name)")
@app_commands.describe(username="Your Twitch @ (login or display name)")
async def
