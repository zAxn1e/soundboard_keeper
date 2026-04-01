import asyncio
import contextlib
import datetime as dt
import logging
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
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

SOUNDBOARD_DISCONNECT_AFTER_PLAY = os.getenv(
    "SOUNDBOARD_DISCONNECT_AFTER_PLAY", "true"
).lower() == "true"
SOUNDBOARD_MAX_FILE_SIZE_MB = int(os.getenv("SOUNDBOARD_MAX_FILE_SIZE_MB", "10"))
SOUNDBOARD_ALLOWED_EXTENSIONS = tuple(
    ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
    for ext in os.getenv("SOUNDBOARD_ALLOWED_EXTENSIONS", ".mp3,.wav,.ogg,.m4a").split(",")
    if ext.strip()
)
SOUNDBOARD_STORAGE_DIR = os.getenv("SOUNDBOARD_STORAGE_DIR", "sounds")
SOUNDBOARD_DB_PATH = os.getenv("SOUNDBOARD_DB_PATH", "soundboard.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN (or TOKEN) in environment.")


@dataclass(frozen=True)
class SoundRecord:
    guild_id: int
    name: str
    name_key: str
    file_path: str
    volume: int
    uploader_user_id: int
    created_at: str


def normalize_sound_name(name: str) -> str:
    return " ".join(name.split()).strip()


def sound_name_key(name: str) -> str:
    return normalize_sound_name(name).casefold()


class SoundStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent != Path(""):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sounds (
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                file_path TEXT NOT NULL,
                volume INTEGER NOT NULL,
                uploader_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, name_key)
            )
            """
        )
        self._conn.commit()

    def add_sound(
        self,
        *,
        guild_id: int,
        name: str,
        file_path: str,
        volume: int,
        uploader_user_id: int,
    ) -> SoundRecord:
        clean_name = normalize_sound_name(name)
        key = sound_name_key(clean_name)
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            self._conn.execute(
                """
                INSERT INTO sounds (guild_id, name, name_key, file_path, volume, uploader_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, clean_name, key, file_path, volume, uploader_user_id, created_at),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as err:
            raise ValueError("duplicate") from err

        return SoundRecord(
            guild_id=guild_id,
            name=clean_name,
            name_key=key,
            file_path=file_path,
            volume=volume,
            uploader_user_id=uploader_user_id,
            created_at=created_at,
        )

    def get_sound(self, guild_id: int, name: str) -> Optional[SoundRecord]:
        key = sound_name_key(name)
        row = self._conn.execute(
            """
            SELECT guild_id, name, name_key, file_path, volume, uploader_user_id, created_at
            FROM sounds
            WHERE guild_id = ? AND name_key = ?
            """,
            (guild_id, key),
        ).fetchone()
        return self._row_to_record(row)

    def list_sounds(self, guild_id: int) -> list[SoundRecord]:
        rows = self._conn.execute(
            """
            SELECT guild_id, name, name_key, file_path, volume, uploader_user_id, created_at
            FROM sounds
            WHERE guild_id = ?
            ORDER BY name COLLATE NOCASE ASC
            """,
            (guild_id,),
        ).fetchall()
        return [self._row_to_record(row) for row in rows if row is not None]

    def search_names(self, guild_id: int, query: str, limit: int = 25) -> list[str]:
        pattern = f"%{query.strip().casefold()}%"
        rows = self._conn.execute(
            """
            SELECT name
            FROM sounds
            WHERE guild_id = ? AND name_key LIKE ?
            ORDER BY name COLLATE NOCASE ASC
            LIMIT ?
            """,
            (guild_id, pattern, max(1, limit)),
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def update_sound(
        self,
        guild_id: int,
        name: str,
        *,
        new_name: Optional[str],
        new_volume: Optional[int],
    ) -> SoundRecord:
        current = self.get_sound(guild_id, name)
        if current is None:
            raise KeyError("missing")

        target_name = normalize_sound_name(new_name) if new_name is not None else current.name
        target_key = sound_name_key(target_name)
        target_volume = current.volume if new_volume is None else new_volume

        if target_key != current.name_key:
            existing = self.get_sound(guild_id, target_name)
            if existing is not None:
                raise ValueError("duplicate")

        self._conn.execute(
            """
            UPDATE sounds
            SET name = ?, name_key = ?, volume = ?
            WHERE guild_id = ? AND name_key = ?
            """,
            (target_name, target_key, target_volume, guild_id, current.name_key),
        )
        self._conn.commit()

        updated = self.get_sound(guild_id, target_name)
        if updated is None:
            raise RuntimeError("failed to fetch updated sound")
        return updated

    def delete_sound(self, guild_id: int, name: str) -> Optional[SoundRecord]:
        current = self.get_sound(guild_id, name)
        if current is None:
            return None

        self._conn.execute(
            """
            DELETE FROM sounds
            WHERE guild_id = ? AND name_key = ?
            """,
            (guild_id, current.name_key),
        )
        self._conn.commit()
        return current

    @staticmethod
    def _row_to_record(row: Optional[sqlite3.Row]) -> Optional[SoundRecord]:
        if row is None:
            return None
        return SoundRecord(
            guild_id=int(row["guild_id"]),
            name=str(row["name"]),
            name_key=str(row["name_key"]),
            file_path=str(row["file_path"]),
            volume=int(row["volume"]),
            uploader_user_id=int(row["uploader_user_id"]),
            created_at=str(row["created_at"]),
        )


class PlaybackManager:
    def __init__(self, bot: "VoiceKeeperBot") -> None:
        self.bot = bot
        self.guild_play_locks: Dict[int, asyncio.Lock] = {}

    def _get_play_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.guild_play_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self.guild_play_locks[guild_id] = lock
        return lock

    async def play_sound_in_channel(
        self,
        *,
        guild: discord.Guild,
        target_channel: discord.VoiceChannel | discord.StageChannel,
        sound: SoundRecord,
    ) -> Tuple[bool, str]:
        guild_id = guild.id
        lock = self._get_play_lock(guild_id)

        async with lock:
            self.bot.active_playback_guilds.add(guild_id)
            try:
                async with self.bot._get_connect_lock(guild_id):
                    ok, msg = await self.bot.connect_to_channel(guild_id, target_channel.id)
                if not ok:
                    return False, msg

                voice_client = guild.voice_client
                if voice_client is None or not voice_client.is_connected():
                    return False, "Voice connection failed to initialize."

                if voice_client.is_playing():
                    voice_client.stop()

                source = discord.FFmpegPCMAudio(sound.file_path)
                source = discord.PCMVolumeTransformer(source, volume=max(0.0, sound.volume / 100.0))

                loop = asyncio.get_running_loop()
                finished = asyncio.Event()
                playback_error: Dict[str, str] = {}

                def after_play(err: Optional[Exception]) -> None:
                    if err is not None:
                        playback_error["error"] = str(err)
                    loop.call_soon_threadsafe(finished.set)

                try:
                    voice_client.play(source, after=after_play)
                except Exception as err:  # noqa: BLE001
                    logger.exception("Failed to start playback in guild %s: %s", guild_id, err)
                    return False, f"Unable to start playback: {err}"

                await finished.wait()

                if playback_error:
                    return False, f"Playback error: {playback_error['error']}"

                await self._post_playback_cleanup(guild)
                return True, f"Played **{sound.name}**."
            finally:
                self.bot.active_playback_guilds.discard(guild_id)

    async def _post_playback_cleanup(self, guild: discord.Guild) -> None:
        guild_id = guild.id
        voice_client = guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            return

        keeper_enabled = guild_id in self.bot.tracked_guilds and guild_id in self.bot.home_channels
        if keeper_enabled:
            home_channel_id = self.bot.home_channels[guild_id]
            if voice_client.channel and voice_client.channel.id != home_channel_id:
                async with self.bot._get_connect_lock(guild_id):
                    ok, msg = await self.bot.connect_to_channel(guild_id, home_channel_id)
                if ok:
                    logger.info("[guild=%s] Returned to keeper home after playback.", guild_id)
                else:
                    logger.warning("[guild=%s] Could not return to keeper home: %s", guild_id, msg)
            return

        if SOUNDBOARD_DISCONNECT_AFTER_PLAY:
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)


def extension_from_filename(filename: str) -> str:
    return Path(filename).suffix.lower()


def make_storage_filename(original_filename: str) -> str:
    ext = extension_from_filename(original_filename)
    return f"{uuid.uuid4().hex}{ext}"


def is_safe_child_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


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
        self.active_playback_guilds: Set[int] = set()

        self.sound_storage_dir = Path(SOUNDBOARD_STORAGE_DIR)
        self.sound_storage_dir.mkdir(parents=True, exist_ok=True)
        self.sound_store = SoundStore(SOUNDBOARD_DB_PATH)
        self.playback_manager = PlaybackManager(self)

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

        if guild_id in self.active_playback_guilds:
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

    sound_group = app_commands.Group(name="sound", description="Per-guild soundboard commands")

    async def sound_name_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        names = bot.sound_store.search_names(guild.id, current, limit=25)
        return [app_commands.Choice(name=name, value=name) for name in names]

    @sound_group.command(name="add", description="Upload and save a new guild sound.")
    @app_commands.describe(name="Sound name", file="Audio file", volume="Volume percent (1-200)")
    async def sound_add_command(
        interaction: discord.Interaction,
        name: str,
        file: discord.Attachment,
        volume: Optional[int] = 100,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        clean_name = normalize_sound_name(name)
        if not clean_name:
            await interaction.response.send_message("Sound name cannot be empty.", ephemeral=True)
            return

        if volume is None:
            volume = 100
        if volume < 1 or volume > 200:
            await interaction.response.send_message("Volume must be between 1 and 200.", ephemeral=True)
            return

        ext = extension_from_filename(file.filename)
        if ext not in SOUNDBOARD_ALLOWED_EXTENSIONS:
            allowed = ", ".join(SOUNDBOARD_ALLOWED_EXTENSIONS)
            await interaction.response.send_message(
                f"Unsupported file type. Allowed: {allowed}",
                ephemeral=True,
            )
            return

        max_bytes = max(1, SOUNDBOARD_MAX_FILE_SIZE_MB) * 1024 * 1024
        if file.size > max_bytes:
            await interaction.response.send_message(
                f"File is too large. Max size is {SOUNDBOARD_MAX_FILE_SIZE_MB} MB.",
                ephemeral=True,
            )
            return

        if bot.sound_store.get_sound(guild.id, clean_name) is not None:
            await interaction.response.send_message(
                f"A sound named **{clean_name}** already exists in this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_dir = bot.sound_storage_dir / str(guild.id)
        guild_dir.mkdir(parents=True, exist_ok=True)
        saved_path = guild_dir / make_storage_filename(file.filename)

        try:
            with saved_path.open("wb") as fp:
                await file.save(fp)

            bot.sound_store.add_sound(
                guild_id=guild.id,
                name=clean_name,
                file_path=str(saved_path),
                volume=volume,
                uploader_user_id=interaction.user.id,
            )
        except ValueError:
            with contextlib.suppress(Exception):
                saved_path.unlink(missing_ok=True)
            await interaction.followup.send(
                f"A sound named **{clean_name}** already exists in this server.",
                ephemeral=True,
            )
            return
        except Exception as err:  # noqa: BLE001
            logger.exception("Failed to add sound in guild %s: %s", guild.id, err)
            with contextlib.suppress(Exception):
                saved_path.unlink(missing_ok=True)
            await interaction.followup.send(
                f"Failed to save sound: {err}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Saved sound **{clean_name}** at {volume}% volume.",
            ephemeral=True,
        )

    @sound_group.command(name="play", description="Play a saved sound in your voice channel.")
    @app_commands.describe(name="Sound name")
    @app_commands.autocomplete(name=sound_name_autocomplete)
    async def sound_play_command(interaction: discord.Interaction, name: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        if shutil.which("ffmpeg") is None:
            await interaction.response.send_message(
                "FFmpeg is not installed or not in PATH. Install FFmpeg to use sound playback.",
                ephemeral=True,
            )
            return

        sound = bot.sound_store.get_sound(guild.id, name)
        if sound is None:
            await interaction.response.send_message(
                f"No sound named **{normalize_sound_name(name)}** was found.",
                ephemeral=True,
            )
            return

        sound_path = Path(sound.file_path)
        if not sound_path.exists():
            await interaction.response.send_message(
                "This sound file is missing on disk. Delete and re-upload it.",
                ephemeral=True,
            )
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                "Join a voice or stage channel first.",
                ephemeral=True,
            )
            return

        target_channel = member.voice.channel
        if not isinstance(target_channel, discord.VoiceChannel | discord.StageChannel):
            await interaction.response.send_message(
                "Join a voice or stage channel first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        ok, msg = await bot.playback_manager.play_sound_in_channel(
            guild=guild,
            target_channel=target_channel,
            sound=sound,
        )

        if ok:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send(f"Error: {msg}", ephemeral=True)

    @sound_group.command(name="edit", description="Edit sound metadata.")
    @app_commands.describe(
        name="Existing sound name",
        new_name="New sound name",
        volume="New volume percent (1-200)",
    )
    @app_commands.autocomplete(name=sound_name_autocomplete)
    async def sound_edit_command(
        interaction: discord.Interaction,
        name: str,
        new_name: Optional[str] = None,
        volume: Optional[int] = None,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        if new_name is None and volume is None:
            await interaction.response.send_message(
                "Provide at least one field to edit.",
                ephemeral=True,
            )
            return

        normalized_new_name: Optional[str] = None
        if new_name is not None:
            normalized_new_name = normalize_sound_name(new_name)
            if not normalized_new_name:
                await interaction.response.send_message("New name cannot be empty.", ephemeral=True)
                return

        if volume is not None and (volume < 1 or volume > 200):
            await interaction.response.send_message("Volume must be between 1 and 200.", ephemeral=True)
            return

        try:
            updated = bot.sound_store.update_sound(
                guild.id,
                name,
                new_name=normalized_new_name,
                new_volume=volume,
            )
        except KeyError:
            await interaction.response.send_message("Sound not found.", ephemeral=True)
            return
        except ValueError:
            await interaction.response.send_message(
                "A sound with that new name already exists.",
                ephemeral=True,
            )
            return
        except Exception as err:  # noqa: BLE001
            logger.exception("Failed to edit sound in guild %s: %s", guild.id, err)
            await interaction.response.send_message(f"Failed to edit sound: {err}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Updated **{updated.name}** (volume: {updated.volume}%).",
            ephemeral=True,
        )

    @sound_group.command(name="delete", description="Delete a saved sound.")
    @app_commands.describe(name="Existing sound name")
    @app_commands.autocomplete(name=sound_name_autocomplete)
    async def sound_delete_command(interaction: discord.Interaction, name: str):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        try:
            removed = bot.sound_store.delete_sound(guild.id, name)
        except Exception as err:  # noqa: BLE001
            logger.exception("Failed to delete sound metadata in guild %s: %s", guild.id, err)
            await interaction.response.send_message(f"Failed to delete sound: {err}", ephemeral=True)
            return

        if removed is None:
            await interaction.response.send_message("Sound not found.", ephemeral=True)
            return

        file_path = Path(removed.file_path)
        if is_safe_child_path(file_path, bot.sound_storage_dir):
            with contextlib.suppress(Exception):
                file_path.unlink(missing_ok=True)

        await interaction.response.send_message(
            f"Deleted sound **{removed.name}**.",
            ephemeral=True,
        )

    @sound_group.command(name="list", description="List available sounds in this server.")
    async def sound_list_command(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        sounds = bot.sound_store.list_sounds(guild.id)
        if not sounds:
            await interaction.response.send_message("No sounds saved for this server yet.", ephemeral=True)
            return

        max_lines = 25
        visible = sounds[:max_lines]
        lines = [f"- **{sound.name}** ({sound.volume}%)" for sound in visible]
        if len(sounds) > max_lines:
            lines.append(f"...and {len(sounds) - max_lines} more")

        embed = discord.Embed(
            title=f"Soundboard ({len(sounds)} sounds)",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    tree.add_command(sound_group)


if __name__ == "__main__":
    bot = VoiceKeeperBot()
    bot.run(BOT_TOKEN, reconnect=True, log_handler=None)