# bot.py — Noni's Pirate Go-Live Bot (Postgres persistence)
# Requirements (requirements.txt):
# discord.py, aiohttp, python-dotenv, psycopg2-binary

import os, time, aiohttp
from typing import Optional, Dict, List
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from urllib.parse import urlparse

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

def db_get_stream_id(twitch_id: str) -> Optional[str]:
    rows = db_exec("SELECT stream_id FROM live_state WHERE twitch_id=%s;", (twitch_id,))
    return rows[0]["stream_id"] if rows and rows[0]["stream_id"] else None

def db_set_stream_id(twitch_id: str, stream_id: str):
    db_exec("""
    INSERT INTO live_state (twitch_id, stream_id)
    VALUES (%s, %s)
    ON CONFLICT (twitch_id) DO UPDATE SET stream_id=EXCLUDED.stream_id;
    """, (twitch_id, stream_id))

def db_clear_live(twitch_id: str):
    db_exec("DELETE FROM live_state WHERE twitch_id=%s;", (twitch_id,))

# ── Load secrets ───────────────────────────────────────────────────────────────
load_dotenv()
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID      = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET  = os.getenv("TWITCH_CLIENT_SECRET")
GUILD_ID              = int(os.getenv("GUILD_ID", "0"))
MENTION_ROLE_ID       = os.getenv("MENTION_ROLE_ID")           # optional
LIVE_ROLE_ID          = os.getenv("LIVE_ROLE_ID")              # optional LIVE role while streaming
LIVE_ROLE_NAME        = os.getenv("LIVE_ROLE_NAME", "Nakama Now Live 🔴")  # fallback name

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

    async def get_user_by_login(self, login: str) -> Optional[dict]:
        url = "https://api.twitch.tv/helix/users"
        async with self.sess.get(url, headers=await self._headers(), params={"login": login}) as r:
            j = await r.json()
            return j["data"][0] if j.get("data") else None

    async def search_channels(self, query: str) -> List[dict]:
        url = "https://api.twitch.tv/helix/search/channels"
        async with self.sess.get(url, headers=await self._headers(), params={"query": query, "first": 20}) as r:
            j = await r.json()
            return j.get("data", []) or []

    async def resolve_user(self, name_or_login: str) -> Optional[dict]:
        # Try as login first
        u = await self.get_user_by_login(name_or_login)
        if u:
            return u
        # Then try display-name search
        chans = await self.search_channels(name_or_login)
        exact = [c for c in chans if c.get("display_name","").lower() == name_or_login.lower()]
        candidate = exact[0] if exact else (chans[0] if chans else None)
        if not candidate:
            return None
        return {
            "id": candidate["id"],
            "login": candidate["broadcaster_login"],
            "display_name": candidate["display_name"],
            "profile_image_url": candidate.get("thumbnail_url")
        }

    async def get_streams(self, user_ids: List[str]) -> Dict[str, dict]:
        if not user_ids:
            return {}
        url = "https://api.twitch.tv/helix/streams"
        params = []
        for uid in user_ids:
            params.append(("user_id", uid))
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

def get_role_by_id_or_name(guild: discord.Guild, role_id: str | None, fallback_name: str):
    if role_id:
        r = guild.get_role(int(role_id))
        if r:
            return r
    return discord.utils.get(guild.roles, name=fallback_name)

# ── Discord Bot ────────────────────────────────────────────────────────────────
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True          # relay
        intents.members = True                  # add/remove roles
        intents.presences = True                # presence detection
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

# ── (Optional) Forward followed channel posts from #test to a specific thread ─
async def _forward_to_thread(src: discord.Message, thread: discord.Thread):
    if isinstance(thread, discord.Thread) and thread.archived:
        try:
            await thread.edit(archived=False, locked=False)
        except Exception as e:
            print("Thread unarchive error:", e)

    files = []
    for a in src.attachments:
        try:
            files.append(await a.to_file())
        except Exception as e:
            print("Attachment fetch error:", e)

    role_ping = f"<@&{MR_ROLE_ID}>" if MR_ROLE_ID else ""
    original   = src.content or ""
    content    = (role_ping + ("\n" if role_ping and original else "")) + original
    if not content and role_ping:
        content = role_ping

    embeds = src.embeds if src.embeds else None
    allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)
    await thread.send(content=content, embeds=embeds, files=files, allowed_mentions=allowed)
    print(f"Marvel relay: forwarded message {src.id} to thread {thread.id}")

@bot.event
async def on_message(message: discord.Message):
    # ignore DMs & ourselves
    if not message.guild or message.author == bot.user:
        return
    # only watch the #test channel
    if TEST_CHANNEL_ID and message.channel.id == TEST_CHANNEL_ID:
        dest = message.guild.get_thread(MR_THREAD_ID)
        if isinstance(dest, discord.Thread):
            try:
                await _forward_to_thread(message, dest)
                if DELETE_SOURCE:
                    print(f"Marvel relay: deleting source message {message.id} from #{message.channel.name}")
                    await message.delete()
            except discord.Forbidden:
                print("Marvel relay: missing perms (Manage Messages in #test; Send/Embed/Attach in thread).")
            except Exception as e:
                print("Marvel relay error:", repr(e))

def _find_streaming_activity(activities):
    for a in activities or []:
        if isinstance(a, discord.Streaming) or getattr(a, "type", None) == discord.ActivityType.streaming:
            if getattr(a, "url", None) and "twitch.tv" in a.url.lower():
                return a
    return None

@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    g = after.guild
    if not g:
        return

    live_role = get_role_by_id_or_name(g, LIVE_ROLE_ID, LIVE_ROLE_NAME)

    before_act = _find_streaming_activity(getattr(before, "activities", []))
    after_act  = _find_streaming_activity(getattr(after, "activities", []))

    # stopped streaming → remove role
    if before_act and not after_act:
        if live_role and live_role in after.roles:
            try:
                await after.remove_roles(live_role, reason="Stream ended (presence)")
            except Exception as e:
                print("presence remove role error:", e)
        return

    # still not streaming
    if not after_act:
        return

    # extract twitch login from URL
    try:
        path = urlparse(after_act.url).path  # "/login"
        login = path.strip("/").split("/")[0]
        if not login:
            return
    except Exception:
        return

    # resolve Twitch user
    try:
        u = await bot.twitch.resolve_user(login)
        if not u:
            return
    except Exception as e:
        print("presence resolve_user error:", e)
        return

    # upsert mapping (auto-register)
    try:
        db_upsert_user(after.id, u["login"], u["id"], u.get("display_name", u["login"]))
    except Exception as e:
        print("presence db_upsert_user error:", e)

    # confirm actually live & get stable stream.id
    try:
        streams = await bot.twitch.get_streams([u["id"]])
        stream = streams.get(u["id"])
        if not stream:
            return
        stream_id = stream.get("id")
    except Exception as e:
        print("presence get_streams error:", e)
        return

    # announce once per stream.id
    try:
        if stream_id and not already_announced(u["id"], stream_id):
            chan_id = db_get_notify_channel(g.id)
            channel = bot.get_channel(chan_id) if chan_id else None
            if isinstance(channel, discord.TextChannel):
                await post_go_live(channel, stream, u)
                db_set_stream_id(u["id"], stream_id)
                print(f"[presence] announced {u['login']} (stream_id={stream_id})")
    except Exception as e:
        print("presence announce error:", e)

    # ensure Live role present
    if live_role and live_role not in after.roles:
        try:
            await after.add_roles(live_role, reason="Now live (presence)")
        except Exception as e:
            print("presence add role error:", e)

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

@bot.tree.command(name="twitch_set", description="Link your Twitch (enter display name OR login)")
@app_commands.describe(name="Your Twitch display name OR login")
async def twitch_set(inter: discord.Interaction, name: str):
    await inter.response.defer(ephemeral=True)
    u = await bot.twitch.resolve_user(name)
    if not u:
        return await inter.followup.send("I couldn’t find that Twitch user. Try your exact channel display name or login.", ephemeral=True)
    db_upsert_user(inter.user.id, u["login"], u["id"], u.get("display_name", u["login"]))
    await inter.followup.send(f"{PIRATE_EMOJI} Linked your Twitch to **{u.get('display_name', u['login'])}** (`{u['login']}`).", ephemeral=True)

@bot.tree.command(name="twitch_remove", description="Unlink your Twitch")
async def twitch_remove(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    db_remove_user(inter.user.id)
    await inter.followup.send("Unlinked your Twitch. Fair winds!", ephemeral=True)

@bot.tree.command(name="twitch_check", description="See stored Twitch info (yours or someone else's)")
@app_commands.describe(member="Leave empty to check yourself")
async def twitch_check(inter: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or inter.user
    row = db_get_user_by_discord(target.id)
    if not row:
        return await inter.response.send_message(f"No Twitch linked for **{target.display_name}**.", ephemeral=True)
    await inter.response.send_message(
        f"**{target.display_name}** → display: **{row.get('display_name') or row['twitch_login']}**, "
        f"login: `{row['twitch_login']}`, id: `{row['twitch_id']}`",
        ephemeral=True
    )

@bot.tree.command(name="twitch_channel_set", description="Choose the go-live alerts channel (mods only)")
@app_commands.describe(channel="Channel where alerts should be posted")
async def twitch_channel_set(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_mod(inter):
        return await inter.response.send_message("Only Fleet Commanders can use this.", ephemeral=True)
    db_set_notify_channel(inter.guild_id, channel.id)
    await inter.response.send_message(f"{PIRATE_EMOJI} Aye! I’ll hail the crew in {channel.mention}.", ephemeral=True)

@bot.tree.command(name="twitch_preview", description="Post a sample go-live embed (mods only)")
@app_commands.describe(channel="Channel to preview in (defaults to your notify channel)")
async def twitch_preview(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not is_mod(inter):
        return await inter.response.send_message("Mods only.", ephemeral=True)
    target_id = db_get_notify_channel(inter.guild_id)
    target = channel or (bot.get_channel(target_id) if target_id else None)
    if not isinstance(target, discord.TextChannel):
        return await inter.response.send_message("No notify channel set. Run /twitch_channel_set first.", ephemeral=True)
    r = db_get_user_by_discord(inter.user.id) or (db_list_users()[0] if db_list_users() else None)
    if not r:
        return await inter.response.send_message("No registered users yet. Run /twitch_set first.", ephemeral=True)
    u = await bot.twitch.get_user_by_login(r["twitch_login"])
    stream = {
        "id": "preview_stream_id",
        "title": "Preview voyage across the Grand Line 🌊",
        "game_name": "Just Chatting",
        "viewer_count": 42,
        "started_at": __import__("datetime").datetime.utcnow().isoformat()+"Z"
    }
    await post_go_live(target, stream, u)
    await inter.response.send_message(f"Preview sent to {target.mention}.", ephemeral=True)

@bot.tree.command(name="apply_live_roles", description="Mods: apply/remove Live role for members streaming right now")
async def apply_live_roles(inter: discord.Interaction):
    if not is_mod(inter):
        return await inter.response.send_message("Only Fleet Commanders can use this.", ephemeral=True)

    g = inter.guild
    live_role = get_role_by_id_or_name(g, LIVE_ROLE_ID, LIVE_ROLE_NAME)
    if not live_role:
        live_role = await g.create_role(name=LIVE_ROLE_NAME, reason="Auto-create live role")

    rows = db_list_users()
    twitch_ids = [r["twitch_id"] for r in rows]
    streams = await bot.twitch.get_streams(twitch_ids)
    applied, removed = [], []

    for r in rows:
        member = g.get_member(int(r["discord_user_id"]))
        if not member:
            continue
        is_live = r["twitch_id"] in streams
        has_role = live_role in member.roles
        try:
            if is_live and not has_role:
                await member.add_roles(live_role, reason="Now live (manual sync)")
                applied.append(member.display_name)
            if (not is_live) and has_role:
                await member.remove_roles(live_role, reason="Stream ended (manual sync)")
                removed.append(member.display_name)
        except Exception as e:
            print("apply_live_roles error:", e)

    msg = []
    if applied:
        msg.append(f"✅ Added **{LIVE_ROLE_NAME}** → " + ", ".join(applied))
    if removed:
        msg.append(f"🧹 Removed **{LIVE_ROLE_NAME}** → " + ", ".join(removed))
    if not msg:
        msg.append("No changes. All synced.")
    await inter.response.send_message("\n".join(msg), ephemeral=True)

@bot.tree.command(name="ping", description="Quick health check")
async def ping(inter: discord.Interaction):
    await inter.response.send_message("Pong! 🏴‍☠️", ephemeral=True)
@bot.tree.command(name="twitch_list", description="Mods only: Show all users who have registered with the bot")
async def twitch_list(inter: discord.Interaction):
    if not is_mod(inter):
        return await inter.response.send_message("Only Fleet Commanders can use this.", ephemeral=True)

    rows = db_list_users()
    if not rows:
        return await inter.response.send_message("No Twitch users have registered yet.", ephemeral=True)

    lines = []
    for row in rows:
        discord_id = row['discord_user_id']
        display = row.get('display_name') or row['twitch_login']
        login = row['twitch_login']
        lines.append(f"<@{discord_id}> → **{display}** (`{login}`)")

    message = "\n".join(lines)
    if len(message) > 2000:
        # Break into chunks if too long
        chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
        await inter.response.send_message("📜 Registered Twitch Users:", ephemeral=True)
        for chunk in chunks:
            await inter.followup.send(chunk, ephemeral=True)
    else:
        await inter.response.send_message("📜 Registered Twitch Users:\n" + message, ephemeral=True)

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

        rows = db_list_users()
        twitch_ids = [r["twitch_id"] for r in rows]
        if not twitch_ids:
            continue

        streams = await bot.twitch.get_streams(twitch_ids)
        live_role = get_role_by_id_or_name(g, LIVE_ROLE_ID, LIVE_ROLE_NAME)

        for r in rows:
            tid = r["twitch_id"]
            stream = streams.get(tid)
            member = g.get_member(int(r["discord_user_id"]))

            if stream:
                stream_id = stream.get("id")
                if stream_id and not already_announced(tid, stream_id):
                    u = await bot.twitch.get_user_by_login(r["twitch_login"])
                    try:
                        await post_go_live(channel, stream, u)
                        db_set_stream_id(tid, stream_id)
                        print(f"[live] announced once for {r['twitch_login']} (stream_id={stream_id})")
                    except Exception as e:
                        print("Post error:", e)
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
