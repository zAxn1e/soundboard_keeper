# SoundboardKeeper

SoundboardKeeper is a voice keeper bot first, with a category-based soundboard layered on top.

It keeps a configured home voice channel alive per guild with watchdog reconnect logic, and also supports uploaded sound playback with prefix-based categories.

## Project Structure

```text
SoundboardKeeper/
  bot/
    __init__.py
    main.py
    config.py
    logging_setup.py
    client.py
    commands/
      __init__.py
      basic.py
      sound.py
    services/
      __init__.py
      voice_keeper.py
      sound_store.py
      playback_manager.py
    utils/
      __init__.py
      filenames.py
      audio.py
  data/
    sounds/
  bot_main.py
  .env.example
  requirements.txt
```

## Architecture

- bot/main.py: entrypoint, loads env, configures logging, creates and runs bot
- bot/config.py: typed environment parsing in one place
- bot/client.py: custom Discord client, shared state, sync flow, startup lifecycle
- bot/commands/basic.py: join, set_home, leave, status, ping
- bot/commands/sound.py: slash UX for soundboard commands and autocomplete
- bot/services/voice_keeper.py: reconnect policy, watchdog loop, keeper safety logic
- bot/services/sound_store.py: SQLite metadata CRUD, categories, lookups
- bot/services/playback_manager.py: guild playback locking and post-play behavior
- bot/utils/audio.py: category derivation and ffmpeg checks
- bot/utils/filenames.py: extension/path helpers and storage filename generation

## Requirements

- Python 3.10+
- FFmpeg on PATH
- Discord bot token with slash command and voice permissions

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run

Preferred:

```bash
python3 -m bot.main
```

Compatibility entrypoint (still works):

```bash
python3 bot_main.py
```

## Environment Variables

Required:

- BOT_TOKEN

Keeper and sync:

- DEFAULT_GUILD_ID
- DEFAULT_CHANNEL_ID
- SYNC_GUILD_IDS
- SELF_DEAF
- SELF_MUTE
- WATCHDOG_INTERVAL_SECONDS
- CONNECT_RETRY_LIMIT
- MIN_RECONNECT_INTERVAL_SECONDS
- VOICE_RECOVERY_GRACE_SECONDS
- COMMAND_SYNC_TIMEOUT_SECONDS
- PURGE_GLOBAL_COMMANDS_ON_GUILD_SYNC

Soundboard:

- SOUNDBOARD_DISCONNECT_AFTER_PLAY
- SOUNDBOARD_MAX_FILE_SIZE_MB
- SOUNDBOARD_ALLOWED_EXTENSIONS
- SOUNDBOARD_STORAGE_DIR (default data/sounds)
- SOUNDBOARD_DB_PATH

## Slash Commands

Keeper:

- /join channel
- /set_home channel
- /leave
- /status
- /ping

Soundboard:

- /sound add name file volume
- /sound play category name
- /sound edit name new_name volume
- /sound delete name
- /sound list category
- /sound categories

## Category Behavior

Category is auto-derived from the first prefix separator in sound name:

- meme_vineboom -> meme
- anime-wow -> anime
- game:headshot -> game
- airhorn -> uncategorized

Supported separators: underscore, hyphen, colon.

## Keeper and Playback Safety

- Keeper mode remains primary for tracked guilds.
- During active sound playback in a guild, watchdog reconnect checks skip that guild to avoid conflicts.
- After playback in keeper-enabled guilds, bot returns to the configured home channel if it moved.
- In non-keeper guilds, post-play disconnect behavior follows SOUNDBOARD_DISCONNECT_AFTER_PLAY.

## Storage

- Uploaded files: data/sounds/<guild_id>/<generated_filename>
- Metadata: SQLite database configured by SOUNDBOARD_DB_PATH

## License

GPL-3.0
