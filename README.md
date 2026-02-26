# gitbot

A minimal IRC bot for git forge webhooks (GitHub, Gitea, GitLab) and RSS feeds.
Supports multiple IRC networks simultaneously, hot reload of network config, and
persistent webhook/RSS routing managed entirely via IRC commands.

---

## Requirements

- Python 3.11+
- `aiohttp`, `feedparser` (installed via pip into a virtualenv — see below)

> **Python < 3.11:** install `tomli` as well (`pip install tomli`), which
> provides the TOML parser that became part of stdlib in 3.11.

---

## Installation

Modern Ubuntu (23.04+) and Debian (12+) prevent installing packages with pip
outside of a virtual environment. The steps below work on all supported systems.

### 1. Create a dedicated user (recommended)

```bash
sudo useradd -r -m -d /opt/gitbot -s /bin/bash gitbot
sudo -u gitbot bash
cd /opt/gitbot
```

### 2. Copy the bot files

```bash
# as the gitbot user, from /opt/gitbot
cp -r /path/to/gitbot/* .
```

### 3. Create a virtualenv and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The virtualenv lives at `/opt/gitbot/venv/`. You must activate it (or use the
full path to `venv/bin/python`) whenever running the bot manually.

### 4. Create the config file

```bash
cp gitbot.toml.example gitbot.toml
$EDITOR gitbot.toml
```

The config file controls IRC networks, the webhook HTTP server, RSS polling
interval, and optional bind addresses. See `gitbot.toml.example` for full
documentation of every option.

### 5. Create the owner account

The bot requires an owner account before it will start. Run setup once:

```bash
source venv/bin/activate   # if not already active
python bot.py --setup -c gitbot.toml
```

You will be prompted for a nick and password. These are stored in the SQLite
database (password hashed with PBKDF2-SHA256, 260 000 iterations).

### 6. Start the bot

```bash
python bot.py -c gitbot.toml
```

Add `-v` / `--verbose` for debug-level logging.

---

## Systemd service

Create `/etc/systemd/system/gitbot.service`:

```ini
[Unit]
Description=gitbot IRC webhook/RSS bot
After=network.target

[Service]
User=gitbot
Group=gitbot
WorkingDirectory=/opt/gitbot
ExecStart=/opt/gitbot/venv/bin/python bot.py -c /opt/gitbot/gitbot.toml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gitbot
sudo journalctl -fu gitbot   # follow logs
```

To run `--setup` with systemd in place:

```bash
sudo systemctl stop gitbot
sudo -u gitbot /opt/gitbot/venv/bin/python /opt/gitbot/bot.py --setup -c /opt/gitbot/gitbot.toml
sudo systemctl start gitbot
```

---

## Authentication

All bot management commands require you to be identified. Authentication is
**PM-only** — never send your password in a channel.

### Logging in

```
/msg gitbot identify <password>
/msg gitbot identify <nick> <password>   ← use this if your current nick differs
```

### Auto-login via hostmask

Once identified, add your current `nick!user@host` as a trusted mask so future
logins are automatic:

```
/msg gitbot hostmask add              ← adds your current nick!user@host
/msg gitbot hostmask add *!*@your.isp.net   ← or any glob pattern
/msg gitbot hostmask list
/msg gitbot hostmask remove <mask>
```

Globs (`*` and `?`) are supported anywhere in the mask. Useful patterns:

- `*!*@unaffiliated/yournick` — libera/oftc cloak
- `*!*@gateway/web/freenode/*` — web gateway
- `yournick!*@*` — any host, matched only when using your nick (less safe)

### Other account commands (all PM-only)

```
/msg gitbot passwd <newpassword>   ← change password
/msg gitbot logout                 ← end current session
```

---

## IRC commands

All channel commands start with `!`. The bot silently ignores commands from
unauthenticated users — it does not reveal that management commands exist.

### Reload config

Re-reads `gitbot.toml` without restarting. Connects new networks, disconnects
removed ones, joins/parts channels as needed, and cleans up orphaned webhook
and RSS entries from the database for any removed networks or channels.

```
!reload
```

### Webhooks

```
!webhook list
!webhook add <repo>
!webhook remove <repo>
!webhook events <repo>                   ← show current event filter
!webhook events <repo> code pr repo      ← set event filter
!webhook branches <repo>                 ← show current branch filter
!webhook branches <repo> main develop    ← only announce these branches
```

`<repo>` can be:
- `owner/repo` — a specific repository
- `owner` — all repos by that owner (matched against the owner field in the payload)

Configure your forge to send webhooks to:
```
https://yourhost/github
https://yourhost/gitea
https://yourhost/gitlab
```

Optionally append `?secret=<value>` to authenticate without HMAC setup:
```
https://yourhost/gitea?secret=mysecret
```

### RSS feeds

```
!rss list
!rss add <url>
!rss remove <url>
!rss format <url>                        ← show current format template
!rss format <url> $feed_name: $title <$link>   ← set format template
```

On first add, the bot delivers the most recent entry immediately so you can
confirm the feed is working and preview the format. Subsequent polls deliver
up to 3 new entries per cycle.

**RSS format template variables** (any feedparser field works):

| Variable | Description |
|---|---|
| `$feed_name` | Feed title, with Gitea's `Feed of "..."` wrapper stripped |
| `$feed_title` | Raw feed title |
| `$feed_link` | Feed URL |
| `$feed_author` | Feed-level author |
| `$feed_subtitle` | Feed subtitle/description |
| `$title` | Entry title (HTML stripped) |
| `$link` | Entry URL |
| `$description` | Entry summary/description (HTML stripped) |
| `$author` | Entry author |
| `$published` | Publication date string |
| `$id` | Entry unique ID |

Default format: `$feed_name: $title <$link>`

Uses Python's `string.Template` with `safe_substitute`, so unknown variables
are left as-is rather than raising an error.

---

## Webhook event categories

Use these with `!webhook events`:

| Category | What it includes |
|---|---|
| `ping` | New webhook registered confirmation |
| `code` | Pushes, commit comments |
| `pr` | PR opened/closed/merged/reviewed/commented (+ label, rename, sync) |
| `pr-minimal` | PR opened/closed/reopened/merged only |
| `issue` | Issues opened/closed/edited/assigned + comments |
| `issue-minimal` | Issues opened/closed/reopened only |
| `repo` | Releases, forks, branch/tag create/delete |
| `star` | Stars (GitHub only) |

You can also use raw forge event names like `push`, `pull_request`,
`merge_request`, etc. Default filter when adding a webhook: `ping code pr issue repo`.

---

## Webhook secrets

Each forge has its own secret to allow the same `owner/repo` slug to exist on
multiple forges owned by different people:

```toml
[webhook_server]
github_secret = "abc"
gitea_secret  = "xyz"
gitlab_secret = "def"
```

Two verification modes are supported — pick whichever is easier:

**URL token** (simplest — just append to the payload URL):
```
https://yourhost/gitea?secret=xyz
```

**HMAC header** (more secure — secret never appears in URLs or logs):
Set the secret in both `gitbot.toml` and the forge's webhook settings UI.
GitHub and Gitea use HMAC-SHA256; GitLab sends the token directly.

If a URL token is present it takes priority; otherwise HMAC headers are checked.
If no secret is configured for a forge, all requests are accepted.

---

## Database

SQLite at the path configured by `database` in `gitbot.toml` (default: `gitbot.db`).

Stores:
- Owner account (nick + hashed password + hostmasks)
- Webhook routing (network, channel, repo, event filter, branch filter)
- RSS feeds (network, channel, URL, format template)
- Seen RSS entry IDs (to avoid re-announcing; capped at 500 per feed)

Schema is created automatically on first run. The `format` column added in a
later release is migrated automatically via `ALTER TABLE` if you're upgrading
from an earlier version.

---

## Migrating between machines

The database is fully portable — copy `gitbot.db` alongside `gitbot.toml`.

The only machine-specific config is the `bind` address. Update or remove it
in `gitbot.toml` before starting on the new host.

```bash
# on old machine
systemctl stop gitbot
scp gitbot.db gitbot.toml newhost:/opt/gitbot/

# on new machine — edit bind if needed, then:
systemctl start gitbot
```
