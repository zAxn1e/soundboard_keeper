from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Optional

import discord

from bot.services.sound_store import SoundRecord

if TYPE_CHECKING:
    from bot.client import KeeperSoundBot


logger = logging.getLogger("voicecord.bot")


class PlaybackManager:
    def __init__(self, bot: "KeeperSoundBot") -> None:
        self.bot = bot
        self.guild_play_locks: dict[int, asyncio.Lock] = {}

    def get_play_lock(self, guild_id: int) -> asyncio.Lock:
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
    ) -> tuple[bool, str]:
        guild_id = guild.id
        async with self.get_play_lock(guild_id):
            self.bot.active_playback_guilds.add(guild_id)
            try:
                async with self.bot.get_connect_lock(guild_id):
                    ok, msg = await self.bot.voice_keeper.connect_to_channel(guild_id, target_channel.id)
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
                done = asyncio.Event()
                playback_error: dict[str, str] = {}

                def after_play(err: Optional[Exception]) -> None:
                    if err is not None:
                        playback_error["error"] = str(err)
                    loop.call_soon_threadsafe(done.set)

                try:
                    voice_client.play(source, after=after_play)
                except Exception as err:
                    logger.exception("Failed to start playback in guild %s: %s", guild_id, err)
                    return False, f"Unable to start playback: {err}"

                await done.wait()

                if playback_error:
                    return False, f"Playback error: {playback_error['error']}"

                await self.post_playback_cleanup(guild)
                return True, f"Played **{sound.name}**."
            finally:
                self.bot.active_playback_guilds.discard(guild_id)

    async def post_playback_cleanup(self, guild: discord.Guild) -> None:
        guild_id = guild.id
        voice_client = guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            return

        keeper_enabled = guild_id in self.bot.tracked_guilds and guild_id in self.bot.home_channels
        if keeper_enabled:
            home_channel_id = self.bot.home_channels[guild_id]
            if voice_client.channel and voice_client.channel.id != home_channel_id:
                async with self.bot.get_connect_lock(guild_id):
                    ok, msg = await self.bot.voice_keeper.connect_to_channel(guild_id, home_channel_id)
                if ok:
                    logger.info("[guild=%s] Returned to keeper home after playback.", guild_id)
                else:
                    logger.warning("[guild=%s] Could not return to keeper home: %s", guild_id, msg)
            return

        if self.bot.config.soundboard_disconnect_after_play:
            with contextlib.suppress(Exception):
                await voice_client.disconnect(force=True)
