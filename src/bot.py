"""
Core Bot class.

Responsibilities
----------------
- Spin up IRC network connections
- Start the webhook HTTP server
- Start the RSS polling loop
- Route IRC commands to the right module handler
- Provide a shared interface for modules to send messages
"""

import asyncio
import fnmatch
import logging
import os
import signal
import typing

from src.config import Config
from src.database import Database
from src.network import Network
from src import shlink as _shlink

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, config: Config):
        self.config = config
        os.makedirs(os.path.dirname(config.db_path) or ".", exist_ok=True)
        self.db = Database(config.db_path)
        self.networks: typing.Dict[str, Network] = {}
        self._network_tasks: typing.Dict[str, asyncio.Task] = {}

        # Initialise Shlink URL shortener (no-op if not configured)
        _shlink.configure(config.shlink.url, config.shlink.api_key)

        # Lazy-loaded modules
        self._modules: dict = {}
        self._load_modules()

    # ----------------------------------------------------------------- startup

    async def run(self):
        tasks = []

        # IRC networks
        for net_cfg in self.config.networks:
            net = Network(net_cfg, self)
            self.networks[net_cfg.name] = net
            task = asyncio.ensure_future(net.run())
            self._network_tasks[net_cfg.name] = task
            tasks.append(task)

        # Webhook HTTP server
        from modules.webhooks import WebhookServer
        wh = WebhookServer(self)
        tasks.append(asyncio.ensure_future(wh.start()))

        # RSS poller
        from modules.rss import RSSPoller
        rss = RSSPoller(self)
        tasks.append(asyncio.ensure_future(rss.run()))

        # SIGHUP → reload config (Unix only)
        try:
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(
                signal.SIGHUP,
                lambda: asyncio.ensure_future(self.reload_config())
            )
            log.info("SIGHUP handler registered — send SIGHUP to reload config.")
        except (AttributeError, NotImplementedError):
            pass   # Windows doesn't have SIGHUP

        await asyncio.gather(*tasks)
        await _shlink.close()

    # ------------------------------------------------------------------ reload

    async def reload_config(self) -> typing.Tuple[list, list]:
        """Re-read config.json, reconcile network connections, and purge DB
        rows for any networks that were removed.

        Returns (added_names, removed_names) for reporting to the caller.
        """
        log.info("Reloading configuration from %s", self.config.path)
        self.config.reload()

        # Re-configure shlink in case its settings changed
        _shlink.configure(self.config.shlink.url, self.config.shlink.api_key)

        old_names = set(self.networks.keys())
        new_names = {nc.name for nc in self.config.networks}

        removed = old_names - new_names
        added   = new_names - old_names

        # --- disconnect removed networks ---
        for name in removed:
            net = self.networks.pop(name, None)
            task = self._network_tasks.pop(name, None)
            if net:
                try:
                    net._raw("QUIT :Network removed from config")
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            purged = self.db.purge_network(name)
            log.info("Removed network %r — purged %d DB rows", name, purged)

        # --- connect new networks ---
        for net_cfg in self.config.networks:
            if net_cfg.name in added:
                net = Network(net_cfg, self)
                self.networks[net_cfg.name] = net
                task = asyncio.ensure_future(net.run())
                self._network_tasks[net_cfg.name] = task
                log.info("Added network %r", net_cfg.name)

        log.info("Reload complete. Added: %s  Removed: %s",
                 list(added) or "none", list(removed) or "none")
        return sorted(added), sorted(removed)

    # --------------------------------------------------------------- modules

    def _load_modules(self):
        from modules import webhooks, rss, admin, help as help_module
        self._modules = {
            "webhook":  webhooks,
            "rss":      rss,
            "admin":    admin,
            "help":     help_module,
        }

    # ------------------------------------------------------- command dispatch

    async def dispatch_command(self, network: Network, hostmask: str,
                               nick: str, target: str,
                               command: str, args: list,
                               is_pm: bool = False):
        is_admin = self._is_admin(network, hostmask, nick)

        ctx = CommandContext(
            bot=self,
            network=network,
            nick=nick,
            hostmask=hostmask,
            target=target,
            command=command,
            args=args,
            is_admin=is_admin,
            is_pm=is_pm,
        )

        # Route
        if command in ("webhook", "wh"):
            from modules.webhooks import handle_command
            await handle_command(ctx)
        elif command == "rss":
            from modules.rss import handle_command
            await handle_command(ctx)
        elif command in ("join", "part", "quit", "say", "raw", "networks", "reload"):
            from modules.admin import handle_command
            await handle_command(ctx)
        elif command in ("auth", "deauth"):
            from modules.admin import handle_auth
            await handle_auth(ctx)
        elif command == "help":
            from modules.help import handle_command
            await handle_command(ctx)

    def _is_admin(self, network: Network, hostmask: str,
                 nick: str = "") -> bool:
        # Session auth takes priority — no hostmask needed.
        if nick and nick in network.authed_nicks:
            return True
        for pattern in network.config.admins:
            if fnmatch.fnmatchcase(hostmask, pattern):
                return True
        return False

    # ---------------------------------------------------------- helper methods

    def send(self, network_name: str, channel: str, message: str):
        net = self.networks.get(network_name)
        if net:
            net.send_privmsg(channel, message)

    def broadcast(self, network_name: str, channel: str, lines: list):
        net = self.networks.get(network_name)
        if net:
            for line in lines:
                net.send_privmsg(channel, line)


class CommandContext:
    """Holds all context for a single IRC command invocation.

    PM behaviour
    ------------
    When a command arrives via PM (is_pm=True) the module should call
    ctx.pm_channel(args) to pop the leading #channel argument and return
    (channel_name, remaining_args).  If the caller forgot to supply a
    channel, pm_channel() replies with a usage hint and returns (None, args).

    ctx.channel always returns the right target for DB queries:
      - in a channel message: the channel name
      - in a PM: the #channel from args (or None if not yet parsed)

    ctx.reply() always responds to the right place:
      - in a channel: the channel
      - in a PM: back to the sender's nick (so the convo stays in PM)
    """

    def __init__(self, bot: Bot, network: Network, nick: str, hostmask: str,
                 target: str, command: str, args: list, is_admin: bool,
                 is_pm: bool = False):
        self.bot = bot
        self.network = network
        self.nick = nick
        self.hostmask = hostmask
        self.target = target        # IRC target: channel name, or bot nick if PM
        self.command = command
        self.args = args
        self.is_admin = is_admin
        self.is_pm = is_pm
        self._pm_channel: typing.Optional[str] = None  # set by pm_channel()

    @property
    def net_name(self) -> str:
        return self.network.name

    @property
    def channel(self) -> str:
        """The IRC channel this command operates on.

        - In a channel message: the channel itself.
        - In a PM after pm_channel() has been called: the #channel from args.
        - In a PM before pm_channel() is called: falls back to target (bot nick).
        """
        if self.is_pm and self._pm_channel:
            return self._pm_channel
        return self.target

    def pm_channel(self, args: list) -> typing.Tuple[typing.Optional[str], list]:
        """Pop and validate the leading #channel argument from a PM command.

        Returns (channel, remaining_args) on success, or (None, args) after
        sending an error reply if the channel argument is missing or invalid.

        Usage in a command handler::

            channel, args = ctx.pm_channel(ctx.args)
            if channel is None:
                return  # error already sent
            ctx._pm_channel = channel  # makes ctx.channel work correctly
        """
        if not self.is_pm:
            # Not a PM — channel is already implicit; nothing to pop.
            return self.target, args

        if not args or not args[0].startswith(("#", "&", "+", "!")):
            self.reply(
                "When messaging me directly, include the target channel as the "
                "first argument.  Example:  webhook add #mychannel owner/repo"
            )
            return None, args

        channel = args[0]
        self._pm_channel = channel
        return channel, args[1:]

    def reply(self, text: str):
        """Reply to the right place: channel if in a channel, nick if in a PM."""
        dest = self.nick if self.is_pm else self.target
        self.network.send_privmsg(dest, text)

    def reply_notice(self, text: str):
        self.network.send_notice(self.nick, text)

    def error(self, text: str):
        self.reply(f"Error: {text}")
