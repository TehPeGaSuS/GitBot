"""
modules/rss.py
==============

RSS/Atom feed announcer + on-demand reader.

Features (modelled after Limnoria's RSS plugin + bitbot's rss module)
----------------------------------------------------------------------
  • Named global feeds  (!rss add <name> <url>)
  • Per-channel announce lists  (!rss announce add/remove/list)
  • On-demand read  (!rss read [<name|url>] [<n>])
  • Feed info  (!rss info <name|url>)
  • Configurable format template per channel
  • Entry deduplication via ID hashing (survives restarts)
  • Etag / Last-Modified HTTP caching
  • Max 3 new entries announced per poll cycle (prevents flood on first add)
  • Poll interval configurable in config.json (default 300 s)

IRC commands (all require admin unless noted)
---------------------------------------------
  !rss add <name> <url>             -- register a named global feed
  !rss remove <name>                -- remove a named global feed
  !rss list                         -- list all named feeds (anyone)
  !rss announce add <name|url> ...  -- start announcing in this channel
  !rss announce remove <name|url> . -- stop announcing
  !rss announce list                -- list feeds announced here (anyone)
  !rss read [<name|url>] [<n>]      -- read latest n entries (anyone)
  !rss info <name|url>              -- show feed metadata (anyone)
  !rss format [<template>]          -- show/set channel format template
  !rss interval [<seconds>]         -- show/set poll interval (admin)

Format template variables
-------------------------
  $title   $link   $author   $date   $description
  $feed_title  $feed_name  (plus any raw feedparser entry fields)

Default template:  [$feed_name] $title — $link
"""

import asyncio
import hashlib
import logging
import string
import time
import typing
from datetime import datetime, timezone

import aiohttp
import feedparser
from src import formatting as fmt

log = logging.getLogger(__name__)

DEFAULT_FORMAT   = "[$feed_name] $title — $link"
MAX_NEW_PER_POLL = 3
MAX_SEEN_IDS     = 500      # cap stored seen-IDs per channel/feed

# ------------------------------------------------------------------- helpers

def _entry_id(entry) -> str:
    raw = entry.get("id") or entry.get("link") or entry.get("title") or ""
    return "sha1:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text).strip()


def _format_date(t) -> str:
    if not t:
        return ""
    try:
        dt = datetime(*t[:6], tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(t)


def _format_entry(feed_name: str, feed_data: dict, entry: dict,
                  template: str) -> str:
    feed_title = feed_data.get("title", feed_name)
    title = _strip_html(entry.get("title", "(no title)"))
    link  = entry.get("link", "")
    author = entry.get("author", "")
    date   = _format_date(entry.get("published_parsed") or
                          entry.get("updated_parsed"))
    description = _strip_html(
        entry.get("summary", "") or entry.get("description", ""))

    # Build kwargs from entry + feed metadata
    kwargs = {k: v for k, v in entry.items() if isinstance(v, str)}
    kwargs.update({
        "feed_name":  feed_name,
        "feed_title": feed_title,
        "title":      title,
        "link":       link,
        "author":     author,
        "date":       date,
        "description": description,
    })
    # feedparser sometimes gives list values; flatten them
    for k, v in list(kwargs.items()):
        if isinstance(v, list):
            kwargs[k] = ", ".join(
                str(i.get("value", i) if isinstance(i, dict) else i) for i in v)

    try:
        return string.Template(template).safe_substitute(**kwargs)
    except Exception:
        return "%s: %s — %s" % (feed_name, title, link)


# ============================================================= poller class

class RSSPoller:
    def __init__(self, bot):
        self.bot = bot

    async def run(self):
        while True:
            interval = self.bot.config.rss_interval
            await asyncio.sleep(interval)
            try:
                await self._poll()
            except Exception as e:
                log.exception("RSS poll error: %s", e)

    async def _poll(self):
        # Collect (url → [(net_name, channel_name)]) from announce settings
        url_map: typing.Dict[str, typing.List[typing.Tuple[str, str]]] = {}

        rows = self.bot.db.find_by_channel_key("rss-announce")
        for net_name, channel_name, names in rows:
            for name_or_url in names:
                url = self._resolve_url(name_or_url)
                if url:
                    url_map.setdefault(url, []).append((net_name, channel_name))

        if not url_map:
            return

        log.debug("RSS polling %d feeds", len(url_map))
        async with aiohttp.ClientSession() as session:
            tasks = {url: asyncio.ensure_future(
                        self._fetch(session, url, net_name, channel_name))
                     for url, targets in url_map.items()
                     for net_name, channel_name in targets[:1]}  # one fetch per URL
            results = {}
            for url, task in tasks.items():
                try:
                    results[url] = await task
                except Exception as e:
                    log.warning("RSS fetch failed for %s: %s", url, e)

        for url, targets in url_map.items():
            parsed = results.get(url)
            if not parsed:
                continue
            feed_data = parsed.get("feed", {})
            entries   = parsed.get("entries", [])

            for net_name, channel_name in targets:
                await self._announce_new(
                    net_name, channel_name, url, feed_data, entries)

    async def _fetch(self, session, url, net_name, channel_name):
        headers = {}
        etag_key  = "rss-etag:%s"  % url
        mtime_key = "rss-mtime:%s" % url
        etag  = self.bot.db.get_bot(etag_key)
        mtime = self.bot.db.get_bot(mtime_key)
        if etag:
            headers["If-None-Match"] = etag
        if mtime:
            headers["If-Modified-Since"] = mtime

        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 304:
                return None
            text = await resp.text()
            if resp.headers.get("ETag"):
                self.bot.db.set_bot(etag_key, resp.headers["ETag"])
            if resp.headers.get("Last-Modified"):
                self.bot.db.set_bot(mtime_key, resp.headers["Last-Modified"])

        return feedparser.parse(text)

    async def _announce_new(self, net_name, channel_name, url,
                            feed_data, entries):
        seen_key = "rss-seen:%s:%s:%s" % (net_name, channel_name,
                                           hashlib.sha1(url.encode()).hexdigest()[:12])
        seen_ids = self.bot.db.get_channel(net_name, channel_name, seen_key, [])

        template = self.bot.db.get_channel(
            net_name, channel_name, "rss-format", DEFAULT_FORMAT)

        net = self.bot.networks.get(net_name)

        # Resolve name for this url
        feed_name = self._name_for_url(url) or url

        announced = 0
        new_ids = []
        for entry in reversed(entries):
            eid = _entry_id(entry)
            if eid in seen_ids:
                continue
            if announced >= MAX_NEW_PER_POLL:
                new_ids.append(eid)
                continue
            announced += 1
            new_ids.append(eid)
            text = _format_entry(feed_name, feed_data, entry, template)
            text = _apply_prefix(text, self.bot, net_name, channel_name, net)
            self.bot.send(net_name, channel_name, text)

        # Merge and trim seen list
        all_seen = seen_ids + new_ids
        if len(all_seen) > MAX_SEEN_IDS:
            all_seen = all_seen[-MAX_SEEN_IDS:]
        self.bot.db.set_channel(net_name, channel_name, seen_key, all_seen)

    # ---------------------------------------------------------------- helpers

    def _resolve_url(self, name_or_url: str) -> typing.Optional[str]:
        """Return URL for a feed name, or the value itself if it's already a URL."""
        if name_or_url.startswith(("http://", "https://")):
            return name_or_url
        feeds = self.bot.db.get_bot("rss-feeds", {})
        return feeds.get(name_or_url)

    def _name_for_url(self, url: str) -> typing.Optional[str]:
        feeds = self.bot.db.get_bot("rss-feeds", {})
        for name, u in feeds.items():
            if u == url:
                return name
        return None


# ============================================================= IRC command

# Subcommands that need a target #channel when used via PM.
# "list", "info", "interval", "add", "remove" are global (no channel context needed).
_CHANNEL_SUBCOMMANDS = {"announce", "read", "format"}

async def handle_command(ctx):
    """Handle !rss commands.

    In a channel:  !rss announce add feedname
    Via PM:        rss announce add #channel feedname
                   rss read #channel feedname 3
                   rss format #channel $title — $link
    Global (no channel needed, same in both modes):
                   rss add feedname https://...
                   rss remove feedname
                   rss list
                   rss info feedname
                   rss interval 120
    """
    if not ctx.args:
        if ctx.is_pm:
            ctx.reply(
                "Usage: rss <subcommand> [#channel] [args]  "
                "— subcommands: add remove list announce read info format interval"
            )
        else:
            ctx.reply("Usage: !rss add|remove|list|announce|read|info|format|interval")
        return

    sub = ctx.args[0].lower()
    rest = ctx.args[1:]

    # For channel-scoped subcommands in a PM, pop #channel from args first.
    if ctx.is_pm and sub in _CHANNEL_SUBCOMMANDS:
        channel, rest = ctx.pm_channel(rest)
        if channel is None:
            return
        ctx._pm_channel = channel

    if sub == "list":
        await _cmd_list(ctx)
    elif sub == "add":
        await _cmd_add(ctx, rest)
    elif sub == "remove":
        await _cmd_remove(ctx, rest)
    elif sub == "announce":
        await _cmd_announce(ctx, rest)
    elif sub == "read":
        await _cmd_read(ctx, rest)
    elif sub == "info":
        await _cmd_info(ctx, rest)
    elif sub == "format":
        await _cmd_format(ctx, rest)
    elif sub == "interval":
        await _cmd_interval(ctx, rest)
    elif sub == "hideprefix":
        await _cmd_hideprefix(ctx, rest)
    elif _is_feed_name(ctx.bot, sub):
        # Limnoria-style shortcut: !rss feedname  →  show latest entry.
        # In a PM this also works but needs no #channel since it just prints.
        # Optional count arg: !rss feedname 3
        n = 1
        if rest:
            try:
                n = int(rest[0])
            except ValueError:
                pass
        await _cmd_read(ctx, [sub, str(n)])
    else:
        ctx.error(
            "Unknown subcommand '%s'. "
            "Use: add remove list announce read info format interval hideprefix  "
            "— or just '!rss <feedname>' to show the latest entry." % sub
        )


# ---------------------------------------------------------------- sub-commands

def _is_feed_name(bot, name: str) -> bool:
    """Return True if *name* is a registered feed name (case-insensitive)."""
    feeds = bot.db.get_bot("rss-feeds", {})
    return name.lower() in {k.lower() for k in feeds}


async def _cmd_list(ctx):
    feeds = ctx.bot.db.get_bot("rss-feeds", {})
    if not feeds:
        ctx.reply("No named feeds registered.")
    else:
        ctx.reply("Named feeds: " + ", ".join(
            "%s (%s)" % (n, u) for n, u in sorted(feeds.items())))


async def _cmd_add(ctx, rest):
    if not ctx.is_admin:
        ctx.error("Admins only.")
        return
    if len(rest) < 2:
        ctx.error("Usage: !rss add <name> <url>")
        return
    name, url = rest[0], rest[1]
    if not url.startswith(("http://", "https://")):
        ctx.error("URL must start with http:// or https://")
        return
    feeds = ctx.bot.db.get_bot("rss-feeds", {})
    if name in feeds:
        ctx.error("A feed named '%s' already exists." % name)
        return
    # Quick validation
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                text = await r.text()
        parsed = feedparser.parse(text)
        if not parsed.get("feed"):
            ctx.error("Could not parse that URL as an RSS/Atom feed.")
            return
        feed_title = parsed["feed"].get("title", "(unknown)")
    except Exception as e:
        ctx.error("Failed to fetch feed: %s" % e)
        return
    feeds[name] = url
    ctx.bot.db.set_bot("rss-feeds", feeds)
    ctx.reply("Added feed '%s' → %s  (title: %s)" % (name, url, feed_title))


async def _cmd_remove(ctx, rest):
    if not ctx.is_admin:
        ctx.error("Admins only.")
        return
    if not rest:
        ctx.error("Usage: !rss remove <name>")
        return
    name = rest[0]
    feeds = ctx.bot.db.get_bot("rss-feeds", {})
    if name not in feeds:
        ctx.error("No feed named '%s'." % name)
        return
    del feeds[name]
    ctx.bot.db.set_bot("rss-feeds", feeds)
    ctx.reply("Removed feed '%s'." % name)


async def _cmd_announce(ctx, rest):
    if not rest:
        ctx.error("Usage: !rss announce add|remove|list [<name|url> ...]")
        return
    sub = rest[0].lower()
    names = rest[1:]

    announced = ctx.bot.db.get_channel(
        ctx.net_name, ctx.channel, "rss-announce", [])

    if sub == "list":
        if not announced:
            ctx.reply("No feeds announced in this channel.")
        else:
            ctx.reply("Announced feeds: " + ", ".join(announced))
        return

    if not ctx.is_admin:
        ctx.error("Admins only.")
        return

    if sub == "add":
        if not names:
            ctx.error("Specify at least one feed name or URL.")
            return
        added = []
        for n in names:
            if n in announced:
                ctx.reply("'%s' is already announced in this channel." % n)
                continue
            # Must be a known name or a valid URL
            url = _resolve_url_static(ctx.bot, n)
            if not url:
                ctx.error("'%s' is not a known feed name or URL." % n)
                continue
            announced.append(n)
            added.append(n)
            # Pre-mark existing entries as seen so we don't flood on first announce
            asyncio.ensure_future(_preseed_seen(ctx, n, url))
        if added:
            ctx.bot.db.set_channel(ctx.net_name, ctx.channel,
                                   "rss-announce", announced)
            ctx.reply("Now announcing: %s" % ", ".join(added))

    elif sub == "remove":
        if not names:
            ctx.error("Specify at least one feed name or URL.")
            return
        removed = []
        for n in names:
            if n in announced:
                announced.remove(n)
                removed.append(n)
            else:
                ctx.reply("'%s' is not announced here." % n)
        if removed:
            ctx.bot.db.set_channel(ctx.net_name, ctx.channel,
                                   "rss-announce", announced)
            ctx.reply("Removed: %s" % ", ".join(removed))

    else:
        ctx.error("Usage: !rss announce add|remove|list")


async def _preseed_seen(ctx, name_or_url, url):
    """Fetch feed and mark all current entries as seen (no announcement)."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
        parsed = feedparser.parse(text)
        entries = parsed.get("entries", [])
        seen_key = "rss-seen:%s:%s:%s" % (
            ctx.net_name, ctx.channel,
            hashlib.sha1(url.encode()).hexdigest()[:12])
        ids = [_entry_id(e) for e in entries]
        ctx.bot.db.set_channel(ctx.net_name, ctx.channel, seen_key, ids)
        log.debug("Pre-seeded %d entries for %s in %s/%s",
                  len(ids), name_or_url, ctx.net_name, ctx.channel)
    except Exception as e:
        log.warning("Pre-seed failed for %s: %s", url, e)


def _apply_prefix(text: str, bot, net_name: str, channel: str,
                  network=None) -> str:
    """Apply [RSS] prefix (green, or plain on +c) unless rss-hide-prefix is set."""
    hide = bot.db.get_channel(net_name, channel, "rss-hide-prefix", False)
    if hide:
        return text
    monochrome = network.has_channel_mode(channel, "c") if network else False
    label = "RSS" if monochrome else fmt.color("RSS", fmt.GREEN)
    text = "[%s] %s" % (label, text)
    if monochrome:
        text = fmt.strip_formatting(text)
    return text


async def _cmd_read(ctx, rest):
    """Read and display entries from a feed on demand."""
    name_or_url = rest[0] if rest else None
    n = 3
    if rest and len(rest) >= 2:
        try:
            n = int(rest[-1])
            name_or_url = rest[0] if len(rest) > 1 else None
        except ValueError:
            pass
    n = max(1, min(n, 10))

    # If no arg, use first announced feed for this channel
    if not name_or_url:
        announced = ctx.bot.db.get_channel(
            ctx.net_name, ctx.channel, "rss-announce", [])
        if not announced:
            ctx.error("No feed specified and no feeds announced in this channel.")
            return
        name_or_url = announced[0]

    url = _resolve_url_static(ctx.bot, name_or_url)
    if not url:
        ctx.error("'%s' is not a known feed name or URL." % name_or_url)
        return

    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
        parsed = feedparser.parse(text)
    except Exception as e:
        ctx.error("Failed to fetch feed: %s" % e)
        return

    entries = parsed.get("entries", [])
    if not entries:
        ctx.reply("No entries found in that feed.")
        return

    feed_data  = parsed.get("feed", {})
    feed_name  = _name_for_url_static(ctx.bot, url) or name_or_url
    template   = ctx.bot.db.get_channel(
        ctx.net_name, ctx.channel, "rss-format", DEFAULT_FORMAT)

    net = ctx.bot.networks.get(ctx.net_name)
    for entry in entries[:n]:
        text = _format_entry(feed_name, feed_data, entry, template)
        ctx.reply(_apply_prefix(text, ctx.bot, ctx.net_name, ctx.channel, net))


async def _cmd_info(ctx, rest):
    if not rest:
        ctx.error("Usage: !rss info <name|url>")
        return
    name_or_url = rest[0]
    url = _resolve_url_static(ctx.bot, name_or_url)
    if not url:
        ctx.error("'%s' is not a known feed name or URL." % name_or_url)
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
        parsed = feedparser.parse(text)
    except Exception as e:
        ctx.error("Failed to fetch: %s" % e)
        return
    f = parsed.get("feed", {})
    ctx.reply("Feed: %s | URL: %s | Entries: %d | Updated: %s"
              % (f.get("title", "?"), url, len(parsed.get("entries", [])),
                 _format_date(f.get("updated_parsed"))))


async def _cmd_format(ctx, rest):
    if not rest:
        tmpl = ctx.bot.db.get_channel(
            ctx.net_name, ctx.channel, "rss-format", DEFAULT_FORMAT)
        ctx.reply("Current format: %s" % tmpl)
    else:
        if not ctx.is_admin:
            ctx.error("Admins only.")
            return
        tmpl = " ".join(rest)
        ctx.bot.db.set_channel(ctx.net_name, ctx.channel, "rss-format", tmpl)
        ctx.reply("Format updated.")


async def _cmd_interval(ctx, rest):
    if not ctx.is_admin:
        ctx.error("Admins only.")
        return
    if not rest:
        ctx.reply("Current RSS poll interval: %d seconds." %
                  ctx.bot.config.rss_interval)
    else:
        try:
            secs = int(rest[0])
            if secs < 30:
                ctx.error("Minimum interval is 30 seconds.")
                return
        except ValueError:
            ctx.error("Interval must be a number of seconds.")
            return
        ctx.bot.config.rss_interval = secs
        ctx.reply("RSS poll interval set to %d seconds." % secs)


async def _cmd_hideprefix(ctx, rest):
    """Show or toggle the [RSS] prefix for this channel.

    !rss hideprefix          -- show current value
    !rss hideprefix on       -- hide the [RSS] prefix
    !rss hideprefix off      -- show the [RSS] prefix (default)
    """
    if not rest:
        current = ctx.bot.db.get_channel(
            ctx.net_name, ctx.channel, "rss-hide-prefix", False)
        ctx.reply("rss-hide-prefix is currently %s." % ("on" if current else "off"))
    else:
        if not ctx.is_admin:
            ctx.error("Admins only.")
            return
        val = rest[0].lower() in ("true", "1", "yes", "on")
        ctx.bot.db.set_channel(ctx.net_name, ctx.channel, "rss-hide-prefix", val)
        ctx.reply("rss-hide-prefix set to %s." % ("on" if val else "off"))


# ---------------------------------------------------------------- static helpers

def _resolve_url_static(bot, name_or_url: str) -> typing.Optional[str]:
    if name_or_url.startswith(("http://", "https://")):
        return name_or_url
    feeds = bot.db.get_bot("rss-feeds", {})
    return feeds.get(name_or_url)


def _name_for_url_static(bot, url: str) -> typing.Optional[str]:
    feeds = bot.db.get_bot("rss-feeds", {})
    for name, u in feeds.items():
        if u == url:
            return name
    return None
