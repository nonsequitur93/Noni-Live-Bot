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
MENTION_ROLE_ID       = os.getenv("MENTION_ROLE_ID")   # optional
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
        intents.message_content = True  # needed to read & forward messages from #test
        intents.members = True 
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
        content = role_ping  # if source was embeds-only

    embeds = src.embeds if src.embeds else None
    allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

    await thread.send(content=content, embeds=embeds, files=files, allowed_mentions=allowed)

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
            await message.delete()
    except discord.Forbidden:
        print("Marvel relay: missing perms (Manage Messages in #test; Send/Embed/Attach in thread).")
    except Exception as e:
        print("Marvel relay error:", repr(e))

# ── Slash Commands ─────────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(inter: discord.Interaction, error: Exception):
    # Log to console
    print("Slash command error:", repr(error))
    # Try to tell the user instead of silently failing
    try:
        if inter.response.is_done():
            await inter.followup.send("Oops! I hit an error running that command. The crew is on it. 🛠️", ephemeral=True)
        else:
            await inter.response.send_message("Oops! I hit an error running that command. The crew is on it. 🛠️", ephemeral=True)
    except Exception as e:
        print("Failed to send error message:", e)

@bot.tree.command(name="twitch_set", description="Link your Twitch username")
@app_commands.describe(username="Your Twitch @ (login name, not display name)")
async def twitch_set(inter: discord.Interaction, username: str):
    await inter.response.defer(ephemeral=True)
    u = await bot.twitch.get_user(username)
    if not u:
        return await inter.followup.send("I couldn't find that Twitch user. Double-check the spelling.", ephemeral=True)
    db_upsert_user(discord_user_id=inter.user.id, login=u["login"], twitch_id=u["id"], display=u.get("display_name"))
    await inter.followup.send(f"{PIRATE_EMOJI} Aye! Linked your Twitch to **{u.get('display_name', u['login'])}**.", ephemeral=True)

@bot.tree.command(name="twitch_remove", description="Unlink your Twitch")
async def twitch_remove(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    db_remove_user(inter.user.id)
    await inter.followup.send("Unlinked your Twitch. Fair winds!", ephemeral=True)

@bot.tree.command(name="twitch_list", description="See who registered (mods only)")
async def twitch_list(inter: discord.Interaction):
    if not is_mod(inter):
        return await inter.response.send_message("Only Fleet Commanders can use this.", ephemeral=True)
    rows = db_list_users()
    if not rows:
        return await inter.response.send_message("No crewmates registered yet.", ephemeral=True)
    lines = []
    for r in rows:
        member = inter.guild.get_member(int(r["discord_user_id"]))
        name = member.display_name if member else f"User {r['discord_user_id']}"
        lines.append(f"• **{name}** → `{r['twitch_login']}`")
    await inter.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="twitch_channel_set", description="Choose the go-live alerts channel (mods only)")
@app_commands.describe(channel="Channel where alerts should be posted")
async def twitch_channel_set(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_mod(inter):
        return await inter.response.send_message("Only Fleet Commanders can use this.", ephemeral=True)
    db_set_notify_channel(inter.guild_id, channel.id)
    await inter.response.send_message(f"{PIRATE_EMOJI} Aye! I’ll hail the crew in {channel.mention}.", ephemeral=True)

@bot.tree.command(name="twitch_preview", description="Post a sample go-live embed (mods only)")
@app_commands.describe(channel="Channel to preview in (defaults to your notify channel)")
async def twitch_preview(inter: discord.Interaction, channel: discord.TextChannel | None = None):
    perms = inter.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        return await inter.response.send_message("Mods only.", ephemeral=True)
    target_id = db_get_notify_channel(inter.guild_id)
    target = channel or (bot.get_channel(target_id) if target_id else None)
    if not isinstance(target, discord.TextChannel):
        return await inter.response.send_message("No notify channel set. Run /twitch_channel_set first.", ephemeral=True)
    r = db_get_user_by_discord(inter.user.id) or (db_list_users()[0] if db_list_users() else None)
    if not r:
        return await inter.response.send_message("No registered users yet. Run /twitch_set first.", ephemeral=True)
    u = await bot.twitch.get_user(r["twitch_login"])
    stream = {
        "title": "Preview voyage across the Grand Line 🌊",
        "game_name": "Just Chatting",
        "viewer_count": 42,
        "started_at": __import__("datetime").datetime.utcnow().isoformat()+"Z"
    }
    await post_go_live(target, stream, u)
    await inter.response.send_message(f"Preview sent to {target.mention}.", ephemeral=True)
@bot.tree.command(name="ping", description="Quick health check")
async def ping(inter: discord.Interaction):
    await inter.response.send_message("Pong! 🏴‍☠️", ephemeral=True)

# ── Posting the go-live embed ──────────────────────────────────────────────────
async def post_go_live(channel: discord.TextChannel, stream: dict, user: dict):
    role_ping = f"<@&{MENTION_ROLE_ID}>" if MENTION_ROLE_ID else ""
    title     = stream.get("title") or "Come aboard and vibe!"
    game      = stream.get("game_name") or "On the High Seas"
    viewers   = stream.get("viewer_count", "—")
    login     = user["login"]
    display   = user.get("display_name", login)
    url       = f"https://twitch.tv/{login}"

    embed = discord.Embed(
        title=f"{PIRATE_EMOJI} {display} is LIVE!",
        url=url,
        description=f"{SPARKLES} **{title}**",
        color=PIRATE_PURPLE,
    )
    embed.add_field(name="Category", value=game, inline=True)
    embed.add_field(name="Viewers",  value=str(viewers), inline=True)
    embed.set_image(url=stream_preview_url(login))
    embed.set_footer(text="Twitch • Go-Live Alert")
    embed.set_author(name="Crewmate Set Sail", icon_url="https://static-00.iconduck.com/assets.00/anchor-emoji-1024x1024-4n8e4b1w.png")

    content = f"{role_ping} {MEGAPHONE} Ahoy, Nakama! {display} just set sail → {url}" if role_ping else None
    await channel.send(content=content, embed=embed)

# ── Live checker (runs every 2 minutes) ────────────────────────────────────────
from discord.ext import tasks

@tasks.loop(minutes=2)
async def check_live():
    print("[live] tick")
    for g in bot.guilds:
        chan_id = db_get_notify_channel(g.id)
        if not chan_id:
            continue
        channel = bot.get_channel(chan_id)
        if not isinstance(channel, discord.TextChannel):
            continue

        twitch_ids = db_all_twitch_ids()
        if not twitch_ids:
            continue

        streams = await bot.twitch.get_streams(twitch_ids)
        live_role = get_role(g, LIVE_ROLE_ID)

        for r in db_list_users():
            tid = r["twitch_id"]
            stream = streams.get(tid)
            member = g.get_member(int(r["discord_user_id"]))

            if stream:
                # announce once per stream session (use stable stream.id)
                stream_id = stream.get("id")
                if stream_id and not already_announced(tid, stream_id):
                    u = await bot.twitch.get_user(r["twitch_login"])
                    try:
                        await post_go_live(channel, stream, u)
                        db_set_stream_id(tid, stream_id)
                        print(f"[live] announced once for {r['twitch_login']} (stream_id={stream_id})")
                    except Exception as e:
                        print("Post error:", e)

                # give LIVE role
                if live_role and member and live_role not in member.roles:
                    try:
                        print(f"[live] add LIVE role → {member} ({member.id})")
                        await member.add_roles(live_role, reason="Now live")
                    except Exception as e:
                        print("Add role error:", e)
            else:
                # clear live flag & remove LIVE role
                db_clear_live(tid)
                if live_role and member and live_role in member.roles:
                    try:
                        print(f"[live] remove LIVE role → {member} ({member.id})")
                        await member.remove_roles(live_role, reason="Stream ended")
                    except Exception as e:
                        print("Remove role error:", e)

@check_live.before_loop
async def before_check_live():
    await bot.wait_until_ready()


# ── Run it ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not (DISCORD_TOKEN and TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        raise SystemExit("Missing secrets. Check your env vars.")
    bot.run(DISCORD_TOKEN)
