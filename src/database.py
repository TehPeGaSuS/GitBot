"""
SQLite-backed persistent storage.

Schema
------
- networks        : registered IRC network configurations
- channel_settings: per-network/channel key→value JSON settings
- bot_settings    : global key→value JSON settings

Settings used by modules
------------------------
  git-webhooks   (channel) : { "owner/repo": { "events": [...], "branches": [] }, ... }
  rss-hooks      (channel) : [ "https://..." , ... ]
  rss-seen-<url> (channel) : [ "sha1:...", ... ]   # recently announced entry IDs
"""

import json
import logging
import sqlite3
import threading
import typing

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # ------------------------------------------------------------------ schema

    def _migrate(self):
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                network  TEXT NOT NULL,
                channel  TEXT NOT NULL,
                key      TEXT NOT NULL,
                value    TEXT NOT NULL,
                PRIMARY KEY (network, channel, key)
            );
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # --------------------------------------------------------- channel settings

    def get_channel(self, network: str, channel: str, key: str,
                    default=None) -> typing.Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM channel_settings "
                "WHERE network=? AND channel=? AND key=?",
                (network, channel.lower(), key)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_channel(self, network: str, channel: str, key: str,
                    value: typing.Any):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO channel_settings "
                "(network, channel, key, value) VALUES (?,?,?,?)",
                (network, channel.lower(), key, json.dumps(value))
            )
            self._conn.commit()

    def del_channel(self, network: str, channel: str, key: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM channel_settings "
                "WHERE network=? AND channel=? AND key=?",
                (network, channel.lower(), key)
            )
            self._conn.commit()

    def find_by_channel_key(self, key: str) -> typing.List[typing.Tuple]:
        """Return all (network, channel, value) rows matching *key*."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT network, channel, value FROM channel_settings "
                "WHERE key=?", (key,)
            ).fetchall()
        return [(r["network"], r["channel"], json.loads(r["value"]))
                for r in rows]

    # ------------------------------------------------------------ bot settings

    def get_bot(self, key: str, default=None) -> typing.Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM bot_settings WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_bot(self, key: str, value: typing.Any):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?,?)",
                (key, json.dumps(value))
            )
            self._conn.commit()

    def del_bot(self, key: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM bot_settings WHERE key=?", (key,)
            )
            self._conn.commit()

    def purge_network(self, network: str) -> int:
        """Delete all channel_settings rows for *network*.

        Returns the number of rows deleted.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM channel_settings WHERE network=?", (network,)
            )
            self._conn.commit()
            return cur.rowcount
