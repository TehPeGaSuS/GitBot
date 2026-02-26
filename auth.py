"""
Authentication module.

- One owner account, stored in DB with bcrypt-hashed password
- Sessions are in-memory (dict of nick!user@host -> authenticated bool)
- Hostmasks stored in DB; auto-login on PRIVMSG if mask matches
- identify only accepted via PM
"""

import fnmatch
import hashlib
import hmac
import logging
import os
import secrets

log = logging.getLogger("auth")

# In-memory sessions: maps full prefix "nick!user@host" -> True
_sessions: dict[str, bool] = {}


# ── Password hashing (simple PBKDF2, no bcrypt dep) ───────────────────────────

def _hash_password(password: str, salt: bytes = None) -> str:
    """Return 'salt_hex:hash_hex' string."""
    if salt is None:
        salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"{salt.hex()}:{key.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        key  = bytes.fromhex(key_hex)
    except Exception:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return hmac.compare_digest(candidate, key)


# ── DB helpers (called with an open sqlite3 connection) ───────────────────────

def setup_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS owner (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            nick     TEXT NOT NULL,
            password TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS owner_hostmasks (
            mask TEXT PRIMARY KEY
        );
    """)
    db.commit()


def has_owner(db) -> bool:
    row = db.execute("SELECT 1 FROM owner WHERE id=1").fetchone()
    return row is not None


def create_owner(db, nick: str, password: str):
    hashed = _hash_password(password)
    db.execute("""
        INSERT INTO owner(id, nick, password) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET nick=excluded.nick, password=excluded.password
    """, (nick, hashed))
    db.commit()
    log.info("Owner account created for %s", nick)


def verify_password(db, password: str) -> bool:
    row = db.execute("SELECT password FROM owner WHERE id=1").fetchone()
    if not row:
        return False
    return _check_password(password, row["password"])


def get_owner_nick(db) -> str | None:
    row = db.execute("SELECT nick FROM owner WHERE id=1").fetchone()
    return row["nick"] if row else None


def change_password(db, new_password: str):
    hashed = _hash_password(new_password)
    db.execute("UPDATE owner SET password=? WHERE id=1", (hashed,))
    db.commit()


# ── Hostmasks ─────────────────────────────────────────────────────────────────

def hostmask_add(db, mask: str):
    db.execute("INSERT OR IGNORE INTO owner_hostmasks(mask) VALUES (?)", (mask,))
    db.commit()


def hostmask_remove(db, mask: str):
    db.execute("DELETE FROM owner_hostmasks WHERE mask=?", (mask,))
    db.commit()


def hostmask_list(db) -> list[str]:
    rows = db.execute("SELECT mask FROM owner_hostmasks ORDER BY mask").fetchall()
    return [r["mask"] for r in rows]


def hostmask_matches(db, prefix: str) -> bool:
    """Check if nick!user@host matches any stored mask (supports * ? globs)."""
    masks = hostmask_list(db)
    for mask in masks:
        if fnmatch.fnmatchcase(prefix.lower(), mask.lower()):
            return True
    return False


# ── Sessions ──────────────────────────────────────────────────────────────────

def login(prefix: str):
    """Mark a prefix as authenticated for this session."""
    _sessions[prefix] = True
    log.info("Session opened for %s", prefix)


def logout(prefix: str):
    _sessions.pop(prefix, None)
    log.info("Session closed for %s", prefix)


def is_authenticated(db, prefix: str) -> bool:
    """
    Returns True if:
      - prefix has an active in-memory session, OR
      - prefix matches a stored hostmask (and we auto-login them)
    """
    if _sessions.get(prefix):
        return True
    if hostmask_matches(db, prefix):
        login(prefix)  # auto-login for this session
        return True
    return False
