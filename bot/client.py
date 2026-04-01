from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import discord
from discord import app_commands

from bot.commands import register_basic_commands, register_sound_commands
from bot.config import BotConfig
from bot.services import PlaybackManager, SoundStore, VoiceKeeperService

logger = logging.getLogger("voicecord.bot")


class KeeperSoundBot(discord.Client):
    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)

        self.config = config
        self.tree = app_commands.CommandTree(self)

        self.home_channels: dict[int, int] = {}
        self.tracked_guilds: set[int] = set()
        self.last_connect_attempt: dict[int, float] = {}
        self.last_voice_disconnect: dict[int, float] = {}

        self.watchdog_task: asyncio.Task[None] | None = None
        self.active_playback_guilds: set[int] = set()
        self.guild_connect_locks: dict[int, asyncio.Lock] = {}

        self.sound_storage_dir = Path(self.config.soundboard_storage_dir)
        self.sound_storage_dir.mkdir(parents=True, exist_ok=True)

        self.sound_store = SoundStore(self.config.soundboard_db_path)
        self.voice_keeper = VoiceKeeperService(self)
        self.playback_manager = PlaybackManager(self)

    def get_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.guild_connect_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self.guild_connect_locks[guild_id] = lock
        return lock

    async def setup_hook(self) -> None:
        self._register_commands()

        guild_ids = list(self.config.sync_guild_ids)
        if self.config.default_guild_id and self.config.default_guild_id not in guild_ids:
            guild_ids.append(self.config.default_guild_id)

        if guild_ids:
            if self.config.purge_global_commands_on_guild_sync:
                self.tree.clear_commands(guild=None)
                removed = await self._sync_commands_with_timeout(guild=None, scope="global purge")
                logger.info("Global commands purged (%s removed).", len(removed))
                self._register_commands()

            for guild_id in guild_ids:
                guild_obj = discord.Object(id=guild_id)
                logger.info("Syncing slash commands to guild %s...", guild_id)
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self._sync_commands_with_timeout(guild=guild_obj, scope=f"guild {guild_id}")
                logger.info("Slash commands synced to guild %s (%s commands).", guild_id, len(synced))
            return

        synced = await self._sync_commands_with_timeout(guild=None, scope="global")
        logger.info("Global slash commands synced (%s commands).", len(synced))

    def _register_commands(self) -> None:
        register_basic_commands(self.tree, self)
        register_sound_commands(self.tree, self)

    async def _sync_commands_with_timeout(
        self,
        *,
        guild: discord.abc.Snowflake | None,
        scope: str,
    ) -> list[app_commands.AppCommand]:
        try:
            return await asyncio.wait_for(
                self.tree.sync(guild=guild),
                timeout=self.config.command_sync_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Slash command sync timed out for %s after %ss.",
                scope,
                self.config.command_sync_timeout_seconds,
            )
            return []
        except Exception as err:
            logger.exception("Slash command sync failed for %s: %s", scope, err)
            return []

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

        if self.watchdog_task is None:
            self.watchdog_task = asyncio.create_task(self.voice_keeper.run_watchdog(), name="voice-watchdog")

        if self.config.default_guild_id and self.config.default_channel_id:
            self.home_channels[self.config.default_guild_id] = self.config.default_channel_id
            self.tracked_guilds.add(self.config.default_guild_id)
            await self.voice_keeper.ensure_connected(self.config.default_guild_id)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        await self.voice_keeper.handle_voice_state_update(member, before, after)

    async def close(self) -> None:
        if self.watchdog_task:
            self.watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog_task
        await super().close()
