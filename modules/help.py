"""
modules/help.py
================

  help              -- list available commands
  help <command>    -- show usage for one command

Works in channels (with the command prefix) and via PM (prefix optional),
same as every other command.
"""

# name -> short usage line.
# Keep this in sync with the commands actually wired up in
# src/bot.py:dispatch_command().
COMMANDS = {
    "help":     "help [command] -- list commands, or show usage for one command",
    "webhook":  "webhook <list|settings> [args] | webhook <add|remove|events|branches|show> "
                "<github|gitea|gitlab> <owner/repo|owner|org> [args] "
                "-- manage GitHub/Gitea/GitLab webhook subscriptions for a channel "
                "(alias: wh)",
    "rss":      "rss <add|remove|list|feeds|announce|read|info|format|interval|hideprefix> [args] "
                "-- manage RSS/Atom feed subscriptions",
    "auth":     "auth <password> -- authenticate this session as admin (PM only)",
    "deauth":   "deauth -- drop your authenticated admin session (PM only)",
    "join":     "join <#channel> -- join a channel (admin)",
    "part":     "part [#channel] [reason] -- leave a channel (admin)",
    "say":      "say <target> <message> -- make the bot say something (admin)",
    "raw":      "raw <irc line> -- send a raw IRC line, use with care (admin)",
    "networks": "networks -- list connected networks (admin)",
    "quit":     "quit [reason] -- disconnect from all networks and exit (admin)",
    "reload":   "reload [--purge] -- reload config.json from disk (admin); "
                "--purge also deletes DB rows for removed networks",
}

ALIASES = {"wh": "webhook"}


async def handle_command(ctx):
    if not ctx.args:
        names = sorted(COMMANDS)
        ctx.reply("Available commands: %s (use 'help <command>' for usage)"
                   % ", ".join(names))
        return

    name = ALIASES.get(ctx.args[0].lower(), ctx.args[0].lower())
    usage = COMMANDS.get(name)
    if usage is None:
        ctx.error("Unknown command '%s'. Use 'help' to list commands." % ctx.args[0])
        return

    ctx.reply("Usage: %s" % usage)
