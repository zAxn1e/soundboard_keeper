import asyncio
import contextlib
import logging
import os
import time
from typing import Dict, Optional, Set, Tuple

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("voicecord.bot")

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
DEFAULT_GUILD_ID = int(os.getenv("DEFAULT_GUILD_ID", "0"))
DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "0"))
SELF_DEAF = os.getenv("SELF_DEAF", "true").lower() == "true"
SELF_MUTE = os.getenv("SELF_MUTE", "true").lower() == "true"

# Comma-separated guild IDs for instant slash command sync, e.g. "123,456"
SYNC_GUILD_IDS = [
    int(gid.strip())
    for gid in os.getenv("SYNC_GUILD_IDS", "").split(",")
    if gid.strip().isdigit()
]

WATCHDOG_INTERVAL_SECONDS = int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "20"))
CONNECT_RETRY_LIMIT = int(os.getenv("CONNECT_RETRY_LIMIT", "4"))
MIN_RECONNECT_INTERVAL_SECONDS = int(os.getenv("MIN_RECONNECT_INTERVAL_SECONDS", "12"))
VOICE_RECOVERY_GRACE_SECONDS = int(os.getenv("VOICE_RECOVERY_GRACE_SECONDS", "35"))
COMMAND_SYNC_TIMEOUT_SECONDS = int(os.getenv("COMMAND_SYNC_TIMEOUT_SECONDS", "30"))
PURGE_GLOBAL_COMMANDS_ON_GUILD_SYNC = os.getenv(
    "PURGE_GLOBAL_COMMANDS_ON_GUILD_SYNC", "true"
).lower() == "true"

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN (or TOKEN) in environment.")


class VoiceKeeperBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.home_channels: Dict[int, int] = {}
        self.tracked_guilds: Set[int] = set()
        self.watchdog_task: Optional[asyncio.Task] = None
        self.guild_connect_locks: Dict[int, asyncio.Lock] = {}
        self.last_connect_attempt: Dict[int, float] = {}
        self.last_voice_disconnect: Dict[int, float] = {}

    def _get_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.guild_connect_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self.guild_connect_locks[guild_id] = lock
        return lock

    async def setup_hook(self) -> None:
        setup_commands(self.tree, self)

        # Guild sync appears almost immediately. Global sync can take up to ~1 hour.
        guild_ids = list(SYNC_GUILD_IDS)
        if DEFAULT_GUILD_ID and DEFAULT_GUILD_ID not in guild_ids:
            guild_ids.append(DEFAULT_GUILD_ID)

        if guild_ids:
            if PURGE_GLOBAL_COMMANDS_ON_GUILD_SYNC:
                # Remove old global commands that can appear as duplicates with guild commands.
                self.tree.clear_commands(guild=None)
                removed = await self._sync_commands_with_timeout(guild=None, scope="global purge")
                logger.info("Global commands purged (%s removed).", len(removed))

                # Re-register local commands so we can copy them to guild scope.
                setup_commands(self.tree, self)

            for guild_id in guild_ids:
                guild_obj = discord.Object(id=guild_id)
                logger.info("Syncing slash commands to guild %s...", guild_id)
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self._sync_commands_with_timeout(guild=guild_obj, scope=f"guild {guild_id}")
                logger.info("Slash commands synced to guild %s (%s commands).", guild_id, len(synced))
            return

        synced = await self._sync_commands_with_timeout(guild=None, scope="global")
        logger.info("Global slash commands synced (%s commands).", len(synced))

    async def _sync_commands_with_timeout(
        self, *, guild: Optional[discord.abc.Snowflake], scope: str
    ) -> list[app_commands.AppCommand]:
        try:
            return await asyncio.wait_for(
                self.tree.sync(guild=guild),
                timeout=COMMAND_SYNC_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Slash command sync timed out for %s after %ss.",
                scope,
                COMMAND_SYNC_TIMEOUT_SECONDS,
            )
            return []
        except Exception as err:  # noqa: BLE001
            logger.exception("Slash command sync failed for %s: %s", scope, err)
            return []

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        if self.watchdog_task is None:
            self.watchdog_task = asyncio.create_task(self.voice_watchdog(), name="voice-watchdog")

        if DEFAULT_GUILD_ID and DEFAULT_CHANNEL_ID:
            self.home_channels[DEFAULT_GUILD_ID] = DEFAULT_CHANNEL_ID
            self.tracked_guilds.add(DEFAULT_GUILD_ID)
            await self.ensure_connected(DEFAULT_GUILD_ID)

    async def close(self) -> None:
        if self.watchdog_task:
            self.watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog_task
        await super().close()

    async def connect_to_channel(self, guild_id: int, channel_id: int) -> Tuple[bool, str]:
        self.last_connect_attempt[guild_id] = time.monotonic()

        guild = self.get_guild(guild_id)
        if guild is None:
            return False, f"Guild {guild_id} not found in bot cache."

        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel | discord.StageChannel):
            return False, f"Channel {channel_id} is missing or not a voice/stage channel."

        existing = guild.voice_client
        if existing and existing.is_connected():
            if existing.channel and existing.channel.id == channel.id:
                return True, f"Already connected to {channel.mention}."
            await existing.move_to(channel)
            return True, f"Moved to {channel.mention}."

        delay = 2
        for attempt in range(1, CONNECT_RETRY_LIMIT + 1):
            try:
                # Keep reconnect ownership in watchdog to avoid overlapping reconnect loops.
                await channel.connect(self_deaf=SELF_DEAF, self_mute=SELF_MUTE, reconnect=False)
                return True, f"Connected to {channel.mention}."
            except Exception as err:  # noqa: BLE001
                logger.warning("Connect attempt %s failed for guild %s: %s", attempt, guild_id, err)
                if attempt == CONNECT_RETRY_LIMIT:
                    return False, f"Failed to connect after {CONNECT_RETRY_LIMIT} attempts: {err}"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)

        return False, "Unknown connection failure."

    async def ensure_connected(self, guild_id: int) -> None:
        channel_id = self.home_channels.get(guild_id)
        if not channel_id:
            return

        guild = self.get_guild(guild_id)
        if guild is None:
            return

        async with self._get_connect_lock(guild_id):
            vc = guild.voice_client

            # Already healthy in target channel.
            if vc and vc.is_connected() and vc.channel and vc.channel.id == channel_id:
                return

            # Let discord.py finish in-progress voice recovery before we force anything.
            if vc and (
                getattr(vc, "_reconnecting", False)
                or getattr(vc, "_potentially_reconnecting", False)
                or getattr(vc, "_handshaking", False)
            ):
                return

            # If a disconnect happened very recently, give voice state recovery a short grace window.
            last_drop = self.last_voice_disconnect.get(guild_id, 0)
            if time.monotonic() - last_drop < VOICE_RECOVERY_GRACE_SECONDS:
                return

            # Avoid reconnect storms while discord.py is already recovering.
            last_attempt = self.last_connect_attempt.get(guild_id, 0)
            if time.monotonic() - last_attempt < MIN_RECONNECT_INTERVAL_SECONDS:
                return

            # Stale voice client objects can block clean reconnects (4006/1006 loops).
            if vc and not vc.is_connected():
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)

            ok, msg = await self.connect_to_channel(guild_id, channel_id)
            if ok:
                logger.info("[guild=%s] %s", guild_id, msg)
            else:
                logger.warning("[guild=%s] %s", guild_id, msg)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self.user or member.id != self.user.id:
            return

        # Track bot-side drops so watchdog can avoid colliding with built-in recovery.
        if before.channel is not None and after.channel is None and member.guild:
            self.last_voice_disconnect[member.guild.id] = time.monotonic()

    async def voice_watchdog(self) -> None:
        await self.wait_until_ready()
        logger.info("Voice watchdog started (%ss interval).", WATCHDOG_INTERVAL_SECONDS)

        while not self.is_closed():
            guild_ids = list(self.tracked_guilds)
            for guild_id in guild_ids:
                try:
                    await self.ensure_connected(guild_id)
                except Exception as err:  # noqa: BLE001
                    logger.exception("Watchdog error in guild %s: %s", guild_id, err)

            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)


def setup_commands(tree: app_commands.CommandTree, bot: VoiceKeeperBot) -> None:
    @tree.command(name="join", description="Join a voice/stage channel and keep reconnecting.")
    @app_commands.describe(channel="Target voice or stage channel")
    async def join_command(interaction: discord.Interaction, channel: discord.VoiceChannel | discord.StageChannel):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        bot.home_channels[guild.id] = channel.id
        bot.tracked_guilds.add(guild.id)
        async with bot._get_connect_lock(guild.id):
            ok, msg = await bot.connect_to_channel(guild.id, channel.id)
        await interaction.followup.send(msg if ok else f"Error: {msg}", ephemeral=True)

    @tree.command(name="set_home", description="Set home VC channel for auto-reconnect.")
    @app_commands.describe(channel="Voice or stage channel to keep alive")
    async def set_home_command(interaction: discord.Interaction, channel: discord.VoiceChannel | discord.StageChannel):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        bot.home_channels[guild.id] = channel.id
        bot.tracked_guilds.add(guild.id)
        await interaction.response.send_message(
            f"Home channel set to {channel.mention}. Watchdog will keep reconnecting.",
            ephemeral=True,
        )

    @tree.command(name="leave", description="Leave VC and stop watchdog for this server.")
    async def leave_command(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        bot.tracked_guilds.discard(guild.id)
        bot.home_channels.pop(guild.id, None)

        vc = guild.voice_client
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
            await interaction.response.send_message("Disconnected and watchdog stopped for this server.", ephemeral=True)
            return

        await interaction.response.send_message("Not connected to voice right now.", ephemeral=True)

    @tree.command(name="status", description="Show current VC keep-alive status.")
    async def status_command(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        tracked = guild.id in bot.tracked_guilds
        home_id = bot.home_channels.get(guild.id)
        vc = guild.voice_client
        current = vc.channel.mention if vc and vc.is_connected() and vc.channel else "Not connected"
        home_text = f"<#{home_id}>" if home_id else "Not set"

        await interaction.response.send_message(
            f"Tracked: **{tracked}**\nHome: {home_text}\nCurrent: {current}",
            ephemeral=True,
        )

    @tree.command(name="ping", description="Bot health check.")
    async def ping_command(interaction: discord.Interaction):
        await interaction.response.send_message(f"Pong: {round(bot.latency * 1000)} ms", ephemeral=True)


if __name__ == "__main__":
    bot = VoiceKeeperBot()
    bot.run(BOT_TOKEN, reconnect=True, log_handler=None)