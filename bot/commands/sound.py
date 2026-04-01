from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands

from bot.services.sound_store import SoundRecord
from bot.utils.audio import derive_category, ffmpeg_available, normalize_category, normalize_sound_name
from bot.utils.filenames import extension_from_filename, is_safe_child_path, make_storage_filename

if TYPE_CHECKING:
    from bot.client import KeeperSoundBot

logger = logging.getLogger("voicecord.bot")


def _build_category_summary_lines(categories: list[tuple[str, int]], max_lines: int = 25) -> list[str]:
    visible = categories[:max_lines]
    lines = [f"- **{category}** ({count})" for category, count in visible]
    if len(categories) > max_lines:
        lines.append(f"...and {len(categories) - max_lines} more")
    return lines


def _build_sound_lines(sounds: list[SoundRecord], max_lines: int = 25) -> list[str]:
    visible = sounds[:max_lines]
    lines = [f"- **{sound.name}** ({sound.volume}%)" for sound in visible]
    if len(sounds) > max_lines:
        lines.append(f"...and {len(sounds) - max_lines} more")
    return lines


def register_sound_commands(tree: app_commands.CommandTree[KeeperSoundBot], bot: KeeperSoundBot) -> None:
    sound_group = app_commands.Group(name="sound", description="Per-guild soundboard commands")

    async def category_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        categories = bot.sound_store.search_categories(guild.id, current, limit=25)
        return [app_commands.Choice(name=category, value=category) for category in categories]

    async def sound_name_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []

        selected_category = normalize_sound_name(getattr(interaction.namespace, "category", ""))
        names = bot.sound_store.search_names(
            guild.id,
            current,
            limit=25,
            category=selected_category if selected_category else None,
        )
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
        if ext not in bot.config.soundboard_allowed_extensions:
            allowed = ", ".join(bot.config.soundboard_allowed_extensions)
            await interaction.response.send_message(
                f"Unsupported file type. Allowed: {allowed}",
                ephemeral=True,
            )
            return

        max_bytes = max(1, bot.config.soundboard_max_file_size_mb) * 1024 * 1024
        if file.size > max_bytes:
            await interaction.response.send_message(
                f"File is too large. Max size is {bot.config.soundboard_max_file_size_mb} MB.",
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
        except Exception as err:
            logger.exception("Failed to add sound in guild %s: %s", guild.id, err)
            with contextlib.suppress(Exception):
                saved_path.unlink(missing_ok=True)
            await interaction.followup.send(f"Failed to save sound: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Saved sound **{clean_name}** in category **{derive_category(clean_name)}** at {volume}% volume.",
            ephemeral=True,
        )

    @sound_group.command(name="play", description="Play a saved sound in your voice channel.")
    @app_commands.describe(category="Sound category", name="Sound name")
    @app_commands.autocomplete(category=category_autocomplete, name=sound_name_autocomplete)
    async def sound_play_command(
        interaction: discord.Interaction,
        category: Optional[str] = None,
        name: Optional[str] = None,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        selected_category = normalize_category(category) if category else None

        if selected_category is None and not name:
            categories = bot.sound_store.list_categories(guild.id)
            if not categories:
                await interaction.response.send_message("No sounds saved for this server yet.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Sound Categories ({len(categories)})",
                description="\n".join(_build_category_summary_lines(categories)),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Use /sound play category:<category> name:<sound>")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if selected_category is not None and not name:
            sounds = bot.sound_store.list_sounds_by_category(guild.id, selected_category)
            if not sounds:
                await interaction.response.send_message(
                    f"No sounds found in category **{selected_category}**.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"Category: {selected_category} ({len(sounds)} sounds)",
                description="\n".join(_build_sound_lines(sounds)),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Add name to play one: /sound play category:<category> name:<sound>")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not ffmpeg_available():
            await interaction.response.send_message(
                "FFmpeg is not installed or not in PATH. Install FFmpeg to use sound playback.",
                ephemeral=True,
            )
            return

        sound: Optional[SoundRecord]
        if selected_category is not None and name is not None:
            sound = bot.sound_store.get_sound_in_category(guild.id, selected_category, name)
        else:
            sound = bot.sound_store.get_sound(guild.id, name or "")

        if sound is None:
            if selected_category is not None and name:
                await interaction.response.send_message(
                    f"No sound named **{normalize_sound_name(name)}** found in category **{selected_category}**.",
                    ephemeral=True,
                )
                return
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
            await interaction.response.send_message("Join a voice or stage channel first.", ephemeral=True)
            return

        target_channel = member.voice.channel
        if not isinstance(target_channel, discord.VoiceChannel | discord.StageChannel):
            await interaction.response.send_message("Join a voice or stage channel first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ok, msg = await bot.playback_manager.play_sound_in_channel(
            guild=guild,
            target_channel=target_channel,
            sound=sound,
        )
        await interaction.followup.send(msg if ok else f"Error: {msg}", ephemeral=True)

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
            await interaction.response.send_message("Provide at least one field to edit.", ephemeral=True)
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
            current = bot.sound_store.get_sound(guild.id, name)
            if current is None:
                await interaction.response.send_message("Sound not found.", ephemeral=True)
                return

            updated = bot.sound_store.update_sound(
                guild.id,
                name,
                new_name=normalized_new_name,
                new_volume=volume,
            )
        except ValueError:
            await interaction.response.send_message("A sound with that new name already exists.", ephemeral=True)
            return
        except Exception as err:
            logger.exception("Failed to edit sound in guild %s: %s", guild.id, err)
            await interaction.response.send_message(f"Failed to edit sound: {err}", ephemeral=True)
            return

        category_change = ""
        if current.category_key != updated.category_key:
            category_change = f" Category: **{current.category}** -> **{updated.category}**."

        await interaction.response.send_message(
            f"Updated **{updated.name}** (volume: {updated.volume}%).{category_change}",
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
        except Exception as err:
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

        await interaction.response.send_message(f"Deleted sound **{removed.name}**.", ephemeral=True)

    @sound_group.command(name="list", description="List available sounds in this server.")
    @app_commands.describe(category="Optional category to list")
    @app_commands.autocomplete(category=category_autocomplete)
    async def sound_list_command(interaction: discord.Interaction, category: Optional[str] = None):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        if category:
            selected_category = normalize_category(category)
            sounds = bot.sound_store.list_sounds_by_category(guild.id, selected_category)
            if not sounds:
                await interaction.response.send_message(
                    f"No sounds found in category **{selected_category}**.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"Category: {selected_category} ({len(sounds)} sounds)",
                description="\n".join(_build_sound_lines(sounds)),
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        categories = bot.sound_store.list_categories(guild.id)
        if not categories:
            await interaction.response.send_message("No sounds saved for this server yet.", ephemeral=True)
            return

        total = sum(count for _, count in categories)
        embed = discord.Embed(
            title=f"Sound Categories ({len(categories)} categories, {total} sounds)",
            description="\n".join(_build_category_summary_lines(categories)),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Use /sound list category:<category> to browse sounds")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @sound_group.command(name="categories", description="Show all detected categories in this server.")
    async def sound_categories_command(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
            return

        categories = bot.sound_store.list_categories(guild.id)
        if not categories:
            await interaction.response.send_message("No sounds saved for this server yet.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Detected Categories ({len(categories)})",
            description="\n".join(_build_category_summary_lines(categories)),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    tree.add_command(sound_group)
