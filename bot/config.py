from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    default_guild_id: int
    default_channel_id: int
    self_deaf: bool
    self_mute: bool
    sync_guild_ids: list[int]

    watchdog_interval_seconds: int
    connect_retry_limit: int
    min_reconnect_interval_seconds: int
    voice_recovery_grace_seconds: int
    command_sync_timeout_seconds: int
    purge_global_commands_on_guild_sync: bool

    soundboard_disconnect_after_play: bool
    soundboard_max_file_size_mb: int
    soundboard_allowed_extensions: tuple[str, ...]
    soundboard_storage_dir: str
    soundboard_db_path: str


def _to_bool(value: str, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_sync_guild_ids(value: str) -> list[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip().isdigit()]


def _parse_extensions(value: str) -> tuple[str, ...]:
    output: list[str] = []
    for raw in value.split(","):
        cleaned = raw.strip().lower()
        if not cleaned:
            continue
        if not cleaned.startswith("."):
            cleaned = f".{cleaned}"
        output.append(cleaned)
    return tuple(output)


def load_config() -> BotConfig:
    bot_token = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
    if not bot_token:
        raise RuntimeError("Missing BOT_TOKEN (or TOKEN) in environment.")

    return BotConfig(
        bot_token=bot_token,
        default_guild_id=int(os.getenv("DEFAULT_GUILD_ID", "0")),
        default_channel_id=int(os.getenv("DEFAULT_CHANNEL_ID", "0")),
        self_deaf=_to_bool(os.getenv("SELF_DEAF", "true"), default=True),
        self_mute=_to_bool(os.getenv("SELF_MUTE", "true"), default=True),
        sync_guild_ids=_parse_sync_guild_ids(os.getenv("SYNC_GUILD_IDS", "")),
        watchdog_interval_seconds=int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "20")),
        connect_retry_limit=int(os.getenv("CONNECT_RETRY_LIMIT", "4")),
        min_reconnect_interval_seconds=int(os.getenv("MIN_RECONNECT_INTERVAL_SECONDS", "12")),
        voice_recovery_grace_seconds=int(os.getenv("VOICE_RECOVERY_GRACE_SECONDS", "35")),
        command_sync_timeout_seconds=int(os.getenv("COMMAND_SYNC_TIMEOUT_SECONDS", "30")),
        purge_global_commands_on_guild_sync=_to_bool(
            os.getenv("PURGE_GLOBAL_COMMANDS_ON_GUILD_SYNC", "true"),
            default=True,
        ),
        soundboard_disconnect_after_play=_to_bool(
            os.getenv("SOUNDBOARD_DISCONNECT_AFTER_PLAY", "true"),
            default=True,
        ),
        soundboard_max_file_size_mb=int(os.getenv("SOUNDBOARD_MAX_FILE_SIZE_MB", "10")),
        soundboard_allowed_extensions=_parse_extensions(
            os.getenv("SOUNDBOARD_ALLOWED_EXTENSIONS", ".mp3,.wav,.ogg,.m4a")
        ),
        soundboard_storage_dir=os.getenv("SOUNDBOARD_STORAGE_DIR", "data/sounds"),
        soundboard_db_path=os.getenv("SOUNDBOARD_DB_PATH", "soundboard.sqlite3"),
    )
