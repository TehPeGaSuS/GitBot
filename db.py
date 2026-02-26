"""SQLite persistence layer for gitbot."""

import json
import sqlite3
from pathlib import Path


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    _migrate(db)
    import auth
    auth.setup_schema(db)
    return db


def _migrate(db: sqlite3.Connection):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS webhook_routes (
            id          INTEGER PRIMARY KEY,
            network     TEXT NOT NULL,
            channel     TEXT NOT NULL,
            repo        TEXT NOT NULL,
            events      TEXT NOT NULL DEFAULT '["ping","code","pr","issue","repo"]',
            branches    TEXT NOT NULL DEFAULT '[]'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook
            ON webhook_routes(network, channel, repo);

        CREATE TABLE IF NOT EXISTS rss_feeds (
            id       INTEGER PRIMARY KEY,
            network  TEXT NOT NULL,
            channel  TEXT NOT NULL,
            url      TEXT NOT NULL,
            format   TEXT NOT NULL DEFAULT '$feed_name: $title <$link>'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_rss
            ON rss_feeds(network, channel, url);

        CREATE TABLE IF NOT EXISTS rss_seen (
            feed_id  INTEGER NOT NULL REFERENCES rss_feeds(id) ON DELETE CASCADE,
            entry_id TEXT NOT NULL,
            PRIMARY KEY (feed_id, entry_id)
        );
    """)
    db.commit()
    # Migrate existing databases that predate the format column
    cols = [r[1] for r in db.execute("PRAGMA table_info(rss_feeds)").fetchall()]
    if "format" not in cols:
        db.execute("""
            ALTER TABLE rss_feeds
            ADD COLUMN format TEXT NOT NULL DEFAULT '$feed_name: $title <$link>'
        """)
        db.commit()


def purge_network(db, network: str):
    """Remove all webhook routes and RSS feeds for a network."""
    db.execute("DELETE FROM webhook_routes WHERE network=?", (network,))
    db.execute("DELETE FROM rss_feeds WHERE network=?", (network,))
    db.commit()


def purge_channel(db, network: str, channel: str):
    """Remove all webhook routes and RSS feeds for a specific channel."""
    db.execute("DELETE FROM webhook_routes WHERE network=? AND channel=?",
               (network, channel))
    db.execute("DELETE FROM rss_feeds WHERE network=? AND channel=?",
               (network, channel))
    db.commit()


# ── Webhook routes ────────────────────────────────────────────────────────────

def webhook_add(db, network, channel, repo,
                events=None, branches=None):
    events   = events   or ["ping", "code", "pr", "issue", "repo"]
    branches = branches or []
    db.execute("""
        INSERT INTO webhook_routes(network, channel, repo, events, branches)
        VALUES (?,?,?,?,?)
        ON CONFLICT(network, channel, repo) DO UPDATE
            SET events=excluded.events, branches=excluded.branches
    """, (network, channel, repo,
          json.dumps(events), json.dumps(branches)))
    db.commit()


def webhook_remove(db, network, channel, repo):
    db.execute("""
        DELETE FROM webhook_routes
        WHERE network=? AND channel=? AND repo=?
    """, (network, channel, repo))
    db.commit()


def webhook_list(db, network, channel):
    rows = db.execute("""
        SELECT repo, events, branches FROM webhook_routes
        WHERE network=? AND channel=?
        ORDER BY repo
    """, (network, channel)).fetchall()
    return [
        {
            "repo":     r["repo"],
            "events":   json.loads(r["events"]),
            "branches": json.loads(r["branches"]),
        }
        for r in rows
    ]


def webhook_set_events(db, network, channel, repo, events):
    db.execute("""
        UPDATE webhook_routes SET events=?
        WHERE network=? AND channel=? AND repo=?
    """, (json.dumps(events), network, channel, repo))
    db.commit()


def webhook_set_branches(db, network, channel, repo, branches):
    db.execute("""
        UPDATE webhook_routes SET branches=?
        WHERE network=? AND channel=? AND repo=?
    """, (json.dumps(branches), network, channel, repo))
    db.commit()


def webhook_targets(db, full_name, repo_user, organisation):
    """Return all (network, channel, events, branches) matching this repo."""
    rows = db.execute("""
        SELECT network, channel, repo, events, branches FROM webhook_routes
    """).fetchall()
    results = []
    candidates = {x.lower() for x in [full_name, repo_user, organisation] if x}
    for r in rows:
        if r["repo"].lower() in candidates:
            results.append({
                "network":  r["network"],
                "channel":  r["channel"],
                "events":   json.loads(r["events"]),
                "branches": json.loads(r["branches"]),
            })
    return results


# ── RSS feeds ─────────────────────────────────────────────────────────────────

def rss_add(db, network, channel, url):
    """Insert feed. Returns (id, created) — created=False if it already existed."""
    existing = db.execute("""
        SELECT id FROM rss_feeds WHERE network=? AND channel=? AND url=?
    """, (network, channel, url)).fetchone()
    if existing:
        return existing["id"], False
    db.execute("""
        INSERT INTO rss_feeds(network, channel, url) VALUES (?,?,?)
    """, (network, channel, url))
    db.commit()
    row = db.execute("""
        SELECT id FROM rss_feeds WHERE network=? AND channel=? AND url=?
    """, (network, channel, url)).fetchone()
    return row["id"], True


def rss_remove(db, network, channel, url):
    db.execute("""
        DELETE FROM rss_feeds WHERE network=? AND channel=? AND url=?
    """, (network, channel, url))
    db.commit()


def rss_list(db, network, channel):
    rows = db.execute("""
        SELECT url, format FROM rss_feeds WHERE network=? AND channel=?
        ORDER BY url
    """, (network, channel)).fetchall()
    return [{"url": r["url"], "format": r["format"]} for r in rows]


def rss_all_feeds(db):
    """Return all feeds: list of {id, network, channel, url, format}."""
    rows = db.execute("""
        SELECT id, network, channel, url, format FROM rss_feeds
    """).fetchall()
    return [dict(r) for r in rows]


def rss_set_format(db, network, channel, url, fmt):
    """Update format template for a feed. Returns True if the feed was found."""
    db.execute("""
        UPDATE rss_feeds SET format=? WHERE network=? AND channel=? AND url=?
    """, (fmt, network, channel, url))
    db.commit()
    return db.execute("SELECT changes()").fetchone()[0] > 0


def rss_get_seen(db, feed_id):
    rows = db.execute("""
        SELECT entry_id FROM rss_seen WHERE feed_id=?
    """, (feed_id,)).fetchall()
    return {r["entry_id"] for r in rows}


def rss_mark_seen(db, feed_id, entry_ids):
    db.executemany("""
        INSERT OR IGNORE INTO rss_seen(feed_id, entry_id) VALUES (?,?)
    """, [(feed_id, eid) for eid in entry_ids])
    # Keep only the most recent 500 per feed to avoid unbounded growth
    db.execute("""
        DELETE FROM rss_seen WHERE feed_id=? AND entry_id NOT IN (
            SELECT entry_id FROM rss_seen WHERE feed_id=?
            ORDER BY rowid DESC LIMIT 500
        )
    """, (feed_id, feed_id))
    db.commit()
