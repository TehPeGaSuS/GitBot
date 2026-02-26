"""RSS/Atom feed poller."""

import asyncio
import hashlib
import html
import logging
import re
from string import Template
from typing import Callable

import aiohttp
import feedparser

import db

log = logging.getLogger("rss")

DEFAULT_INTERVAL      = 300   # 5 minutes
DEFAULT_FORMAT        = "$feed_name: $title <$link>"

# Template variables available in format strings:
#
#   From the feed entry (feedparser field names):
#     $title          entry title, HTML stripped
#     $link           entry URL
#     $description    entry summary/description, HTML stripped
#     $author         entry author
#     $published      publication date string (as-is from feedparser)
#     $id             entry unique ID
#     ... any other feedparser entry field
#
#   From the feed itself (prefixed with feed_):
#     $feed_name      feed title  (e.g. "pegasus/testrepo")
#     $feed_link      feed URL
#     $feed_author    feed author
#     $feed_subtitle  feed subtitle/description
#     ... any other feedparser feed.* field, prefixed with feed_


_TAG_RE = re.compile(r"<[^>]+>")

def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities, collapse whitespace."""
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _flatten_value(value) -> str:
    """
    feedparser returns some fields as lists of dicts with type/value/href.
    Resolve them to a plain string the same way Limnoria does:
    prefer text/plain, then strip HTML from text/html, then fall back to href.
    """
    if not isinstance(value, list):
        if isinstance(value, str):
            return value
        return str(value) if value is not None else ""

    for item in value:
        if isinstance(item, dict) and item.get("type") == "text/plain":
            return item.get("value", "")
    for item in value:
        if isinstance(item, dict) and item.get("type") in \
                ("text/html", "application/xhtml+xml"):
            if "value" in item:
                return _strip_html(item["value"])
            if "href" in item:
                return item["href"]
    for item in value:
        if isinstance(item, dict) and "href" in item:
            return item["href"]
    for item in value:
        if isinstance(item, dict) and "value" in item:
            return str(item["value"])
    return str(value)


def _build_vars(feed_meta: dict, entry) -> dict:
    """
    Build the template substitution dict from a feedparser entry
    and feed metadata dict.
    """
    # Feed-level vars ($feed_*)
    kwargs = {}
    for k, v in feed_meta.items():
        if isinstance(v, str):
            kwargs[f"feed_{k}"] = _strip_html(v)
        elif isinstance(v, list):
            kwargs[f"feed_{k}"] = _flatten_value(v)

    # Convenience alias — strip Gitea's "Feed of X" wrapper if present
    raw_name = kwargs.get("feed_title", "")
    m = re.match(r'^[Ff]eed\s+of\s+"?(.+?)"?\s*$', raw_name)
    kwargs.setdefault("feed_name", m.group(1) if m else raw_name)

    # Entry-level vars — iterate feedparser entry attributes
    entry_dict = dict(entry) if hasattr(entry, "items") else {}
    for k, v in entry_dict.items():
        kwargs[k] = _flatten_value(v) if isinstance(v, list) \
                    else (_strip_html(v) if isinstance(v, str) else str(v) if v is not None else "")

    # Ensure the most common keys always exist (empty string if absent)
    for key in ("title", "link", "description", "author", "published", "id"):
        kwargs.setdefault(key, "")

    # HTML-strip summary/title from their _detail counterparts if available
    for key in ("title", "summary"):
        detail = entry_dict.get(f"{key}_detail")
        if isinstance(detail, dict) and detail.get("type") in \
                ("text/html", "application/xhtml+xml"):
            kwargs[key] = _strip_html(detail.get("value", kwargs.get(key, "")))

    # Map feedparser's "summary" to "description" if not already set
    if not kwargs.get("description") and kwargs.get("summary"):
        kwargs["description"] = kwargs["summary"]

    return kwargs


def _format_entry(template_str: str, feed_meta: dict, entry) -> str:
    variables = _build_vars(feed_meta, entry)
    try:
        return Template(template_str).safe_substitute(variables)
    except (KeyError, ValueError) as e:
        log.warning("Bad RSS format template %r: %s — falling back", template_str, e)
        title = variables.get("title", "")
        link  = variables.get("link", "")
        name  = variables.get("feed_name", "")
        return f"{name}: {title} <{link}>"


def _entry_id(entry) -> str:
    """Stable unique ID for a feedparser entry."""
    raw = entry.get("id") or entry.get("link") or entry.get("title") or ""
    return "sha1:" + hashlib.sha1(raw.encode()).hexdigest()


class RSSPoller:
    def __init__(self, database, deliver: Callable,
                 interval: int = DEFAULT_INTERVAL):
        """deliver: async callable(network, channel, message)"""
        self._db       = database
        self._deliver  = deliver
        self._interval = interval

    async def run(self):
        while True:
            await self._poll()
            await asyncio.sleep(self._interval)

    async def _poll(self):
        feeds = db.rss_all_feeds(self._db)
        if not feeds:
            return
        log.debug("Polling %d RSS feed(s)", len(feeds))
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            for feed in feeds:
                try:
                    await self._fetch_and_deliver(session, feed)
                except Exception as e:
                    log.exception("Unhandled error polling %s: %s", feed["url"], e)

    async def _fetch_and_deliver(self, session, feed: dict):
        url     = feed["url"]
        feed_id = feed["id"]
        network = feed["network"]
        channel = feed["channel"]
        fmt     = feed.get("format", DEFAULT_FORMAT)

        try:
            async with session.get(url) as resp:
                content = await resp.text()
        except Exception as e:
            log.warning("Failed to fetch %s: %s", url, e)
            return

        parsed    = feedparser.parse(content)
        entries   = parsed.get("entries", [])
        feed_meta = dict(parsed.get("feed", {}))

        if not entries:
            log.debug("No entries in feed %s", url)
            return

        seen        = db.rss_get_seen(self._db, feed_id)
        new_entries = [(eid, e) for e in reversed(entries)   # oldest first
                       if (eid := _entry_id(e)) not in seen]

        if not new_entries:
            log.debug("No new entries in %s", url)
            return

        # First poll: show only the single most recent entry as a "hello",
        # then mark everything seen to avoid a flood of history.
        if not seen:
            eid, entry = new_entries[-1]
            await self._deliver(network, channel,
                                _format_entry(fmt, feed_meta, entry))
            db.rss_mark_seen(self._db, feed_id, [e[0] for e in new_entries])
            log.info("Initialised feed %s (%d entries), showed latest",
                     url, len(new_entries))
            return

        # Subsequent polls: deliver up to 3 new entries.
        to_deliver = new_entries[:3]
        for eid, entry in to_deliver:
            await self._deliver(network, channel,
                                _format_entry(fmt, feed_meta, entry))
            log.debug("Delivered entry from %s: %s",
                      url, entry.get("title", ""))

        db.rss_mark_seen(self._db, feed_id, [e[0] for e in to_deliver])
        if len(new_entries) > 3:
            log.info("%s had %d new entries, delivered 3", url, len(new_entries))
