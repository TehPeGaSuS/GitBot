#!/usr/bin/env python3
"""
Gitbot - A single IRC bot combining Bitbot webhook features and Limnoria RSS features.
Supports multiple IRC networks, GitHub/Gitea/GitLab webhooks, and RSS/Atom feed announcements.
All configured via IRC commands.
"""

import asyncio
import logging
import logging.handlers
import os
import sys

from src.bot import Bot
from src.config import Config

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Bootstrap: stderr only until config tells us otherwise.
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT,
                    stream=sys.stderr)

log = logging.getLogger("gitbot")


def _setup_logging(config: Config) -> None:
    """Reconfigure the root logger from config (called after config loads)."""
    level = getattr(logging, config.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    if config.log_stdout:
        root.addHandler(logging.StreamHandler(sys.stdout))

    if config.log_file:
        if config.log_rotate_days > 0:
            root.addHandler(logging.handlers.TimedRotatingFileHandler(
                config.log_file, when="midnight", backupCount=config.log_rotate_days,
            ))
        else:
            root.addHandler(logging.FileHandler(config.log_file))

    if not root.handlers:
        # Safety net: always keep at least stderr so errors aren't swallowed.
        root.addHandler(logging.StreamHandler(sys.stderr))


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"

    if not os.path.exists(config_path):
        log.error("Config file not found: %s", config_path)
        log.info("Copy config.example.json to %s and edit it.", config_path)
        sys.exit(1)

    try:
        config = Config(config_path)
        _setup_logging(config)
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
