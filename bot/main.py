from __future__ import annotations

from dotenv import load_dotenv

from bot.client import KeeperSoundBot
from bot.config import load_config
from bot.logging_setup import configure_logging


def main() -> None:
    load_dotenv()
    configure_logging()
    config = load_config()
    bot = KeeperSoundBot(config)
    bot.run(config.bot_token, reconnect=True, log_handler=None)


if __name__ == "__main__":
    main()
