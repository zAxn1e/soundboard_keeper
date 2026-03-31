# VoiceKeeper Stable

VoiceKeeper Stable is a lightweight Discord voice keep-alive bot.

It joins a configured voice or stage channel, monitors connection health, and reconnects automatically when Discord voice sessions drop.

## Features

- Slash commands for join, leave, status, and health checks
- Per-guild home channel tracking for reconnects
- Background watchdog for automatic voice recovery
- Exponential backoff for connection retries
- Fast guild-scoped slash command sync

## Tech Stack

- Python 3.10+
- [discord.py](https://github.com/Rapptz/discord.py)
- [python-dotenv](https://github.com/theskumar/python-dotenv)
- [PyNaCl](https://github.com/pyca/pynacl) (voice support)

## Quick Start

### 1. Clone

```bash
git clone https://github.com/<your-username>/VoiceKeeper_Stable.git
cd VoiceKeeper_Stable
```

### 2. Create and activate virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

Copy the example env file and edit values:

```bash
cp .env.example .env
```

Required:

- `BOT_TOKEN`: Your Discord bot token

Common optional settings:

- `DEFAULT_GUILD_ID`: Guild to auto-connect on startup (`0` disables)
- `DEFAULT_CHANNEL_ID`: Voice/stage channel to auto-connect on startup (`0` disables)
- `SYNC_GUILD_IDS`: Comma-separated guild IDs for instant slash command sync
- `SELF_DEAF`: `true`/`false`
- `SELF_MUTE`: `true`/`false`
- `WATCHDOG_INTERVAL_SECONDS`: Watchdog loop interval
- `CONNECT_RETRY_LIMIT`: Max retries per connect operation
- `MIN_RECONNECT_INTERVAL_SECONDS`: Minimum time between reconnect attempts
- `PURGE_GLOBAL_COMMANDS_ON_GUILD_SYNC`: Remove old global commands when syncing to guilds

### 5. Run

```bash
python3 bot_main.py
```

## Slash Commands

- `/join` - Join a target voice/stage channel and keep reconnecting
- `/set_home` - Set home channel for watchdog reconnect behavior
- `/leave` - Disconnect and stop tracking current server
- `/status` - Show tracked state, home channel, and current connection
- `/ping` - Health check latency response

## Logging and Reconnect Notes

Discord voice sessions may occasionally drop with websocket close code `1006`.
This bot and `discord.py` automatically recover in normal cases.

## Open Source License

This project is licensed under the GNU General Public License v3.0.
See the [LICENSE](LICENSE) file for full terms.

## Security

- Never commit real `.env` secrets.
- Rotate your bot token immediately if exposed.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit changes with clear messages
4. Open a pull request

By contributing, you agree that your contributions are licensed under GPL-3.0.
