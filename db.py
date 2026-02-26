"""SQLite persistence layer for gitbot."""

import json
import sqlite3


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
            id       INTEGER PRIMARY KEY,
            network  TEXT NOT NULL,
            channel  TEXT NOT NULL,
            repo     TEXT NOT NULL,
            forge    TEXT,
            events   TEXT NOT NULL DEFAULT '["ping","code","pr","issue","repo"]',
            branches TEXT NOT NULL DEFAULT '[]'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook
            ON webhook_routes(network, channel, repo, forge);

        CREATE TABLE IF NOT EXISTS rss_feeds (
            id      INTEGER PRIMARY KEY,
            network TEXT NOT NULL,
            channel TEXT NOT NULL,
            url     TEXT NOT NULL,
            format  TEXT NOT NULL DEFAULT '$feed_name: $title <$link>'
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
    _migrate_webhook_forge(db)
    _migrate_rss_format(db)


def _migrate_webhook_forge(db):
    """Add forge column and rebuild unique index to include it."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(webhook_routes)").fetchall()]
    if "forge" in cols:
        return
    db.executescript("""
        ALTER TABLE webhook_routes ADD COLUMN forge TEXT;
        DROP INDEX IF EXISTS uq_webhook;
        CREATE UNIQUE INDEX uq_webhook
            ON webhook_routes(network, channel, repo, forge);
    """)
    db.commit()


def _migrate_rss_format(db):
    cols = [r[1] for r in db.execute("PRAGMA table_info(rss_feeds)").fetchall()]
    if "format" not in cols:
        db.execute("""
            ALTER TABLE rss_feeds
            ADD COLUMN format TEXT NOT NULL DEFAULT '$feed_name: $title <$link>'
        """)
        db.commit()


# ── Purge helpers (used by reload) ────────────────────────────────────────────

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

def webhook_add(db, network, channel, repo, forge=None,
                events=None, branches=None):
    events   = events   or ["ping", "code", "pr", "issue", "repo"]
    branches = branches or []
    db.execute("""
        INSERT INTO webhook_routes(network, channel, repo, forge, events, branches)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(network, channel, repo, forge) DO UPDATE
            SET events=excluded.events, branches=excluded.branches
    """, (network, channel, repo, forge,
          json.dumps(events), json.dumps(branches)))
    db.commit()


def webhook_remove(db, network, channel, repo, forge=None):
    db.execute("""
        DELETE FROM webhook_routes
        WHERE network=? AND channel=? AND repo=?
          AND (forge IS ? OR (forge IS NULL AND ? IS NULL))
    """, (network, channel, repo, forge, forge))
    db.commit()


def webhook_list(db, network, channel):
    rows = db.execute("""
        SELECT repo, forge, events, branches FROM webhook_routes
        WHERE network=? AND channel=?
        ORDER BY repo, forge
    """, (network, channel)).fetchall()
    return [
        {
            "repo":     r["repo"],
            "forge":    r["forge"],
            "events":   json.loads(r["events"]),
            "branches": json.loads(r["branches"]),
        }
        for r in rows
    ]


def webhook_set_events(db, network, channel, repo, events, forge=None):
    db.execute("""
        UPDATE webhook_routes SET events=?
        WHERE network=? AND channel=? AND repo=?
          AND (forge IS ? OR (forge IS NULL AND ? IS NULL))
    """, (json.dumps(events), network, channel, repo, forge, forge))
    db.commit()


def webhook_set_branches(db, network, channel, repo, branches, forge=None):
    db.execute("""
        UPDATE webhook_routes SET branches=?
        WHERE network=? AND channel=? AND repo=?
          AND (forge IS ? OR (forge IS NULL AND ? IS NULL))
    """, (json.dumps(branches), network, channel, repo, forge, forge))
    db.commit()


def webhook_targets(db, forge, full_name, repo_user, organisation):
    """
    Return routes matching this repo+forge.
    Routes with forge=NULL match any forge; specific forge routes only match
    that forge.
    """
    rows = db.execute("""
        SELECT network, channel, repo, forge, events, branches
        FROM webhook_routes
        WHERE forge IS NULL OR forge=?
    """, (forge,)).fetchall()
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
    """Insert feed. Returns (id, created) — created=False if already existed."""
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
    rows = db.execute("""
        SELECT id, network, channel, url, format FROM rss_feeds
    """).fetchall()
    return [dict(r) for r in rows]


def rss_set_format(db, network, channel, url, fmt):
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
    db.execute("""
        DELETE FROM rss_seen WHERE feed_id=? AND entry_id NOT IN (
            SELECT entry_id FROM rss_seen WHERE feed_id=?
            ORDER BY rowid DESC LIMIT 500
        )
    """, (feed_id, feed_id))
    db.commit()
