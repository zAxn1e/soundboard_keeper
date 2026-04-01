from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from bot.client import KeeperSoundBot


def register_basic_commands(tree: app_commands.CommandTree[KeeperSoundBot], bot: KeeperSoundBot) -> None:
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
        async with bot.get_connect_lock(guild.id):
            ok, msg = await bot.voice_keeper.connect_to_channel(guild.id, channel.id)
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
