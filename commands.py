"""
IRC command dispatcher.

Commands that change state require authentication.
identify / logout are PM-only.
All other commands work in channels too, but auth is checked via prefix.
"""

import logging
import auth
import db

log = logging.getLogger("commands")

PREFIX = "!"


def parse(text: str):
    if not text.startswith(PREFIX):
        return None, None
    parts = text[len(PREFIX):].split()
    if not parts:
        return None, None
    return parts[0].lower(), parts[1:]


async def handle_pm(network: str, nick: str, prefix: str, text: str,
                    database, reply, reload_fn=None):
    """
    Handle a private message to the bot.
    prefix = "nick!user@host"
    reply  = async callable(message) — sends a PM back to nick
    """
    words = text.strip().split()
    if not words:
        return

    cmd = words[0].lower()

    # identify and logout don't use the ! prefix — plain words in PM
    if cmd == "identify":
        if len(words) < 2:
            await reply("Usage: identify <password>  or  identify <nick> <password>")
            return
        if not auth.has_owner(database):
            await reply("No owner account exists. Run the bot with --setup to create one.")
            return
        # Two forms:
        #   identify <password>         — current nick must match owner nick
        #   identify <nick> <password>  — explicit nick, useful when using a different nick
        if len(words) == 2:
            owner_nick = auth.get_owner_nick(database)
            if nick.lower() != owner_nick.lower():
                await reply(f"Your current nick doesn't match the owner nick. Use: identify <nick> <password>")
                return
            password = words[1]
        else:
            password = words[2]
        if auth.verify_password(database, password):
            auth.login(prefix)
            await reply("You are now identified.")
            log.info("Successful identify from %s (%s)", nick, prefix)
        else:
            await reply("Wrong password.")
            log.warning("Failed identify attempt from %s (%s)", nick, prefix)
        return

    if cmd == "logout":
        auth.logout(prefix)
        await reply("Logged out.")
        return

    # Everything below requires auth
    if not auth.is_authenticated(database, prefix):
        await reply("You are not identified. Use: identify <password>")
        return

    if cmd == "hostmask":
        await _hostmask(words[1:], prefix, database, reply)
    elif cmd == "passwd":
        await _passwd(words[1:], database, reply)
    elif cmd in ("help", "bothelp"):
        await _pm_help(reply)
    else:
        # Also accept !-prefixed commands in PM, and bare commands without !
        bare_cmd = cmd.lstrip("!")
        bare_args = words[1:]
        if cmd.startswith("!"):
            bare_cmd, bare_args = parse(text)
            bare_cmd = bare_cmd or cmd.lstrip("!")
        await _shared(bare_cmd, bare_args, None, None, prefix, database, reply, reload_fn)


async def handle_channel(network: str, channel: str, nick: str, prefix: str,
                          text: str, database, reply, reload_fn=None):
    """
    Handle a channel message.
    reply = async callable(message) — sends to the channel
    """
    cmd, args = parse(text)
    if cmd is None:
        return

    if cmd in ("identify", "logout"):
        await reply(f"{nick}: please use a private message for that.")
        return

    if not auth.is_authenticated(database, prefix):
        # Silently ignore — don't advertise admin commands to bystanders
        return

    await _shared(cmd, args, network, channel, prefix, database, reply, reload_fn)


# ── Shared commands (work in both PM and channel) ─────────────────────────────

async def _shared(cmd, args, network, channel, prefix, database, reply, reload_fn=None):
    if cmd == "reload":
        if reload_fn is None:
            await reply("Reload not available.")
            return
        result = await reload_fn()
        await reply(result)
    elif cmd == "webhook":
        if not network or not channel:
            await reply("!webhook must be used in a channel.")
            return
        await _webhook(network, channel, args, database, reply)
    elif cmd == "rss":
        if not network or not channel:
            await reply("!rss must be used in a channel.")
            return
        await _rss(network, channel, args, database, reply)
    elif cmd in ("help", "bothelp"):
        await _channel_help(reply)


# ── hostmask (PM only) ────────────────────────────────────────────────────────

async def _hostmask(args, current_prefix, database, reply):
    if not args:
        await reply(
            "hostmask add [mask]  — add mask (omit to use your current host)  |  "
            "hostmask remove <mask>  |  "
            "hostmask list"
        )
        return

    sub = args[0].lower()

    if sub == "list":
        masks = auth.hostmask_list(database)
        if not masks:
            await reply("No hostmasks registered.")
        else:
            for mask in masks:
                await reply(f"  {mask}")

    elif sub == "add":
        mask = args[1] if len(args) >= 2 else current_prefix
        auth.hostmask_add(database, mask)
        await reply(f"Hostmask added: {mask}")

    elif sub == "remove":
        if len(args) < 2:
            await reply("Usage: hostmask remove <mask>")
            return
        auth.hostmask_remove(database, args[1])
        await reply(f"Hostmask removed: {args[1]}")

    else:
        await reply("Unknown subcommand. Use: hostmask add|remove|list")


# ── passwd (PM only) ──────────────────────────────────────────────────────────

async def _passwd(args, database, reply):
    if len(args) < 1:
        await reply("Usage: passwd <newpassword>")
        return
    auth.change_password(database, args[0])
    await reply("Password updated.")


# ── !webhook ──────────────────────────────────────────────────────────────────

WEBHOOK_HELP = (
    "!webhook list  |  "
    "!webhook add <repo>  |  "
    "!webhook remove <repo>  |  "
    "!webhook events <repo> [event …]  |  "
    "!webhook branches <repo> [branch …]"
)

async def _webhook(network, channel, args, database, reply):
    if not args:
        await reply(WEBHOOK_HELP)
        return

    sub = args[0].lower()

    if sub == "list":
        hooks = db.webhook_list(database, network, channel)
        if not hooks:
            await reply("No webhooks registered for this channel.")
        else:
            for h in hooks:
                branches = ", ".join(h["branches"]) or "all"
                events   = ", ".join(h["events"])
                await reply(f"  {h['repo']}  events={events}  branches={branches}")

    elif sub == "add":
        if len(args) < 2:
            await reply("Usage: !webhook add <repo>")
            return
        db.webhook_add(database, network, channel, args[1])
        await reply(f"Webhook added for {args[1]}")

    elif sub == "remove":
        if len(args) < 2:
            await reply("Usage: !webhook remove <repo>")
            return
        db.webhook_remove(database, network, channel, args[1])
        await reply(f"Webhook removed for {args[1]}")

    elif sub == "events":
        if len(args) < 2:
            await reply("Usage: !webhook events <repo> [event …]")
            return
        repo = args[1]
        if len(args) == 2:
            hooks = db.webhook_list(database, network, channel)
            hook = next((h for h in hooks if h["repo"].lower() == repo.lower()), None)
            if not hook:
                await reply(f"No webhook found for {repo}")
            else:
                await reply(f"{repo} events: {', '.join(hook['events'])}")
        else:
            events = [e.lower() for e in args[2:]]
            db.webhook_set_events(database, network, channel, repo, events)
            await reply(f"Updated events for {repo}: {', '.join(events)}")

    elif sub == "branches":
        if len(args) < 2:
            await reply("Usage: !webhook branches <repo> [branch …]")
            return
        repo = args[1]
        if len(args) == 2:
            hooks = db.webhook_list(database, network, channel)
            hook = next((h for h in hooks if h["repo"].lower() == repo.lower()), None)
            if not hook:
                await reply(f"No webhook found for {repo}")
            else:
                await reply(f"{repo} branches: {', '.join(hook['branches']) or 'all'}")
        else:
            db.webhook_set_branches(database, network, channel, repo, args[2:])
            await reply(f"Updated branches for {repo}: {', '.join(args[2:])}")

    else:
        await reply(WEBHOOK_HELP)


# ── !rss ──────────────────────────────────────────────────────────────────────

RSS_HELP = (
    "!rss list  |  "
    "!rss add <url>  |  "
    "!rss remove <url>  |  "
    "!rss format <url> [template]"
)

async def _rss(network, channel, args, database, reply):
    if not args:
        await reply(RSS_HELP)
        return

    sub = args[0].lower()

    if sub == "list":
        feeds = db.rss_list(database, network, channel)
        if not feeds:
            await reply("No RSS feeds registered for this channel.")
        else:
            for f in feeds:
                await reply(f"  {f['url']}  format={f['format']}")

    elif sub == "add":
        if len(args) < 2:
            await reply("Usage: !rss add <url>")
            return
        _, created = db.rss_add(database, network, channel, args[1])
        if created:
            await reply(f"RSS feed added: {args[1]}")
        else:
            await reply(f"Already watching: {args[1]}")

    elif sub == "remove":
        if len(args) < 2:
            await reply("Usage: !rss remove <url>")
            return
        db.rss_remove(database, network, channel, args[1])
        await reply(f"RSS feed removed: {args[1]}")

    elif sub == "format":
        if len(args) < 2:
            await reply("Usage: !rss format <url> [template]")
            return
        url = args[1]
        if len(args) == 2:
            # Show current format
            feeds = db.rss_list(database, network, channel)
            feed  = next((f for f in feeds if f["url"] == url), None)
            if not feed:
                await reply(f"No feed found for {url}")
            else:
                await reply(f"Format for {url}: {feed['format']}")
        else:
            template = " ".join(args[2:])
            found = db.rss_set_format(database, network, channel, url, template)
            if found:
                await reply(
                    "Format updated. Entry vars: $title $link $description "
                    "$author $published $id  —  "
                    "Feed vars: $feed_name $feed_title $feed_link $feed_author $feed_subtitle  —  "
                    "(any feedparser field works)"
                )
            else:
                await reply(f"No feed found for {url}")

    else:
        await reply(RSS_HELP)


# ── help ──────────────────────────────────────────────────────────────────────

async def _pm_help(reply):
    lines = [
        "── PM commands ──────────────────────────────",
        "  identify <password>          log in (current nick must match owner nick)",
        "  identify <nick> <password>   log in from a different nick",
        "  logout                  end session",
        "  passwd <new>            change password",
        "  hostmask list           show auto-login masks",
        "  hostmask add [mask]     add mask (omit = your current host)",
        "  hostmask remove <mask>  remove a mask",
        "── Channel commands ─────────────────────────",
        "  !webhook list/add/remove/events/branches",
        "  !rss list/add/remove",
    ]
    for line in lines:
        await reply(line)


async def _channel_help(reply):
    await reply(
        "!webhook list/add/remove/events/branches  |  "
        "!rss list/add/remove/format  |  "
        "!reload  |  "
        "PM the bot: identify, logout, passwd, hostmask"
    )
