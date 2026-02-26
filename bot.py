"""
gitbot — main entry point.

Usage:
    python bot.py [gitbot.toml]          # normal run
    python bot.py --setup [gitbot.toml]  # create/reset owner account, then run
"""

import asyncio
import getpass
import itertools
import logging
import sys

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("Python < 3.11 detected. Install tomli: pip install tomli",
              file=sys.stderr)
        sys.exit(1)

import auth
import commands
import db
import irc_format as fmt
import rss as rss_module
import webhook_github
import webhook_gitea
import webhook_gitlab
from irc_client import IRCClient
from webhook_server import WebhookServer

log = logging.getLogger("bot")

PARSERS = {
    "github": webhook_github,
    "gitea":  webhook_gitea,
    "gitlab": webhook_gitlab,
}

DEFAULT_EVENTS = {"ping", "code", "pr", "issue", "repo"}


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def run_setup(database):
    """Interactive terminal setup for the owner account."""
    print()
    if auth.has_owner(database):
        print("An owner account already exists.")
        answer = input("Reset it? [y/N] ").strip().lower()
        if answer != "y":
            print("Setup cancelled.")
            return
    print("── gitbot owner account setup ──────────────────")
    nick = input("Owner nick: ").strip()
    if not nick:
        print("Nick cannot be empty.")
        sys.exit(1)
    while True:
        password  = getpass.getpass("Password: ")
        password2 = getpass.getpass("Confirm password: ")
        if not password:
            print("Password cannot be empty.")
        elif password != password2:
            print("Passwords do not match, try again.")
        else:
            break
    auth.create_owner(database, nick, password)
    print(f"Owner account created for '{nick}'.")
    print("You can now /msg the bot:  identify <password>")
    print()


class Bot:
    def __init__(self, config: dict, config_path: str):
        self._cfg         = config
        self._config_path = config_path
        self._database    = db.connect(config.get("database", "gitbot.db"))
        self._clients: dict[str, IRCClient] = {}

        self._load_static_webhooks()
        self._load_static_rss()

    # ── Static config loading ─────────────────────────────────────────────────

    def _load_static_webhooks(self):
        for net_cfg in self._cfg.get("network", []):
            net = net_cfg["name"]
            for ch_cfg in net_cfg.get("channel", []):
                ch = ch_cfg["name"]
                for hook in ch_cfg.get("webhook", []):
                    db.webhook_add(
                        self._database, net, ch,
                        hook["repo"],
                        hook.get("events", list(DEFAULT_EVENTS)),
                        hook.get("branches", []),
                    )

    def _load_static_rss(self):
        for net_cfg in self._cfg.get("network", []):
            net = net_cfg["name"]
            for ch_cfg in net_cfg.get("channel", []):
                ch = ch_cfg["name"]
                for url in ch_cfg.get("rss", []):
                    db.rss_add(self._database, net, ch, url)  # (id, created) ignored here

    # ── IRC message routing ───────────────────────────────────────────────────

    async def _on_message(self, network: str, target: str, nick: str,
                          prefix: str, text: str):
        own_nick = self._clients[network].config["nickname"]

        if target.lower() == own_nick.lower():
            # Private message to the bot
            async def pm_reply(msg):
                self._clients[network].privmsg(nick, msg)

            await commands.handle_pm(
                network, nick, prefix, text,
                self._database, pm_reply, self.reload)
        elif target.startswith("#"):
            # Channel message
            async def ch_reply(msg):
                await self._deliver_irc(network, target, msg)

            await commands.handle_channel(
                network, target, nick, prefix, text,
                self._database, ch_reply, self.reload)

    async def _on_connected(self, network: str):
        log.info("[%s] Connected and registered", network)

    # ── Hot reload ────────────────────────────────────────────────────────────

    async def reload(self) -> str:
        """
        Re-read the config file and reconcile network connections:
          - New networks  → connect
          - Gone networks → QUIT and stop
          - Kept networks → join any new channels, part removed ones
        Returns a human-readable summary.
        """
        try:
            new_cfg = load_config(self._config_path)
        except Exception as e:
            return f"Failed to reload config: {e}"

        self._cfg = new_cfg
        self._load_static_webhooks()
        self._load_static_rss()

        global_bind  = new_cfg.get("bind")
        new_nets     = {n["name"]: n for n in new_cfg.get("network", [])}
        current_nets = set(self._clients.keys())

        added   = []
        removed = []
        updated = []

        # Disconnect networks that are no longer in config
        for name in list(current_nets):
            if name not in new_nets:
                log.info("Reload: disconnecting %s", name)
                await self._clients[name].stop("Configuration removed")
                del self._clients[name]
                db.purge_network(self._database, name)
                removed.append(name)

        # Connect new networks; reconcile channels on existing ones
        for name, net_cfg in new_nets.items():
            if global_bind and "bind" not in net_cfg:
                net_cfg = {**net_cfg, "_global_bind": global_bind}

            if name not in self._clients:
                # Brand new network
                log.info("Reload: connecting new network %s", name)
                self._start_network(net_cfg)
                added.append(name)
            else:
                # Existing network — reconcile channels
                client    = self._clients[name]
                new_chans = {c.lower() for c in net_cfg.get("channels", [])}
                cur_chans = set(client._channels)  # already lowercase

                for ch in new_chans - cur_chans:
                    log.info("Reload: joining %s on %s", ch, name)
                    client.join(ch)

                for ch in cur_chans - new_chans:
                    log.info("Reload: parting %s on %s", ch, name)
                    client.send(f"PART {ch} :Removed from config")
                    db.purge_channel(self._database, name, ch)

                if new_chans != cur_chans:
                    updated.append(name)

        parts = []
        if added:
            parts.append(f"connected: {', '.join(added)}")
        if removed:
            parts.append(f"disconnected: {', '.join(removed)}")
        if updated:
            parts.append(f"channels updated: {', '.join(updated)}")
        return "Reloaded. " + ("; ".join(parts) if parts else "no network changes.")

    def _start_network(self, net_cfg: dict):
        client = IRCClient(
            net_cfg,
            on_message=self._on_message,
            on_connected=self._on_connected,
        )
        self._clients[net_cfg["name"]] = client
        asyncio.create_task(client.run())

    # ── IRC delivery ──────────────────────────────────────────────────────────

    async def _deliver_irc(self, network: str, channel: str, message: str):
        client = self._clients.get(network)
        if not client:
            log.warning("No client for network %s", network)
            return
        if not client.in_channel(channel):
            log.debug("[%s] Not in %s yet, joining…", network, channel)
            client.join(channel)
            await asyncio.sleep(2)
        client.privmsg(channel, message)

    # ── Webhook delivery ──────────────────────────────────────────────────────

    async def _on_webhook(self, forge: str, headers: dict, data: dict):
        parser = PARSERS.get(forge)
        if not parser:
            return

        full_name, repo_user, repo_name, organisation = parser.names(data, headers)
        branch  = parser.branch(data, headers)
        events  = parser.event(data, headers)
        primary = events[0] if events else ""

        targets = db.webhook_targets(
            self._database, full_name, repo_user, organisation)

        if not targets:
            log.debug("[%s] No targets for %s", forge, full_name)
            return

        outputs = parser.parse(full_name, primary, data, headers)
        if not outputs:
            return

        for target in targets:
            if branch and target["branches"] and branch not in target["branches"]:
                continue

            allowed = set(itertools.chain.from_iterable(
                parser.event_categories(e) for e in target["events"]
            ))
            if not set(events) & allowed:
                continue

            source = fmt.color(
                full_name or organisation or repo_name or forge,
                fmt.COLOR_REPO)

            for message, url in outputs:
                line = f"({source}) {message}"
                if url:
                    line = f"{line} - {url}"
                await self._deliver_irc(target["network"], target["channel"], line)

    # ── Main run ──────────────────────────────────────────────────────────────

    async def run(self):
        global_bind = self._cfg.get("bind")

        for net_cfg in self._cfg.get("network", []):
            if global_bind and "bind" not in net_cfg:
                net_cfg = {**net_cfg, "_global_bind": global_bind}
            self._start_network(net_cfg)

        wh_cfg = self._cfg.get("webhook_server", {})
        if wh_cfg.get("enabled", True):
            # Per-forge secrets; fall back to legacy 'secret' key for all forges
            legacy = wh_cfg.get("secret", "")
            secrets = {
                "github": wh_cfg.get("github_secret", legacy),
                "gitea":  wh_cfg.get("gitea_secret",  legacy),
                "gitlab": wh_cfg.get("gitlab_secret",  legacy),
            }
            server = WebhookServer(
                host=wh_cfg.get("host", "127.0.0.1"),
                port=wh_cfg.get("port", 8080),
                deliver=self._on_webhook,
                secrets=secrets,
            )
            asyncio.create_task(server.run())

        rss_cfg = self._cfg.get("rss", {})
        if rss_cfg.get("enabled", True):
            poller = rss_module.RSSPoller(
                database=self._database,
                deliver=self._deliver_irc,
                interval=rss_cfg.get("interval", 300),
            )
            asyncio.create_task(poller.run())

        log.info("gitbot started")
        await asyncio.Event().wait()  # run forever


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="gitbot — git webhook + RSS IRC bot")
    parser.add_argument("-c", "--config", default="gitbot.toml", metavar="FILE",
                        help="Path to TOML config file (default: gitbot.toml)")
    parser.add_argument("--setup", action="store_true",
                        help="Create or reset the owner account, then start the bot")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    # Open DB early so setup can use it
    database = db.connect(config.get("database", "gitbot.db"))

    if args.setup:
        run_setup(database)
    elif not auth.has_owner(database):
        print("No owner account found. Run with --setup first:", file=sys.stderr)
        print(f"  python bot.py --setup -c {args.config}", file=sys.stderr)
        sys.exit(1)

    bot = Bot(config, args.config)
    # Re-use the already-open DB connection
    bot._database = database

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
