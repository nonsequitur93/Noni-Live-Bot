# bot.py — Noni's Pirate Go-Live Bot
# Requirements: discord.py, aiohttp, python-dotenv
# pip install discord.py aiohttp python-dotenv

import asyncio, json, os, time, aiohttp
from pathlib import Path
from typing import Dict, Optional
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# ── Load secrets ────────────────────────────────────────────────────────────────
load_dotenv()
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID      = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET  = os.getenv("TWITCH_CLIENT_SECRET")
GUILD_ID              = int(os.getenv("GUILD_ID", "0"))
MENTION_ROLE_ID       = os.getenv("MENTION_ROLE_ID")  # optional role to ping on alerts
LIVE_ROLE_ID          = os.getenv("LIVE_ROLE_ID")     # optional: role to give/remove while live

# ── Storage (JSON file, nice & simple) ─────────────────────────────────────────
DATA_PATH = Path("twitch_data.json")
DEFAULTS  = {"notify_channel_id": None, "users": {}, "live_cache": {}}
if not DATA_PATH.exists():
    DATA_PATH.write_text(json.dumps(DEFAULTS, indent=2))

def load_data():
    return json.loads(DATA_PATH.read_text())

def save_data(d):
    DATA_PATH.write_text(json.dumps(d, indent=2))

# ── Twitch API helper (client-credentials) ─────────────────────────────────────
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
        # multiple user_id params allowed
        params = [("user_id", uid) for uid in user_ids]
        async with self.sess.get(url, headers=await self._headers(), params=params) as r:
            j = await r.json()
            return {s["user_id"]: s for s in j.get("data", [])}

# ── Pretty helpers ─────────────────────────────────────────────────────────────
PIRATE_PURPLE = 0x8B5CF6  # 💜
PIRATE_EMOJI  = "🏴‍☠️"
MEGAPHONE     = "📣"
SPARKLES      = "✨"

def is_mod(inter: discord.Interaction) -> bool:
    perms = inter.user.guild_permissions
    return perms.administrator or perms.manage_guild

def already_announced(live_cache: dict, twitch_user_id: str, started_at: str) -> bool:
    """Prevent duplicate posts for the same stream session."""
    return live_cache.get(twitch_user_id) == started_at
def stream_preview_url(login: str, w=1280, h=720):
    # Twitch live preview template (updates server-side automatically)
    return f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-{w}x{h}.jpg"
def get_role(guild: discord.Guild, role_id: str | None):
    return guild.get_role(int(role_id)) if role_id else None

# ── Discord Bot ────────────────────────────────────────────────────────────────
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session: Optional[aiohttp.ClientSession] = None
        self.twitch: Optional[TwitchAPI] = None
        self.guild_obj: Optional[discord.Object] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.twitch  = TwitchAPI(self.session)
        if GUILD_ID:
            self.guild_obj = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=self.guild_obj)
            await self.tree.sync(guild=self.guild_obj)
        else:
            # Global sync fallback (commands can take up to 1h to appear)
            await self.tree.sync()
        check_live.start()

    async def close(self):
        await super().close()
        if self.session:
            await self.session.close()

bot = Bot()

# ── Slash Commands ─────────────────────────────────────────────────────────────
@bot.tree.command(name="twitch_set", description="Link your Twitch username")
@app_commands.describe(username="Your Twitch @ (login name, not display name)")
async def twitch_set(inter: discord.Interaction, username: str):
    await inter.response.defer(ephemeral=True)
    data = load_data()
    # Validate on Twitch so typos don't get saved
    u = await bot.twitch.get_user(username)
    if not u:
        return await inter.followup.send("I couldn't find that Twitch user. Double-check the spelling.", ephemeral=True)
    data["users"][str(inter.user.id)] = {"login": u["login"], "twitch_id": u["id"], "display": u.get("display_name", u["login"])}
    save_data(data)
    await inter.followup.send(f"{PIRATE_EMOJI} Aye! Linked your Twitch to **{u.get('display_name', u['login'])}**.", ephemeral=True)

@bot.tree.command(name="twitch_remove", description="Unlink your Twitch")
async def twitch_remove(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    data = load_data()
    data["users"].pop(str(inter.user.id), None)
    save_data(data)
    await inter.followup.send("Unlinked your Twitch. Fair winds!", ephemeral=True)

@bot.tree.command(name="twitch_list", description="See who registered (mods only)")
async def twitch_list(inter: discord.Interaction):
    if not is_mod(inter):
        return await inter.response.send_message("Only Fleet Commanders can use this.", ephemeral=True)
    data  = load_data()
    users = data.get("users", {})
    if not users:
        return await inter.response.send_message("No crewmates registered yet.", ephemeral=True)
    lines = []
    for uid, info in users.items():
        member = inter.guild.get_member(int(uid))
        name = member.display_name if member else f"User {uid}"
        lines.append(f"• **{name}** → `{info['login']}`")
    await inter.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="twitch_channel_set", description="Choose the go-live alerts channel (mods only)")
@app_commands.describe(channel="Channel where alerts should be posted")
async def twitch_channel_set(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_mod(inter):
        return await inter.response.send_message("Only Fleet Commanders can use this.", ephemeral=True)
    data = load_data()
    data["notify_channel_id"] = channel.id
    save_data(data)
    await inter.response.send_message(f"{PIRATE_EMOJI} Aye! I’ll hail the crew in {channel.mention}.", ephemeral=True)
@bot.tree.command(name="twitch_preview", description="Post a sample go-live embed (mods only)")
@app_commands.describe(channel="Channel to preview in (defaults to your notify channel)")
async def twitch_preview(inter: discord.Interaction, channel: discord.TextChannel | None = None):
    # mods only
    perms = inter.user.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        return await inter.response.send_message("Mods only.", ephemeral=True)

    data = load_data()
    chan_id = data.get("notify_channel_id")
    target = channel or (bot.get_channel(chan_id) if chan_id else None)
    if not isinstance(target, discord.TextChannel):
        return await inter.response.send_message("No notify channel set. Run /twitch_channel_set first.", ephemeral=True)

    # use your own link if you’re registered, else a sample
    users = data.get("users", {})
    uid = str(inter.user.id)
    info = users.get(uid) or next(iter(users.values()), None)
    if not info:
        return await inter.response.send_message("No registered users yet. Run /twitch_set first.", ephemeral=True)

    # fake stream data for preview
    from datetime import datetime, timezone
    stream = {
        "title": "Preview voyage across the Grand Line 🌊",
        "game_name": "Just Chatting",
        "viewer_count": 42,
        "started_at": datetime.now(timezone.utc).isoformat()
    }
    # fetch twitch user data
    u = await bot.twitch.get_user(info["login"])
    await post_go_live(target, stream, u)
    await inter.response.send_message(f"Preview sent to {target.mention}.", ephemeral=True)

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
    # Preview image (Twitch generates dynamic thumbs for live streams)
    embed.set_image(url=stream_preview_url(login))
    embed.set_footer(text="Twitch • Go-Live Alert")

    # Optional: author line (swap the icon URL for your brand mark if you want)
    embed.set_author(name="Crewmate Set Sail", icon_url="https://static-00.iconduck.com/assets.00/anchor-emoji-1024x1024-4n8e4b1w.png")

    content = f"{role_ping} {MEGAPHONE} Ahoy, Nakama! {display} just set sail → {url}" if role_ping else None
    await channel.send(content=content, embed=embed)

# ── Live checker (runs every 2 minutes) ────────────────────────────────────────
@tasks.loop(minutes=2)
async def check_live():
    data    = load_data()
    chan_id = data.get("notify_channel_id")
    if not chan_id:
        return

    channel = bot.get_channel(chan_id)
    if not isinstance(channel, discord.TextChannel):
        return

    users = data.get("users", {})
    if not users:
        return

    # Who should we check?
    user_ids  = [info["twitch_id"] for info in users.values()]
    streams   = await bot.twitch.get_streams(user_ids)
    live_cache = data.get("live_cache", {})

    # Get guild + LIVE role
    guild     = channel.guild
    live_role = get_role(guild, LIVE_ROLE_ID)

    # Loop through all registered users
    for uid, info in list(users.items()):
        tid    = info["twitch_id"]
        stream = streams.get(tid)
        member = guild.get_member(int(uid)) if guild else None

        if stream:
            # User is live → announce once per session
            u = await bot.twitch.get_user(info["login"])
            started_at = stream["started_at"]
            if not already_announced(live_cache, tid, started_at):
                try:
                    await post_go_live(channel, stream, u)
                    live_cache[tid] = started_at
                except Exception as e:
                    print("Post error:", e)

            # Give LIVE role while streaming
            if live_role and member and live_role not in member.roles:
                try:
                    await member.add_roles(live_role, reason="Now live")
                except Exception as e:
                    print("Add role error:", e)

        else:
            # Not live → clear cache so next stream can be announced again
            live_cache.pop(tid, None)

            # Remove LIVE role after stream ends
            if live_role and member and live_role in member.roles:
                try:
                    await member.remove_roles(live_role, reason="Stream ended")
                except Exception as e:
                    print("Remove role error:", e)

    data["live_cache"] = live_cache
    save_data(data)

@check_live.before_loop
async def before_check_live():
    await bot.wait_until_ready()


# ── Run it ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not (DISCORD_TOKEN and TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        raise SystemExit("Missing secrets. Check your .env file.")
    bot.run(DISCORD_TOKEN)

