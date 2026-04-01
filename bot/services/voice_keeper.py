from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot.client import KeeperSoundBot


logger = logging.getLogger("voicecord.bot")


class VoiceKeeperService:
    def __init__(self, bot: "KeeperSoundBot") -> None:
        self.bot = bot

    async def connect_to_channel(self, guild_id: int, channel_id: int) -> tuple[bool, str]:
        self.bot.last_connect_attempt[guild_id] = time.monotonic()

        guild = self.bot.get_guild(guild_id)
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
        for attempt in range(1, self.bot.config.connect_retry_limit + 1):
            try:
                await channel.connect(
                    self_deaf=self.bot.config.self_deaf,
                    self_mute=self.bot.config.self_mute,
                    reconnect=False,
                )
                return True, f"Connected to {channel.mention}."
            except Exception as err:
                logger.warning("Connect attempt %s failed for guild %s: %s", attempt, guild_id, err)
                if attempt == self.bot.config.connect_retry_limit:
                    return False, f"Failed to connect after {self.bot.config.connect_retry_limit} attempts: {err}"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)

        return False, "Unknown connection failure."

    async def ensure_connected(self, guild_id: int) -> None:
        channel_id = self.bot.home_channels.get(guild_id)
        if not channel_id:
            return

        if guild_id in self.bot.active_playback_guilds:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        async with self.bot.get_connect_lock(guild_id):
            vc = guild.voice_client

            if vc and vc.is_connected() and vc.channel and vc.channel.id == channel_id:
                return

            if vc and (
                getattr(vc, "_reconnecting", False)
                or getattr(vc, "_potentially_reconnecting", False)
                or getattr(vc, "_handshaking", False)
            ):
                return

            last_drop = self.bot.last_voice_disconnect.get(guild_id, 0.0)
            if time.monotonic() - last_drop < self.bot.config.voice_recovery_grace_seconds:
                return

            last_attempt = self.bot.last_connect_attempt.get(guild_id, 0.0)
            if time.monotonic() - last_attempt < self.bot.config.min_reconnect_interval_seconds:
                return

            if vc and not vc.is_connected():
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)

            ok, msg = await self.connect_to_channel(guild_id, channel_id)
            if ok:
                logger.info("[guild=%s] %s", guild_id, msg)
            else:
                logger.warning("[guild=%s] %s", guild_id, msg)

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self.bot.user or member.id != self.bot.user.id:
            return

        if before.channel is not None and after.channel is None and member.guild:
            self.bot.last_voice_disconnect[member.guild.id] = time.monotonic()

    async def run_watchdog(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("Voice watchdog started (%ss interval).", self.bot.config.watchdog_interval_seconds)

        while not self.bot.is_closed():
            for guild_id in list(self.bot.tracked_guilds):
                try:
                    await self.ensure_connected(guild_id)
                except Exception as err:
                    logger.exception("Watchdog error in guild %s: %s", guild_id, err)
            await asyncio.sleep(self.bot.config.watchdog_interval_seconds)
