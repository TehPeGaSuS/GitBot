"""
modules/admin.py
================

Basic bot administration commands.

  !join <#channel>              -- join a channel
  !part [#channel] [reason]     -- part a channel
  !say <#channel|nick> <text>   -- make the bot say something
  !raw <irc line>               -- send a raw IRC line (use with care)
  !networks                     -- list connected networks
  !quit [reason]                -- disconnect from all networks and exit
  !reload [--purge]             -- reload config from disk (--purge deletes
                                    DB rows for networks removed from config)
  auth <password>                -- authenticate this session as admin (PM only)
  deauth                         -- drop your authenticated admin session (PM only)

All commands work via PM too (prefix optional):
  /msg gitbot webhook list #channel
  /msg gitbot rss announce list #channel
"""

import asyncio
import logging
import sys

log = logging.getLogger(__name__)


async def handle_command(ctx):
    """Admin commands — work from both channel and PM.

    Channel:  !join #foo
    PM:       join #foo          (prefix optional in PMs)
    """
    if not ctx.is_admin:
        ctx.error("You must be an admin to use that command.")
        return

    cmd = ctx.command.lower()
    args = ctx.args

    if cmd == "join":
        if not args:
            ctx.error("Usage: join <#channel>")
            return
        ctx.network._raw("JOIN %s" % args[0])
        ctx.reply("Joining %s." % args[0])

    elif cmd == "part":
        channel = args[0] if args else ctx.target
        reason  = " ".join(args[1:]) if len(args) > 1 else "Parting"
        ctx.network._raw("PART %s :%s" % (channel, reason))

    elif cmd == "say":
        if len(args) < 2:
            ctx.error("Usage: say <target> <message>")
            return
        target = args[0]
        text   = " ".join(args[1:])
        ctx.network.send_privmsg(target, text)

    elif cmd == "raw":
        if not args:
            ctx.error("Usage: raw <irc line>")
            return
        ctx.network._raw(" ".join(args))

    elif cmd == "networks":
        names = list(ctx.bot.networks.keys())
        ctx.reply("Connected networks: " + ", ".join(names))

    elif cmd == "quit":
        reason = " ".join(args) if args else "Shutting down"
        for net in ctx.bot.networks.values():
            net._raw("QUIT :%s" % reason)
        await asyncio.sleep(1)
        sys.exit(0)

    elif cmd == "reload":
        purge = "--purge" in args
        ctx.reply("Reloading config…")
        added, removed = await ctx.bot.reload_config(purge=purge)
        parts = []
        if added:
            parts.append("added: " + ", ".join(added))
        if removed:
            if purge:
                parts.append("removed: " + ", ".join(removed) + " (DB rows purged)")
            else:
                parts.append(
                    "removed: " + ", ".join(removed)
                    + " (DB rows kept — reload --purge to delete)"
                )
        detail = " | ".join(parts) if parts else "no network changes"
        ctx.reply("Reload complete — %s." % detail)

    else:
        ctx.error("Unknown admin command. Available: join part say raw networks quit reload")


async def handle_auth(ctx):
    """Handle auth/deauth — always via PM, always replies to nick privately.

    /msg gitbot auth <password>   — authenticate for this session
    /msg gitbot deauth            — drop your session
    """
    import hmac as _hmac

    # Auth commands must come via PM — never accept them in a channel
    # to avoid leaking the password into channel logs.
    if not ctx.is_pm:
        ctx.network.send_privmsg(ctx.nick,
            "auth/deauth must be sent via private message.")
        return

    password = ctx.bot.config.auth_password
    cmd = ctx.command.lower()

    if cmd == "auth":
        if not password:
            ctx.network.send_privmsg(ctx.nick,
                "No auth password configured for this network.")
            return
        if not ctx.args:
            ctx.network.send_privmsg(ctx.nick, "Usage: auth <password>")
            return
        given = ctx.args[0]
        if _hmac.compare_digest(given, password):
            ctx.network.authed_nicks.add(ctx.nick)
            ctx.network.send_privmsg(ctx.nick,
                "You are now authenticated as admin on %s." % ctx.network.name)
        else:
            ctx.network.send_privmsg(ctx.nick, "Wrong password.")

    elif cmd == "deauth":
        ctx.network.authed_nicks.discard(ctx.nick)
        ctx.network.send_privmsg(ctx.nick,
            "Session dropped. You are no longer authenticated.")
