"""
Async IRC network connection.

Each Network object manages one TCP connection to one IRC server.
It handles:
  - connecting / TLS
  - nick registration + optional SASL PLAIN
  - auto-join
  - line reading / writing with flood throttle
  - dispatching incoming lines to the command handler
  - reconnection with backoff
"""

import asyncio
import base64
import logging
import ssl
import time
import typing

from src.config import NetworkConfig

log = logging.getLogger(__name__)

# Maximum lines per burst before throttling
FLOOD_BURST = 5
FLOOD_DELAY = 1.2   # seconds between lines after burst


class Network:
    def __init__(self, config: NetworkConfig, bot: "Bot"):  # type: ignore
        self.config = config
        self.bot = bot
        self.name = config.name

        self.channels: typing.Set[str] = set()
        self.channel_members: typing.Dict[str, typing.Set[str]] = {}
        # Stores the raw mode string for each channel (lowercased name → mode string).
        # e.g. "#test" → "+nrtc"
        self.channel_modes: typing.Dict[str, str] = {}
        # Nicks that have authenticated with !auth this session (in-memory only).
        # Cleared when the nick quits or the bot reconnects.
        self.authed_nicks: typing.Set[str] = set()

        self._writer: typing.Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._registered = False

        # flood control
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._last_send = 0.0
        self._burst_count = 0

    # ---------------------------------------------------------------- lifecycle

    async def run(self):
        backoff = 5
        while True:
            try:
                await self._connect()
                backoff = 5
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("[%s] Disconnected: %s – reconnecting in %ds",
                            self.name, exc, backoff)
                self._connected = False
                self._registered = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _connect(self):
        log.info("[%s] Connecting to %s:%d (TLS=%s)",
                 self.name, self.config.host, self.config.port, self.config.tls)

        if self.config.tls:
            ctx = ssl.create_default_context()
            reader, writer = await asyncio.open_connection(
                self.config.host, self.config.port, ssl=ctx)
        else:
            reader, writer = await asyncio.open_connection(
                self.config.host, self.config.port)

        self._writer = writer
        self._connected = True
        self.authed_nicks.clear()  # sessions don't survive reconnects

        # start write-loop
        asyncio.ensure_future(self._write_loop())

        # Registration
        if self.config.sasl_password:
            self._raw("CAP REQ :sasl")
        if self.config.password:
            self._raw(f"PASS :{self.config.password}")
        self._raw(f"NICK {self.config.nick}")
        self._raw(f"USER {self.config.username} 0 * :{self.config.realname}")

        # Read loop
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("Server closed connection")
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            await self._handle_line(text)

    # ----------------------------------------------------------------- sending

    def _raw(self, line: str):
        """Enqueue a raw IRC line."""
        self._send_queue.put_nowait(line)

    async def _write_loop(self):
        while self._connected:
            try:
                line = await asyncio.wait_for(self._send_queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue

            # flood throttle
            now = time.monotonic()
            if self._burst_count >= FLOOD_BURST:
                sleep = FLOOD_DELAY - (now - self._last_send)
                if sleep > 0:
                    await asyncio.sleep(sleep)
                self._burst_count = 0
            self._burst_count += 1
            self._last_send = time.monotonic()

            if self._writer and not self._writer.is_closing():
                self._writer.write((line + "\r\n").encode("utf-8"))
                try:
                    await self._writer.drain()
                except Exception:
                    break

    def send_privmsg(self, target: str, text: str):
        self._raw(f"PRIVMSG {target} :{text}")

    def send_notice(self, target: str, text: str):
        self._raw(f"NOTICE {target} :{text}")

    # ----------------------------------------------------------- line handling

    async def _handle_line(self, line: str):
        log.debug("[%s] << %s", self.name, line)
        parts = line.split(" ")

        # PING
        if parts[0] == "PING":
            self._raw(f"PONG {parts[1]}")
            return

        # Numeric 001 – welcome
        if len(parts) >= 2 and parts[1] == "001":
            self._registered = True
            log.info("[%s] Registered as %s", self.name, self.config.nick)
            self._on_registered()
            return

        # CAP handling for SASL
        if len(parts) >= 4 and parts[1] == "CAP":
            await self._handle_cap(parts)
            return

        # AUTHENTICATE
        if parts[0] == "AUTHENTICATE":
            if parts[1] == "+":
                creds = f"{self.config.nick}\x00{self.config.nick}\x00{self.config.sasl_password}"
                encoded = base64.b64encode(creds.encode()).decode()
                self._raw(f"AUTHENTICATE {encoded}")
            return

        # 903 SASL success
        if len(parts) >= 2 and parts[1] == "903":
            self._raw("CAP END")
            return

        # JOIN
        if len(parts) >= 3 and parts[1] == "JOIN":
            channel = parts[2].lstrip(":")
            nick = parts[0].split("!")[0].lstrip(":")
            if nick == self.config.nick:
                self.channels.add(channel.lower())
                log.info("[%s] Joined %s", self.name, channel)
                # Ask for the current channel modes so we know about +c etc.
                self._raw(f"MODE {channel}")
            self.channel_members.setdefault(channel.lower(), set()).add(nick)
            return

        # PART / KICK
        if len(parts) >= 3 and parts[1] in ("PART", "KICK"):
            channel = parts[2].lstrip(":")
            nick = parts[0].split("!")[0].lstrip(":")
            target_nick = parts[3] if parts[1] == "KICK" else nick
            self.channel_members.get(channel.lower(), set()).discard(target_nick)
            if target_nick == self.config.nick:
                self.channels.discard(channel.lower())
            return

        # QUIT
        if len(parts) >= 1 and parts[1] == "QUIT":
            nick = parts[0].split("!")[0].lstrip(":")
            for members in self.channel_members.values():
                members.discard(nick)
            self.authed_nicks.discard(nick)
            return

        # 353 NAMES list
        if len(parts) >= 6 and parts[1] == "353":
            channel = parts[4].lower()
            # First token may have a leading : from the IRC framing.
            # After that, strip all possible status prefixes (~&@%+! and
            # similar) to get the bare nick regardless of server prefix set.
            raw_nicks = parts[5:]
            raw_nicks[0] = raw_nicks[0].lstrip(":")
            nicks = [n.lstrip("~&@%+!") for n in raw_nicks]
            self.channel_members.setdefault(channel, set()).update(nicks)
            return

        # MODE – track channel mode changes
        # :server MODE #chan +c   or   :nick!u@h MODE #chan -c
        if len(parts) >= 4 and parts[1] == "MODE":
            target = parts[2]
            if target.startswith(("#", "&", "+", "!")):
                modestr = parts[3].lstrip(":")
                self._apply_channel_modes(target.lower(), modestr)
            return

        # 324 – mode reply sent when we join or ask with MODE #chan
        # :server 324 botnick #chan +nrtc
        if len(parts) >= 5 and parts[1] == "324":
            channel = parts[3].lower()
            modestr = parts[4].lstrip(":")
            self.channel_modes[channel] = modestr
            return

        # PRIVMSG – commands
        if len(parts) >= 4 and parts[1] == "PRIVMSG":
            await self._handle_privmsg(parts, line)
            return

    async def _handle_cap(self, parts):
        sub = parts[3]
        if sub == "ACK":
            caps = parts[4].lstrip(":").split()
            if "sasl" in caps:
                self._raw("AUTHENTICATE PLAIN")
        elif sub == "NAK":
            self._raw("CAP END")

    def _on_registered(self):
        for channel in self.config.channels:
            self._raw(f"JOIN {channel}")

    def _apply_channel_modes(self, channel: str, modestr: str):
        """Apply a +/- mode change string to the stored channel mode set."""
        current = set(self.channel_modes.get(channel, "").lstrip("+"))
        adding = True
        for ch in modestr:
            if ch == "+":
                adding = True
            elif ch == "-":
                adding = False
            elif ch.isalpha():
                if adding:
                    current.add(ch)
                else:
                    current.discard(ch)
            # Ignore everything else (digits, brackets, commas — flood params etc.)
        self.channel_modes[channel] = "+" + "".join(sorted(current))

    def has_channel_mode(self, channel: str, flag: str) -> bool:
        """Return True if *flag* is set on *channel*.

        The mode string is always:  +letters [param1 param2 ...]
        Everything after the first space is a parameter and can be ignored —
        that covers +f flood strings, +k keys, +l limits, etc.
        We only inspect the first token (the letters themselves).
        """
        modestr = self.channel_modes.get(channel.lower(), "")
        letters = modestr.split()[0] if modestr else ""
        return flag in letters

    async def _handle_privmsg(self, parts: list, raw_line: str):
        prefix = parts[0].lstrip(":")           # nick!user@host
        nick = prefix.split("!")[0]
        hostmask = prefix
        target = parts[2]                        # channel or our nick
        msg = " ".join(parts[3:]).lstrip(":")

        pfx = self.config.command_prefix

        # Detect whether this is a PM (target is the bot's own nick).
        # Channel names always start with # & + ! on IRC servers.
        is_pm = not target.startswith(("#", "&", "+", "!"))

        if is_pm:
            # In PMs the prefix is required, same as in channels.
            if not msg.startswith(pfx):
                return
            msg = msg[len(pfx):]
        else:
            # In channels, require the command prefix.
            if not msg.startswith(pfx):
                return
            msg = msg[len(pfx):]

        cmd_parts = msg.split()
        if not cmd_parts:
            return

        command = cmd_parts[0].lower()
        args = cmd_parts[1:]

        await self.bot.dispatch_command(
            network=self,
            hostmask=hostmask,
            nick=nick,
            target=target,
            command=command,
            args=args,
            is_pm=is_pm,
        )
