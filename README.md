# SoundboardKeeper

SoundboardKeeper is a Discord bot that prioritizes stable voice keep-alive behavior and adds a per-guild soundboard on top.

It can stay connected to a configured home voice channel with automatic reconnect logic, and it can also play uploaded sounds with slash commands under the sound command group.

## Features

- Keeper mode with per-guild home voice channel tracking
- Watchdog loop for reconnect and recovery
- Exponential retry backoff and reconnect storm protection
- Per-guild soundboard with SQLite metadata storage
- Sound uploads via slash command attachments
- Name autocomplete for play, edit, and delete sound actions
- FFmpeg-based playback with per-sound volume

## Requirements

- Python 3.10+
- FFmpeg available on PATH
- A Discord bot application with slash command and voice permissions

## Tech Stack

- discord.py 2.x
- python-dotenv
- PyNaCl for voice support
- SQLite for sound metadata

## Quick Start

### 1. Clone

```bash
git clone https://github.com/<your-username>/SoundboardKeeper.git
cd SoundboardKeeper
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
```

Then edit .env with your values.

### 5. Run

```bash
python3 bot_main.py
```

## Environment Variables

Required:

- BOT_TOKEN: Discord bot token

Keeper and startup:

- DEFAULT_GUILD_ID: Auto-connect guild on startup, 0 to disable
- DEFAULT_CHANNEL_ID: Auto-connect voice or stage channel on startup, 0 to disable
- SYNC_GUILD_IDS: Comma-separated guild IDs for fast slash sync
- SELF_DEAF: true or false
- SELF_MUTE: true or false

Reconnect and command sync:

- WATCHDOG_INTERVAL_SECONDS
- CONNECT_RETRY_LIMIT
- MIN_RECONNECT_INTERVAL_SECONDS
- VOICE_RECOVERY_GRACE_SECONDS
- COMMAND_SYNC_TIMEOUT_SECONDS
- PURGE_GLOBAL_COMMANDS_ON_GUILD_SYNC

Soundboard:

- SOUNDBOARD_DISCONNECT_AFTER_PLAY: Disconnect after playback when keeper mode is not active
- SOUNDBOARD_MAX_FILE_SIZE_MB: Max upload size in megabytes
- SOUNDBOARD_ALLOWED_EXTENSIONS: Comma-separated list such as .mp3,.wav,.ogg,.m4a
- SOUNDBOARD_STORAGE_DIR: Base folder for uploaded sound files
- SOUNDBOARD_DB_PATH: SQLite file path for sound metadata

## Slash Commands

Keeper commands:

- /join channel: Join or move to a voice or stage channel and track it for keep-alive
- /set_home channel: Set the home channel for watchdog reconnect behavior
- /leave: Disconnect and stop keeper tracking for the current server
- /status: Show tracked state, home channel, and current connection
- /ping: Health check

Soundboard commands:

- /sound add name file volume: Upload and register a sound for this guild
- /sound play name: Play a saved sound in your current voice or stage channel
- /sound edit name new_name volume: Update sound metadata
- /sound delete name: Remove a sound and delete its file
- /sound list: List sounds available in this guild

## Soundboard Notes

- Sound names are case-insensitive for lookup and uniqueness.
- Duplicate names are rejected within the same guild.
- Accepted upload extensions are controlled by SOUNDBOARD_ALLOWED_EXTENSIONS.
- If FFmpeg is missing, playback fails gracefully with an explicit message.
- If a sound file is missing on disk, users are prompted to re-upload it.

## Keeper Mode and Soundboard Interaction

- Keeper mode remains primary when a guild is tracked with a home channel.
- During active sound playback, watchdog reconnect checks are skipped for that guild to avoid mode conflicts.
- After playback in keeper-enabled guilds, the bot returns to the home channel if it moved for playback.
- For non-keeper guilds, post-play behavior uses SOUNDBOARD_DISCONNECT_AFTER_PLAY.

## Data Layout

- Sound metadata: SQLite table in the file defined by SOUNDBOARD_DB_PATH
- Uploaded files: SOUNDBOARD_STORAGE_DIR/<guild_id>/<generated_filename>

## Troubleshooting

- Commands not appearing: verify SYNC_GUILD_IDS and bot command permissions, then restart.
- Playback errors: ensure FFmpeg is installed and executable from the bot host.
- Voice reconnect issues: tune WATCHDOG_INTERVAL_SECONDS and MIN_RECONNECT_INTERVAL_SECONDS.

## License

This project is licensed under GNU GPL v3.0. See LICENSE for details.

## Security

- Never commit real .env secrets.
- Rotate BOT_TOKEN immediately if exposed.
