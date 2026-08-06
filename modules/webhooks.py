"""
modules/webhooks.py
===================

Two responsibilities:
  1. HTTP server that receives GitHub / Gitea / GitLab webhook POSTs and
     fans the formatted output to every subscribed IRC channel.
  2. IRC !webhook command for per-channel configuration.

IRC command syntax
------------------
  !webhook list
  !webhook add <github|gitea|gitlab> <owner/repo|owner|org>
  !webhook remove <github|gitea|gitlab> <hook>
  !webhook events <github|gitea|gitlab> <hook> [category ...]      -- show or set event categories
  !webhook branches <github|gitea|gitlab> <hook> [branch ...]      -- show or set branch filter
  !webhook show <github|gitea|gitlab> <hook>                       -- show full config for a hook

Default event categories (all active by default):
  ping  code  pr  issue  repo

All categories (pass any subset to "events"):
  ping  code  pr  pr-minimal  pr-all  pr-review-minimal
  issue  issue-minimal  issue-all  issue-comment-minimal
  repo  team  star

HMAC signature verification
----------------------------
Set "secret" in the webhook section of config.json.
For GitHub: X-Hub-Signature-256 header is checked.
Leave "secret" empty to skip verification.
"""

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import urllib.parse
from aiohttp import web

from modules.wh_github import GitHub
from modules.wh_gitea  import Gitea
from modules.wh_gitlab import GitLab
from src import formatting as fmt
from src import shlink

log = logging.getLogger(__name__)

FORM_ENCODED = "application/x-www-form-urlencoded"

DEFAULT_EVENT_CATEGORIES = ["ping", "code", "pr", "issue", "repo"]

_github = GitHub()
_gitea  = Gitea()
_gitlab = GitLab()

HANDLERS = {
    "github": (_github, "X-GitHub-Event", "X-Hub-Signature-256"),
    "gitea":  (_gitea,  "X-Gitea-Event",   None),
    "gitlab": (_gitlab, "X-Gitlab-Event",  "X-Gitlab-Token"),
}


# ============================================================== HTTP server

class WebhookServer:
    def __init__(self, bot):
        self.bot = bot
        self.cfg = bot.config.webhook

    async def start(self):
        app = web.Application()
        app.router.add_post("/github", lambda r: self._handle(r, "github"))
        app.router.add_post("/gitea",  lambda r: self._handle(r, "gitea"))
        app.router.add_post("/gitlab", lambda r: self._handle(r, "gitlab"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.host, self.cfg.port)
        await site.start()
        log.info("Webhook server listening on %s:%d", self.cfg.host, self.cfg.port)
        # keep running
        while True:
            await asyncio.sleep(3600)

    async def _handle(self, request: web.Request, platform: str):
        body = await request.read()
        headers = dict(request.headers)

        # Prefer CF-Connecting-IP (set by cloudflared tunnel) then X-Real-IP
        # (set by nginx), falling back to the raw peer address.
        real_ip = (headers.get("CF-Connecting-IP") or
                   headers.get("X-Real-IP") or
                   headers.get("X-Forwarded-For", "").split(",")[0].strip() or
                   request.remote)
        log.info("[webhook] %s request from %s", platform, real_ip)
        log.debug("[webhook] headers: %s", {k: v for k, v in headers.items()
                  if k.lower().startswith(("x-", "cf-", "forwarded"))})

        # Signature / secret verification — each platform does it differently.
        # If a ?secret= query parameter is present and matches config, that
        # counts as verified regardless of platform (simple shared password).
        # Otherwise fall through to platform-specific HMAC/token verification.
        secret = self.cfg.secret
        if secret:
            qs_secret = request.rel_url.query.get("secret", "")
            if qs_secret:
                if not hmac.compare_digest(qs_secret, secret):
                    log.warning("[webhook] query string secret mismatch (%s)", platform)
                    return web.Response(status=403, text="Forbidden")
            elif platform == "gitlab":
                # GitLab sends the secret as a plain token header (no HMAC).
                token = headers.get("X-Gitlab-Token", "")
                if token != secret:
                    log.warning("[webhook] GitLab secret mismatch")
                    return web.Response(status=403, text="Forbidden")
            elif platform == "github":
                # GitHub: X-Hub-Signature-256: sha256=<hmac>
                sig = headers.get("X-Hub-Signature-256", "")
                if not sig:
                    log.warning("[webhook] GitHub request missing X-Hub-Signature-256")
                    return web.Response(status=403, text="Forbidden")
                expected = "sha256=" + hmac.new(
                    secret.encode(), body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, sig):
                    log.warning("[webhook] GitHub HMAC mismatch")
                    return web.Response(status=403, text="Forbidden")
            elif platform == "gitea":
                # Gitea: X-Gitea-Signature: <hmac-sha256> (no "sha256=" prefix)
                sig = headers.get("X-Gitea-Signature", "")
                if not sig:
                    log.warning("[webhook] Gitea request missing X-Gitea-Signature")
                    return web.Response(status=403, text="Forbidden")
                expected = hmac.new(
                    secret.encode(), body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, sig):
                    log.warning("[webhook] Gitea HMAC mismatch")
                    return web.Response(status=403, text="Forbidden")

        handler, _event_hdr, _sig_hdr = HANDLERS[platform]

        # Decode payload
        content_type = headers.get("Content-Type", "")
        payload = body.decode("utf-8", errors="replace")
        if FORM_ENCODED in content_type:
            payload = urllib.parse.unquote(
                urllib.parse.parse_qs(payload).get("payload", ["{}"])[0])
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            log.error("[webhook] JSON parse error (%s): %s", platform, e)
            return web.Response(status=400, text="Bad JSON")

        is_private = handler.is_private(data, headers)
        full_name, repo_username, repo_name, organisation = handler.names(data, headers)
        branch   = handler.branch(data, headers)
        events   = handler.event(data, headers)

        full_lower = (full_name   or "").lower()
        user_lower = (repo_username or "").lower()
        org_lower  = (organisation  or "").lower()

        # Find all subscribed channels across all networks
        targets = _find_targets(self.bot, platform, full_lower, user_lower, org_lower,
                                is_private, branch, events, handler)

        if not targets:
            return web.Response(text=json.dumps({"state": "success", "deliveries": 0}),
                                content_type="application/json")

        outputs = handler.webhook(full_name, events[0], data, headers)
        if not outputs:
            return web.Response(text=json.dumps({"state": "success", "deliveries": 0}),
                                content_type="application/json")

        deliveries = 0
        for net_name, channel_name, hook in targets:
            net = self.bot.networks.get(net_name)
            if not net:
                continue

            source = full_name or organisation
            # Optionally hide organisation prefix
            hide_org = self.bot.db.get_channel(net_name, channel_name,
                                               "git-hide-organisation", False)
            if repo_name and hide_org:
                source = repo_name

            hide_prefix = self.bot.db.get_channel(net_name, channel_name,
                                                   "git-hide-prefix", False)
            prevent_hl = self.bot.db.get_channel(net_name, channel_name,
                                                  "git-prevent-highlight", False)

            channel_nicks = list(net.channel_members.get(channel_name.lower(), []))

            monochrome = net.has_channel_mode(channel_name, "c")

            for output, url in outputs:
                line = "(%s) %s" % (fmt.color(source, fmt.COLOR_REPO), output)
                if url:
                    use_shlink = self.bot.db.get_channel(net_name, channel_name,
                                                         "git-shlink", True)
                    short_url = await shlink.shorten(url) if use_shlink else url
                    line = "%s - %s" % (line, short_url)
                if prevent_hl:
                    line = fmt.prevent_highlight(channel_nicks, line)
                if not hide_prefix:
                    label = {"github": "GitHub", "gitea": "Gitea", "gitlab": "GitLab"}.get(platform, "Git")
                    line = "[%s] %s" % (fmt.color(label, fmt.GREEN), line)
                if monochrome:
                    line = fmt.strip_formatting(line)
                net.send_privmsg(channel_name, line)
                deliveries += 1

        return web.Response(
            text=json.dumps({"state": "success", "deliveries": deliveries}),
            content_type="application/json")


def _find_targets(bot, platform, full_lower, user_lower, org_lower,
                  is_private, branch, events, handler):
    """Return list of (net_name, channel, hook_dict) ready to receive this event.

    Hook keys are stored as "platform:repo" (e.g. "gitea:claude/ai") so that
    the same repo name on different platforms is handled independently.
    """
    rows = bot.db.find_by_channel_key("git-webhooks")
    targets = []
    for net_name, channel_name, hooks in rows:
        hook = _find_hook(platform, full_lower, user_lower, org_lower, hooks)
        if hook is None:
            continue

        # Private repo filter
        if is_private and not bot.db.get_channel(
                net_name, channel_name, "git-show-private", False):
            continue

        # Branch filter
        if branch and hook.get("branches") and branch not in hook["branches"]:
            continue

        # Event category filter
        hooked_events = set(itertools.chain.from_iterable(
            handler.event_categories(e) for e in hook.get("events", [])))
        if not set(events) & hooked_events:
            continue

        targets.append((net_name, channel_name, hook))
    return targets


_MISSING = object()

def _find_hook(platform, full_lower, user_lower, org_lower, hooks: dict):
    """Return the hook config dict if a platform-qualified key matches, else None.

    Keys are stored as "platform:name", e.g. "gitea:claude/ai" or "github:alice".
    Matching order: full "owner/repo", then "owner/user", then "org".
    Only keys whose platform prefix matches the incoming request platform are
    considered, so gitea:claude/ai and github:claude/ai never cross-match.
    """
    # Build a lookup of  (platform, name_lower) → value
    lower = {}
    for k, v in hooks.items():
        if ":" in k:
            plat, _, name = k.partition(":")
            lower[(plat.lower(), name.lower())] = v
        else:
            # Legacy key without platform prefix — match any platform.
            lower[(None, k.lower())] = v

    for name in filter(None, [full_lower, user_lower, org_lower]):
        # Prefer exact platform match, fall back to legacy platform-less key.
        for plat_key in [(platform, name), (None, name)]:
            v = lower.get(plat_key, _MISSING)
            if v is not _MISSING:
                return v
    return None


# ============================================================== IRC command

VALID_PLATFORMS = ("github", "gitea", "gitlab")

def _hook_key(platform: str, name: str) -> str:
    return "%s:%s" % (platform.lower(), name)

def _parse_platform_and_name(rest: list, ctx):
    """Pop and validate <platform> <name> from *rest*.
    Returns (key, platform, name, remaining) or (None, ...) after sending an error.
    """
    if len(rest) < 2:
        ctx.error(
            "Usage: !webhook <subcommand> <github|gitea|gitlab> <owner/repo|owner|org>"
        )
        return None, None, None, rest
    platform = rest[0].lower()
    if platform not in VALID_PLATFORMS:
        ctx.error("Unknown platform '%s'. Use: github, gitea, or gitlab." % rest[0])
        return None, None, None, rest
    name = rest[1]
    return _hook_key(platform, name), platform, name, rest[2:]


async def handle_command(ctx):
    """Handle !webhook <subcommand> [args…]

    In a channel:  !webhook add gitea claude/ai
    Via PM:        webhook add #channel gitea claude/ai

    Hook keys are stored as "platform:name" so the same repo name on
    different platforms is tracked independently.
    """
    if not ctx.is_admin:
        ctx.error("You must be an admin to use !webhook.")
        return

    if not ctx.args:
        if ctx.is_pm:
            ctx.reply(
                "Usage: !webhook <list|add|remove|events|branches|show|settings> "
                "#channel [github|gitea|gitlab] [args]"
            )
        else:
            ctx.reply(
                "Usage: !webhook list|add|remove|events|branches|show|settings "
                "[github|gitea|gitlab] [args]"
            )
        return

    sub = ctx.args[0].lower()
    rest = ctx.args[1:]

    # In a PM the #channel must come right after the subcommand.
    if ctx.is_pm:
        channel, rest = ctx.pm_channel(rest)
        if channel is None:
            return
        ctx._pm_channel = channel

    all_hooks: dict = ctx.bot.db.get_channel(
        ctx.net_name, ctx.channel, "git-webhooks", {})

    def _save():
        if all_hooks:
            ctx.bot.db.set_channel(ctx.net_name, ctx.channel, "git-webhooks", all_hooks)
        else:
            ctx.bot.db.del_channel(ctx.net_name, ctx.channel, "git-webhooks")

    def _find_existing(key):
        for k in all_hooks:
            if k.lower() == key.lower():
                return k
        return None

    if sub == "list":
        if not all_hooks:
            ctx.reply("No webhooks registered in this channel.")
        else:
            ctx.reply("Registered webhooks: %s" % ", ".join(sorted(all_hooks.keys())))

    elif sub == "add":
        key, platform, name, _ = _parse_platform_and_name(rest, ctx)
        if key is None:
            return
        existing = _find_existing(key)
        if existing:
            ctx.error("A hook for %s already exists." % key)
            return
        all_hooks[key] = {
            "events":   DEFAULT_EVENT_CATEGORIES.copy(),
            "branches": [],
        }
        _save()
        ctx.reply("Added webhook for %s (events: %s)"
                  % (key, ", ".join(DEFAULT_EVENT_CATEGORIES)))

    elif sub == "remove":
        key, platform, name, _ = _parse_platform_and_name(rest, ctx)
        if key is None:
            return
        existing = _find_existing(key)
        if not existing:
            ctx.error("No hook found for %s." % key)
            return
        del all_hooks[existing]
        _save()
        ctx.reply("Removed webhook for %s." % existing)

    elif sub == "events":
        key, platform, name, extra = _parse_platform_and_name(rest, ctx)
        if key is None:
            return
        existing = _find_existing(key)
        if not existing:
            ctx.error("No hook found for %s." % key)
            return
        if not extra:
            ctx.reply("Events for %s: %s"
                      % (existing, ", ".join(all_hooks[existing]["events"])))
        else:
            new_events = [e.lower() for e in extra]
            all_hooks[existing]["events"] = new_events
            _save()
            ctx.reply("Updated events for %s: %s" % (existing, ", ".join(new_events)))

    elif sub == "branches":
        key, platform, name, extra = _parse_platform_and_name(rest, ctx)
        if key is None:
            return
        existing = _find_existing(key)
        if not existing:
            ctx.error("No hook found for %s." % key)
            return
        if not extra:
            branches = all_hooks[existing].get("branches", [])
            ctx.reply("Branch filter for %s: %s"
                      % (existing, ", ".join(branches) if branches else "(all branches)"))
        else:
            all_hooks[existing]["branches"] = extra
            _save()
            ctx.reply("Branch filter for %s set to: %s"
                      % (existing, ", ".join(extra)))

    elif sub == "show":
        key, platform, name, _ = _parse_platform_and_name(rest, ctx)
        if key is None:
            return
        existing = _find_existing(key)
        if not existing:
            ctx.error("No hook found for %s." % key)
            return
        h = all_hooks[existing]
        branches = ", ".join(h.get("branches", [])) or "(all)"
        ctx.reply("%s — events: %s | branches: %s"
                  % (existing, ", ".join(h.get("events", [])), branches))

    elif sub == "settings":
        if not rest:
            opts = {
                "git-hide-organisation": ctx.bot.db.get_channel(
                    ctx.net_name, ctx.channel, "git-hide-organisation", False),
                "git-hide-prefix": ctx.bot.db.get_channel(
                    ctx.net_name, ctx.channel, "git-hide-prefix", False),
                "git-prevent-highlight": ctx.bot.db.get_channel(
                    ctx.net_name, ctx.channel, "git-prevent-highlight", False),
                "git-show-private": ctx.bot.db.get_channel(
                    ctx.net_name, ctx.channel, "git-show-private", False),
                "git-shlink": ctx.bot.db.get_channel(
                    ctx.net_name, ctx.channel, "git-shlink", True),
            }
            ctx.reply("Webhook settings: " +
                      " | ".join("%s=%s" % (k, v) for k, v in opts.items()))
        else:
            if len(rest) < 2:
                ctx.error("Usage: !webhook settings <key> <true|false>")
                return
            key, val = rest[0], rest[1].lower() in ("true", "1", "yes", "on")
            allowed = {"git-hide-organisation", "git-hide-prefix",
                       "git-prevent-highlight", "git-show-private", "git-shlink"}
            if key not in allowed:
                ctx.error("Unknown setting. Allowed: %s" % ", ".join(sorted(allowed)))
                return
            ctx.bot.db.set_channel(ctx.net_name, ctx.channel, key, val)
            ctx.reply("Set %s = %s" % (key, val))

    else:
        ctx.error(
            "Unknown subcommand '%s'. Use: list add remove events branches show settings"
            % sub
        )
