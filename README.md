# GitBot

A single IRC bot that combines:
- **Bitbot's** GitHub / Gitea / GitLab webhook announcements
- **Limnoria's** RSS/Atom feed polling and announcement system

Multi-network, SQLite-backed, configured entirely through IRC commands.

---

## Requirements

- Python 3.9+
- `pip install -r requirements.txt`  (aiohttp, feedparser)

---

## Quick start

```bash
cp config.example.json config.json
# edit config.json — set your networks, nick, admins, webhook port
python bot.py config.json
```

---

## Configuration (`config.json`)

| Field | Default | Description |
|---|---|---|
| `networks` | — | List of IRC network objects (see below) |
| `webhook.host` | `127.0.0.1` | IP to bind the webhook HTTP server |
| `webhook.port` | `8765` | Port for the webhook HTTP server |
| `webhook.secret` | `""` | HMAC secret; leave empty to skip verification |
| `auth_password` | `""` | Global password for `!auth` (PM-only admin login); leave empty to disable |
| `shlink.url` | `""` | Base URL of your [Shlink](https://shlink.io) instance |
| `shlink.api_key` | `""` | Shlink REST API key |
| `db_path` | `data/gitbot.db` | SQLite database path |
| `rss_interval` | `300` | RSS poll interval in seconds |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Network object

```json
{
  "name":           "libera",
  "host":           "irc.libera.chat",
  "port":           6697,
  "tls":            true,
  "nick":           "mybot",
  "username":       "mybot",
  "realname":       "Gitbot",
  "password":       "",
  "sasl_password":  "",
  "channels":       ["#myproject"],
  "command_prefix": "!",
  "bind":           "",
  "admins":         ["mynick!myuser@myhost"]
}
```

`admins` are glob patterns matched against `nick!user@host`.  
`sasl_password` enables SASL PLAIN authentication.  
`bind` (optional) binds the outbound connection to a specific local IP —
useful on a multi-homed box, or to give each network its own source IP.
Leave empty to use the default route.

---

## URL shortening (Shlink)

Gitbot can shorten webhook URLs via a self-hosted [Shlink](https://shlink.io) instance:

```json
"shlink": {
  "url":     "https://s.example.com",
  "api_key": "YOUR-API-KEY"
}
```

Omit the block (or leave `url`/`api_key` empty) to disable. Can also be toggled
per-channel with `!webhook settings git-shlink false` (see below).

---

## Reloading config

Config can be reloaded without restarting the bot in two ways:

- **IRC:** `!reload` — reloads `config.json` and reports what changed
- **Terminal:** `kill -HUP <pid>` — same effect, no IRC output

On reload, networks added to `config.json` are connected automatically.  
Networks removed from `config.json` are disconnected, but their channel
settings are **kept** in the database in case the removal was accidental.
Use `!reload --purge` to also delete DB rows for removed networks.

---

## Webhook setup

### 1. Start the bot

The built-in HTTP server listens on `webhook.host:webhook.port`.

Webhook endpoints:
| Platform | URL path |
|---|---|
| GitHub   | `POST /github` |
| Gitea    | `POST /gitea`  |
| GitLab   | `POST /gitlab` |

### 2. Reverse-proxy (recommended)

Put nginx, Apache, or Caddy in front so you can use HTTPS:

nginx:
```nginx
location /github {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Apache:
```apache
ProxyPass /github http://127.0.0.1:8765/github
ProxyPassReverse /github http://127.0.0.1:8765/github
ProxyPreserveHost On
RequestHeader set X-Real-IP %{REMOTE_ADDR}s
```

Caddy:
```caddy
handle /github {
    reverse_proxy 127.0.0.1:8765
}
```

### 3. Register a webhook in IRC

```
!webhook add github owner/repo
```

Then point your GitHub/Gitea/GitLab webhook at `https://your.host/github`  
(or `/gitea`, `/gitlab`).

### 4. HMAC secret (optional but recommended)

Set `webhook.secret` in `config.json` and paste the same value into  
GitHub → Settings → Webhooks → Secret.

---

## IRC commands — Webhooks

All webhook commands require admin.

```
!webhook list
    List all hooks registered in this channel.

!webhook add <github|gitea|gitlab> <owner/repo|owner|org>
    Register a new webhook hook on a specific platform.
    Use "owner/repo" for a specific repo, "owner" for all repos from a user,
    or an org name for all repos in an organisation.
    The same repo name is tracked independently per platform, so
    "gitea:acme/widget" and "github:acme/widget" never collide.

!webhook remove <github|gitea|gitlab> <hook>
    Remove a hook.

!webhook events <github|gitea|gitlab> <hook> [category ...]
    Show or replace the event category filter.
    If no categories given, shows the current list.

!webhook branches <github|gitea|gitlab> <hook> [branch ...]
    Show or replace the branch filter.
    If no branches given, shows the current filter (empty = all branches).

!webhook show <github|gitea|gitlab> <hook>
    Show the full configuration for a hook.

!webhook settings
    Show all display settings for this channel.

!webhook settings <key> <true|false>
    Toggle a display setting. Keys:
      git-hide-organisation   -- hide the "owner/" prefix in repo names
      git-hide-prefix         -- hide the "[git]" prefix from announcements
      git-prevent-highlight   -- insert ZWNJ to avoid pinging channel users
      git-show-private        -- announce events from private repositories
      git-shlink              -- shorten URLs via Shlink (default: true)
```

### Event categories

Pass one or more to `!webhook events <hook> <category ...>`:

| Category | Events included |
|---|---|
| `ping` | new webhook |
| `code` | push, commit_comment |
| `pr-minimal` | PR opened/closed/reopened |
| `pr` | all common PR events |
| `pr-all` | every PR sub-event |
| `pr-review-minimal` | review submitted/dismissed |
| `issue-minimal` | issue opened/closed/reopened/deleted |
| `issue` | all common issue events |
| `issue-all` | every issue sub-event |
| `issue-comment-minimal` | issue comment created |
| `pr-review-comment-minimal` | PR review comment created |
| `repo` | create, delete, release, fork |
| `star` | watch (GitHub star) |
| `team` | membership changes |

Default: `ping code pr issue repo`

---

## IRC commands — RSS

```
!rss list
    List all globally registered named feeds.

!rss add <name> <url>
    Register a named feed.  (admin)
    Example: !rss add limnoria https://github.com/progval/Limnoria/releases.atom

!rss remove <name>
    Remove a named feed.  (admin)

!rss announce list
    List feeds being announced in this channel.

!rss feeds [global|#channel]
    List feeds announced in this channel, in another channel, or (with
    "global") every named feed alongside every network/channel announcing it.

!rss announce add <name|url> [<name|url> ...]
    Start announcing a feed in this channel.  (admin)
    Accepts either a registered feed name or a direct URL.
    Existing entries are silently marked as seen — no flood on first add.

!rss announce remove <name|url> [...]
    Stop announcing a feed in this channel.  (admin)

!rss read [<name|url>] [<n>]
    Fetch and display the latest n entries (default 3, max 10).
    If no feed specified, uses the first announced feed.

!rss info <name|url>
    Show feed metadata (title, entry count, last updated).

!rss format [<template>]
    Show or set the announcement format template for this channel.
    Default: [$feed_name] $title — $link

!rss interval [<seconds>]
    Show or set the poll interval.  (admin, minimum 30 s)

!rss hideprefix [on|off]
    Show or toggle the "[RSS]" prefix for this channel.  (admin to set)
    Default: off (prefix shown)
```

### Format template variables

| Variable | Content |
|---|---|
| `$feed_name` | Registered name or URL |
| `$feed_title` | Title from the feed itself |
| `$title` | Entry title (HTML stripped) |
| `$link` | Entry URL |
| `$author` | Entry author |
| `$date` | Published/updated date |
| `$description` | Entry summary (HTML stripped) |

Plus any raw field from the feedparser entry dict.

Example custom format:
```
!rss format [$feed_name] $title by $author ($date) → $link
```

---

## Help

```
!help              -- list available commands
!help <command>    -- show usage for one command
```

---

## Admin commands

```
!join <#channel>
!part [#channel] [reason]
!say <target> <message>
!raw <irc line>
!networks
!quit [reason]
!reload [--purge]
```

## Authenticating as admin (PM only)

If `auth_password` is set in `config.json`, anyone who knows it can gain
admin rights for their current session without being listed in a
network's `admins` masks:

```
/msg gitbot auth <password>
/msg gitbot deauth
```

`auth` is PM-only (it would leak the password to the channel otherwise).
`deauth` drops the session's admin rights early; sessions also lose them
on disconnect.

---

## Using commands via PM

All commands work in a private message to the bot. The `!` prefix is
optional in PMs — there's no channel chatter to disambiguate from, so
either form works.  
Channel-scoped commands take `#channel` as their first argument:

```
/msg gitbot webhook list #mychannel
/msg gitbot webhook add #mychannel github owner/repo
/msg gitbot webhook settings git-shlink false #mychannel
/msg gitbot rss announce list #mychannel
/msg gitbot rss format #mychannel [$feed_name] $title → $link
/msg gitbot reload
```

This is useful for keeping configuration chatter out of public channels.

---

## Multi-network behaviour

- Each network is an independent connection with its own nick, channels, and admin list.
- Webhook hooks and RSS announcements are stored per-**network**/channel, so the same  
  webhook can fan out to different channels on different networks simultaneously.
- Adding or removing a network in `config.json` and running `!reload` (or `kill -HUP`)
  connects/disconnects it live. Removed networks keep their DB entries unless you
  run `!reload --purge`.
- To see which network a channel is on, use `!networks`.

---

## Architecture

```
bot.py                  entry point, asyncio.run()
src/
  config.py             JSON config loader (incl. ShlinkConfig)
  database.py           SQLite wrapper (channel_settings, bot_settings)
  formatting.py         IRC colour/bold helpers
  network.py            async IRC connection (TLS, SASL, flood throttle, reconnect)
  bot.py                Bot class, command router, reload_config(), SIGHUP handler
  shlink.py             async Shlink URL shortener client
modules/
  webhooks.py           HTTP server + !webhook IRC command
  wh_github.py          GitHub payload handler
  wh_gitea.py           Gitea payload handler
  wh_gitlab.py          GitLab payload handler
  rss.py                RSS poller + !rss IRC command
  admin.py              !join !part !say !raw !quit !reload !auth !deauth
  help.py               !help IRC command
data/
  gitbot.db         auto-created SQLite database
```

---

## Licence

MIT — feel free to use, modify, and redistribute.
