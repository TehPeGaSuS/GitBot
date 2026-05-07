#!/usr/bin/env python3
"""
Gitbot - A single IRC bot combining Bitbot webhook features and Limnoria RSS features.
Supports multiple IRC networks, GitHub/Gitea/GitLab webhooks, and RSS/Atom feed announcements.
All configured via IRC commands.
"""

import asyncio
import json
import logging
import os
import sys

from src.bot import Bot
from src.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("gitbot.log"),
    ]
)

log = logging.getLogger("gitbot")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"

    if not os.path.exists(config_path):
        log.error("Config file not found: %s", config_path)
        log.info("Copy config.example.json to %s and edit it.", config_path)
        sys.exit(1)

    try:
        config = Config(config_path)
        bot = Bot(config)
    except Exception:
        log.exception("Failed to initialise bot — check your config and installation.")
        sys.exit(1)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Shutting down.")
    except Exception:
        log.exception("Fatal error — bot is exiting.")
        sys.exit(1)


if __name__ == "__main__":
    main()
