# Gitbot — Beginner's Guide

This guide assumes you have never run an IRC bot before.  
No prior Python or IRC experience required.

---

## What does this bot do?

It sits in your IRC channels and does two things:

1. **Git webhooks** — when someone pushes code, opens a PR, or creates an issue on
   GitHub, Gitea, or GitLab, the bot announces it in your channel automatically.

2. **RSS/Atom feeds** — the bot regularly checks RSS feeds (news sites, GitHub
   release pages, blogs…) and announces new items in your channel.

Everything is configured by typing commands to the bot in IRC — you never
have to edit a file again after the initial setup.

---

## Step 1 — Install Python

You need Python 3.9 or newer.

**Linux / macOS**
```bash
python3 --version   # check if you already have it
```
If the version shown is below 3.9, install via your package manager:
```bash
# Debian / Ubuntu
sudo apt install python3 python3-pip

# Fedora
sudo dnf install python3 python3-pip

# macOS with Homebrew
brew install python
```

**Windows** — download the installer from https://python.org/downloads and tick
"Add Python to PATH" during installation.

---

## Step 2 — Download and unpack Gitbot

Unzip the file you downloaded.  You should see a folder called `gitbot/`.

```
gitbot/
  bot.py
  config.example.json
  requirements.txt
  README.md
  README-dummies.md   ← you are reading this
  src/
  modules/
```

Open a terminal and go into that folder:
```bash
cd gitbot
```

---

## Step 3 — Create a virtual environment and install dependencies

Modern Linux distros (Debian 12+, Ubuntu 23.04+, Fedora 38+, and others) block
`pip install` system-wide on purpose — you need a **virtual environment** (venv)
instead.  A venv is just a self-contained folder that holds the bot's libraries
without touching anything else on your system.

**Create the venv once** (do this from inside the `gitbot/` folder):

```bash
python3 -m venv ~/virtualenv
```

This creates `~/virtualenv/` in your home directory.

**Activate it** (you need to do this every time you open a new terminal):

```bash
source ~/virtualenv/bin/activate
```

Your prompt will change to show `(virtualenv)` at the start — that means it's active.

**Install the dependencies** (only needed once, after creating the venv):

```bash
pip install -r requirements.txt
```

This installs two small libraries (`aiohttp` and `feedparser`).  Takes about 10 seconds.

> **Tip:** if you ever see `externally-managed-environment` or
> `error: no module named aiohttp` when starting the bot, it means the venv
> isn't active.  Run `source ~/virtualenv/bin/activate` and try again.

---

## Step 4 — Create your config file

Copy the example:
```bash
cp config.example.json config.json
```

Now open `config.json` in any text editor.  Here is what the important parts mean:

```jsonc
{
  "networks": [
    {
      "name": "libera",            // A short nickname YOU choose for this network.
                                   // You will use this name in bot commands later.

      "host": "irc.libera.chat",  // The server address.
      "port": 6697,               // 6697 = TLS (encrypted). 6667 = plain text.
      "tls":  true,               // true = encrypted connection (recommended).

      "nick": "mybot",            // The nickname the bot will use on IRC.
      "username": "mybot",        // Usually the same as nick.
      "realname": "Gitbot",   // Shown in /whois. Can be anything.

      "password": "",             // Server password (rare). Leave empty if none.
      "sasl_password": "",        // NickServ SASL password. See "Registering the
                                  // bot's nick" below. Leave empty if not needed.

      "channels": ["#myproject"], // Channels to join automatically on connect.
                                  // Add as many as you like: ["#foo", "#bar"]

      "command_prefix": "!",      // Commands start with this character in channels.

      "admins": [                 // Who is allowed to run admin commands.
        "yournick!*@*"            // This pattern matches yournick from any host.
      ]
    }
  ],

  "webhook": {
    "host": "127.0.0.1",  // Where the webhook HTTP server listens.
                           // Use 127.0.0.1 if you put nginx/caddy in front.
                           // Use 0.0.0.0 to listen on all interfaces directly.
    "port": 8765,         // The port number.
    "secret": ""          // Optional security token. See the webhook section below.
  },

  "db_path": "data/gitbot.db",  // Where to store settings. Leave as-is.
  "rss_interval": 300               // How often to check RSS feeds (in seconds).
}
```

---

## Step 5 — Add a second (or third) network

Each entry in the `"networks"` list is one IRC server.  
To connect to multiple networks at once, just add more entries:

```json
"networks": [
  {
    "name": "libera",
    "host": "irc.libera.chat",
    "port": 6697,
    "tls":  true,
    "nick": "mybot",
    "username": "mybot",
    "realname": "Gitbot",
    "password": "",
    "sasl_password": "",
    "channels": ["#myproject"],
    "command_prefix": "!",
    "admins": ["yournick!*@*"]
  },
  {
    "name": "oftc",
    "host": "irc.oftc.net",
    "port": 6697,
    "tls":  true,
    "nick": "mybot",
    "username": "mybot",
    "realname": "Gitbot",
    "password": "",
    "sasl_password": "",
    "channels": ["#myproject-dev"],
    "command_prefix": "!",
    "admins": ["yournick!*@*"]
  }
]
```

The bot connects to all of them simultaneously.  
Settings (webhooks, RSS feeds) are stored per-network/channel, so the same
webhook can announce to `#myproject` on Libera **and** `#myproject-dev` on
OFTC at the same time.

**Common IRC server addresses**

| Network | Address | Port |
|---|---|---|
| Libera.Chat | irc.libera.chat | 6697 (TLS) |
| OFTC | irc.oftc.net | 6697 (TLS) |
| IRCnet | open.ircnet.net | 6697 (TLS) |
| EFnet | irc.efnet.org | 6697 (TLS) |
| freenode (historical) | irc.freenode.net | 6697 (TLS) |
| Your own (Ergo/InspIRCd/…) | whatever you set | 6697 (TLS) |

---

## Step 6 — Registering the bot's nick (optional but recommended)

Most public IRC networks let you register a nickname with NickServ so nobody
else can use it.

1. Connect with your own IRC client under the nick you want the bot to use.
2. Type:  `/msg NickServ REGISTER <password> <email>`
3. Follow the confirmation email instructions.
4. Put that password in `"sasl_password"` in config.json.  The bot will
   authenticate automatically on connect.

---

## Step 7 — Start the bot

Make sure the venv is active first (`source ~/virtualenv/bin/activate`), then:

```bash
~/virtualenv/bin/python3 bot.py config.json
```

Using the full venv path (`~/virtualenv/bin/python3`) means it works even if
you forget to activate the venv first — handy for scripts and service files.

You should see output like:
```
2024-01-15 12:00:00 [INFO] gitbot: ...
2024-01-15 12:00:01 [INFO] src.network: [libera] Connecting to irc.libera.chat:6697 (TLS=True)
2024-01-15 12:00:02 [INFO] src.network: [libera] Registered as mybot
2024-01-15 12:00:02 [INFO] src.network: [libera] Joined #myproject
2024-01-15 12:00:02 [INFO] modules.webhooks: Webhook server listening on 127.0.0.1:8765
```

Press `Ctrl+C` to stop.

**Running it in the background (Linux)**

```bash
nohup ~/virtualenv/bin/python3 bot.py config.json > bot.log 2>&1 &
echo $! > bot.pid      # save the process ID so you can kill it later
```

To stop it later:  `kill $(cat bot.pid)`

For a proper always-on setup, see the "Running as a service" section at the end.

---

## Step 8 — Who counts as an admin?

The `"admins"` list in config.json contains **glob patterns** matched against
a user's full `nick!user@host` string.

```
yournick!*@*           matches yournick from any user/host (simplest)
*!*@trusted.isp.net    matches anyone connecting from trusted.isp.net
alice!alice@192.168.*  matches alice from any 192.168.x.x address
```

You can find your own `nick!user@host` by typing `/whois yournick` in your
IRC client and looking at the "is" line.

---

## Step 9 — Using commands

### In a channel

Type commands with the prefix (default `!`):
```
!webhook list
!rss announce list
```

### Via private message

Send commands directly to the bot (no prefix needed, or you can use `!`):
```
/msg mybot webhook list #myproject
/msg mybot rss announce add #myproject mynews
```

When messaging the bot directly, you need to tell it **which channel** you
mean — add `#channel` right after the subcommand:
```
/msg mybot webhook add #myproject github owner/repo
/msg mybot rss announce add #myproject limnoria
/msg mybot rss format #myproject [$feed_name] $title — $link
```

---

## Setting up Git webhooks

### What is a webhook?

When you push code (or open a PR, etc.), GitHub/Gitea/GitLab can send an
HTTP POST request to a URL you specify.  Gitbot receives that request
and announces it in your IRC channel.

### Step A — Make the bot reachable from the internet

The webhook server listens on `localhost:8765` by default.  GitHub needs to
be able to reach it.

**Option 1 — nginx reverse proxy (recommended)**

```nginx
server {
    listen 443 ssl;
    server_name mybot.example.com;
    # ... your SSL cert config ...

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Then use `https://mybot.example.com/github` as your webhook URL.

**Option 2 — expose directly**

Change `"host": "0.0.0.0"` in config.json and open port 8765 in your firewall.
Then use `http://YOUR_SERVER_IP:8765/github`.

> ⚠️  Without HTTPS, webhook payloads are sent in plain text.
> For production use, set up a reverse proxy with TLS.

### Step B — Register the hook in the channel

In IRC, tell it which platform the repo lives on (`github`, `gitea`, or `gitlab`):
```
!webhook add github owner/repo
```

For example:
```
!webhook add github myorg/myrepo  ← specific repo on GitHub
!webhook add github myorg         ← all repos from this user/org on GitHub
!webhook add gitea myorg/myrepo   ← same repo name, but on your Gitea instance
```

The platform is tracked separately, so `github myorg/myrepo` and
`gitea myorg/myrepo` are two independent hooks — handy if you mirror
a repo across platforms.

### Step C — Add the webhook in GitHub / Gitea / GitLab

**GitHub**
1. Go to your repo → Settings → Webhooks → Add webhook
2. Payload URL: `https://mybot.example.com/github`
3. Content type: `application/json`
4. Secret: put something random here and also set `"secret"` in config.json
5. Choose which events to send (or "Send me everything")
6. Click Add webhook

**Gitea**
1. Repo → Settings → Webhooks → Add Webhook → Gitea
2. Target URL: `https://mybot.example.com/gitea`
3. Secret: same as above
4. Trigger on: everything you want

**GitLab**
1. Repo → Settings → Webhooks
2. URL: `https://mybot.example.com/gitlab`
3. Secret token: same value as `"secret"` in config.json
4. Check the events you want
5. Add webhook

### Step D — Filter what gets announced (optional)

By default all categories are announced.  You can restrict them:

```
!webhook events github myorg/myrepo code pr
```

Or filter to a specific branch only:
```
!webhook branches github myorg/myrepo main
```

Available event categories:

| Category | What it covers |
|---|---|
| `ping` | Confirmation when webhook is first set up |
| `code` | Pushes and commit comments |
| `pr-minimal` | PR opened / closed / reopened |
| `pr` | All common PR activity |
| `issue-minimal` | Issue opened / closed / reopened |
| `issue` | All common issue activity |
| `repo` | Creates, deletes, releases, forks |
| `star` | Stars (GitHub only) |

---

## Setting up RSS feeds

### Step A — Register a named feed (optional)

You can give a feed a short name so you don't have to type the URL every time:

```
!rss add limnoria https://github.com/progval/Limnoria/releases.atom
!rss add hackernews https://news.ycombinator.com/rss
```

### Step B — Announce it in a channel

```
!rss announce add limnoria
!rss announce add hackernews
```

Or use a URL directly without registering it first:
```
!rss announce add https://blog.example.com/feed.xml
```

The bot will immediately fetch the feed, mark all existing entries as already
seen (so you don't get flooded), and from then on announce only new items.

### Step C — Reading feeds on demand

```
!rss read limnoria        ← latest 3 entries
!rss read limnoria 5      ← latest 5 entries
!rss read               ← first announced feed in this channel
```

### Step D — Customising the announcement format

```
!rss format [$feed_name] $title — $link
!rss format New post on $feed_title: "$title" by $author — $link ($date)
```

Available variables:

| Variable | Meaning |
|---|---|
| `$feed_name` | The short name you gave it, or the URL |
| `$feed_title` | The title from the feed itself |
| `$title` | Entry headline |
| `$link` | Entry URL |
| `$author` | Entry author |
| `$date` | Published date (YYYY-MM-DD HH:MM) |
| `$description` | Entry summary / excerpt |

---

## Quick command reference

### Webhook commands  (admin only)

| Command | What it does |
|---|---|
| `!webhook list` | List hooks registered in this channel |
| `!webhook add github\|gitea\|gitlab owner/repo` | Register a new hook |
| `!webhook remove github\|gitea\|gitlab owner/repo` | Remove a hook |
| `!webhook events github\|gitea\|gitlab hook [cats…]` | Show or set event category filter |
| `!webhook branches github\|gitea\|gitlab hook [br…]` | Show or set branch filter (empty = all) |
| `!webhook show github\|gitea\|gitlab hook` | Show full config for a hook |
| `!webhook settings` | Show display settings |
| `!webhook settings git-prevent-highlight true` | Prevent bot from pinging nicks |
| `!webhook settings git-hide-prefix true` | Hide the `[git]` prefix |

### RSS commands

| Command | Who | What it does |
|---|---|---|
| `!rss list` | anyone | List named feeds |
| `!rss add name url` | admin | Register a named feed |
| `!rss remove name` | admin | Remove a named feed |
| `!rss announce list` | anyone | List feeds announced here |
| `!rss announce add name` | admin | Start announcing in this channel |
| `!rss announce remove name` | admin | Stop announcing |
| `!rss read [name] [n]` | anyone | Read latest n entries |
| `!rss info name` | anyone | Show feed metadata |
| `!rss format [template]` | admin | Show or set announcement format |
| `!rss interval [seconds]` | admin | Show or set poll interval |

### Admin commands  (admin only, work in channel and via PM)

| Command | What it does |
|---|---|
| `!join #channel` | Make the bot join a channel |
| `!part [#channel]` | Make the bot leave a channel |
| `!say target message` | Make the bot say something |
| `!raw irc line` | Send a raw IRC line |
| `!networks` | List all connected networks |
| `!quit [reason]` | Disconnect and exit |
| `!reload` | Reload config.json from disk |

---

## Running as a service (Linux, systemd)

Create `/etc/systemd/system/gitbot.service`:

```ini
[Unit]
Description=Gitbot IRC bot
After=network.target

[Service]
Type=simple
User=yourusername
WorkingDirectory=/path/to/gitbot
ExecStart=/home/yourusername/virtualenv/bin/python3 bot.py config.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Replace `yourusername` and `/path/to/gitbot` with your actual values.  
The venv path (`/home/yourusername/virtualenv/bin/python3`) points directly
into the virtual environment — no `source activate` needed in a service file.

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable gitbot
sudo systemctl start gitbot
sudo systemctl status gitbot     # check it's running
journalctl -u gitbot -f          # follow the logs
```

---

## Troubleshooting

**The bot connects but immediately disconnects**
→ Check that the nick isn't already taken on the server.  
→ If registration is required, set `sasl_password`.

**Commands don't work**
→ Make sure your `nick!user@host` matches one of the patterns in `"admins"`.  
→ You can find your hostmask with `/whois yournick` in your IRC client.  
→ Check you are using the right `command_prefix` (default `!`).

**Webhooks arrive but nothing is announced**
→ Run `!webhook list` to check the hook is registered.  
→ Check that the event category filter includes what you sent
   (run `!webhook show github|gitea|gitlab owner/repo`).  
→ Check the bot's console output for errors.

**RSS feeds aren't being announced**
→ Run `!rss announce list` — is the feed listed?  
→ Run `!rss read feedname` to check the feed is reachable.  
→ The poll interval is 5 minutes by default; wait for a new item to appear.

**I get "Error: You must be an admin to use !webhook"**
→ Your hostmask doesn't match the admins list.  Try a looser pattern like
  `yournick!*@*` while testing, then tighten it later.
