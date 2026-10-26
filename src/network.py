"""
Async IRC network connection.

Each Network object manages one TCP connection to one IRC server.
It handles:
  - connecting / TLS
  - nick registration (with a fallback nick if ours is taken) + optional
    SASL PLAIN
  - auto-join
  - line reading / writing with flood throttle
  - ping timeout detection
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

if typing.TYPE_CHECKING:
    from src.bot import Bot

log = logging.getLogger(__name__)

# Flood control: a burst of FLOOD_BURST lines, then one line every
# FLOOD_DELAY seconds until the bucket refills.
FLOOD_BURST = 5
FLOOD_DELAY = 1.2   # seconds between lines after burst

# RFC 2812: a full line, including the leading ":prefix " the server
# prepends and the trailing CRLF, must not exceed 512 bytes.
MAX_LINE_BYTES = 512
# Longest hostname component the server may hand us in a hostmask, used
# as a conservative fallback until we've actually seen our own hostmask.
MAX_HOSTNAME_BYTES = 63

CONNECT_TIMEOUT = 30    # seconds to establish TCP/TLS
READ_TIMEOUT = 180      # silence before we PING the server ourselves
PING_TIMEOUT = 60       # then how long we wait for any reply
NICK_REGAIN_EVERY = 300 # seconds between attempts to get our nick back

# Messages kept while disconnected, sent once we're registered again.
MAX_PENDING = 200

# SASL failure numerics: ERR_NICKLOCKED, ERR_SASLFAIL, ERR_SASLTOOLONG,
# ERR_SASLABORTED, ERR_SASLALREADY, RPL_SASLMECHS.
SASL_FAILURES = ("902", "904", "905", "906", "907", "908")
# Nick unusable during registration: ERR_ERRONEUSNICKNAME,
# ERR_NICKNAMEINUSE, ERR_NICKCOLLISION, ERR_UNAVAILRESOURCE.
NICK_REJECTED = ("432", "433", "436", "437")


def _split_by_bytes(text: str, max_bytes: int) -> typing.List[str]:
    """Split text into chunks of at most max_bytes UTF-8 bytes each,
    never cutting a multi-byte character in half."""
    if max_bytes <= 0:
        max_bytes = 1
    encoded = text.encode("utf-8")
    n = len(encoded)
    if n <= max_bytes:
        return [text]

    chunks = []
    start = 0
    while start < n:
        end = min(start + max_bytes, n)
        # Back off if we landed inside a multi-byte UTF-8 sequence —
        # continuation bytes have the top two bits set to 10.
        while end > start and end < n and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end].decode("utf-8"))
        start = end
    return chunks


class FloodBucket:
    """Token bucket: up to `burst` lines at once, then one every `delay`
    seconds. Tokens come back over time, so a quiet bot can burst again,
    but a busy one is held to the steady rate however long it stays busy.
    """

    def __init__(self, burst: int = FLOOD_BURST, delay: float = FLOOD_DELAY,
                 clock=time.monotonic):
        self.burst = burst
        self.delay = delay
        self.clock = clock
        self.tokens = float(burst)
        self.last = clock()

    def wait_time(self) -> float:
        """Take a token; return how long to sleep before sending."""
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) / self.delay)
        self.last = now
        self.tokens -= 1
        if self.tokens >= 0:
            return 0.0
        return -self.tokens * self.delay


def _nick_of(prefix: str) -> str:
    return prefix.lstrip(":").split("!", 1)[0]


class Network:
    def __init__(self, config: NetworkConfig, bot: "Bot"):
        self.config = config
        self.bot = bot
        self.name = config.name

        # Our current nick; differs from config.nick while that one is taken.
        self.nick: str = config.nick

        self.channels: typing.Set[str] = set()
        self.channel_members: typing.Dict[str, typing.Set[str]] = {}
        # Stores the raw mode string for each channel (lowercased name → mode string).
        # e.g. "#test" → "+nrtc"
        self.channel_modes: typing.Dict[str, str] = {}
        # Full nick!user@host of users who authenticated with auth this
        # session (in-memory only). Kept up to date on NICK, dropped on QUIT
        # and on reconnect. Matching the whole hostmask means someone who
        # takes an admin's old nick is not an admin.
        self.authed: typing.Set[str] = set()

        self._writer: typing.Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._registered = False

        # Our own nick!user@host, learned from our own JOIN echo. Used to
        # size outgoing PRIVMSG/NOTICE splits correctly — see send_privmsg().
        self._own_hostmask: str = ""

        # One queue and writer task per connection, so nothing from an old
        # connection leaks into the next one.
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._write_task: typing.Optional[asyncio.Task] = None
        # PRIVMSG/NOTICE sent while not registered, flushed after 001.
        self._pending: typing.List[str] = []
        self._last_nick_try = 0.0
        self._nick_tries = 0

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
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _connect(self):
        log.info("[%s] Connecting to %s:%d (TLS=%s%s)",
                 self.name, self.config.host, self.config.port, self.config.tls,
                 ", bind=%s" % self.config.bind if self.config.bind else "")

        local_addr = (self.config.bind, 0) if self.config.bind else None
        ssl_ctx = ssl.create_default_context() if self.config.tls else None

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.config.host, self.config.port,
                                    ssl=ssl_ctx, local_addr=local_addr),
            timeout=CONNECT_TIMEOUT)

        self._writer = writer
        self._connected = True
        self._registered = False
        self.nick = self.config.nick
        self._nick_tries = 0
        self.authed.clear()       # sessions don't survive reconnects
        self._own_hostmask = ""   # re-learned from our next JOIN echo
        self.channels.clear()
        self.channel_members.clear()
        self.channel_modes.clear()

        self._send_queue = asyncio.Queue()
        self._write_task = asyncio.ensure_future(
            self._write_loop(writer, self._send_queue))

        try:
            # Registration
            if self.config.sasl_password:
                self._raw("CAP REQ :sasl")
            if self.config.password:
                self._raw(f"PASS :{self.config.password}")
            self._raw(f"NICK {self.nick}")
            self._raw(f"USER {self.config.username} 0 * :{self.config.realname}")

            await self._read_loop(reader)
        finally:
            # Always close this connection before a new one is made, or the
            # old session keeps our nick until the server pings it out.
            self._connected = False
            self._registered = False
            if self._write_task:
                self._write_task.cancel()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=5)
            except Exception:
                pass

    async def _read_loop(self, reader: asyncio.StreamReader):
        pinged = False
        while True:
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=PING_TIMEOUT if pinged else READ_TIMEOUT)
            except asyncio.TimeoutError:
                if pinged:
                    raise ConnectionError("Ping timeout")
                # Quiet for a while: make sure the server is still there.
                self._raw("PING :gitbot")
                pinged = True
                continue
            if not line:
                raise ConnectionError("Server closed connection")
            pinged = False
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text:
                continue
            try:
                await self._handle_line(text)
            except Exception:
                # A line we don't understand must not drop the connection.
                log.exception("[%s] Error handling line: %r", self.name, text)
            self.try_regain_nick()

    # ----------------------------------------------------------------- sending

    def _raw(self, line: str):
        """Enqueue a raw IRC line."""
        self._send_queue.put_nowait(line)

    def _send_text(self, line: str):
        """Enqueue a PRIVMSG/NOTICE, or keep it until we're registered
        (sent before registration it would only be refused)."""
        if self._registered:
            self._raw(line)
        else:
            self._pending.append(line)
            if len(self._pending) > MAX_PENDING:
                del self._pending[0]

    async def _write_loop(self, writer: asyncio.StreamWriter, queue: asyncio.Queue):
        bucket = FloodBucket()
        while not writer.is_closing():
            line = await queue.get()
            sleep = bucket.wait_time()
            if sleep > 0:
                await asyncio.sleep(sleep)
            if writer.is_closing():
                break
            writer.write((line + "\r\n").encode("utf-8"))
            try:
                await writer.drain()
            except Exception:
                break

    def _max_text_bytes(self, command: str, target: str) -> int:
        """Budget for the trailing text of an outgoing PRIVMSG/NOTICE.

        The 512-byte RFC 2812 line limit applies to the line as the server
        relays it — i.e. INCLUDING the ":nick!user@host " prefix the server
        prepends, which we never send ourselves. We estimate it using our
        own hostmask once we've learned it from a JOIN echo; until then we
        fall back to a conservative worst-case estimate.
        """
        own = self._own_hostmask or (
            f"{self.nick}!{self.config.username}@"
            + "x" * MAX_HOSTNAME_BYTES
        )
        frame = f":{own} {command} {target} :\r\n"
        return MAX_LINE_BYTES - len(frame.encode("utf-8"))

    def send_privmsg(self, target: str, text: str):
        limit = self._max_text_bytes("PRIVMSG", target)
        for chunk in _split_by_bytes(text, limit):
            self._send_text(f"PRIVMSG {target} :{chunk}")

    def send_notice(self, target: str, text: str):
        limit = self._max_text_bytes("NOTICE", target)
        for chunk in _split_by_bytes(text, limit):
            self._send_text(f"NOTICE {target} :{chunk}")

    def _learn_own_hostmask(self, parts: list):
        """Update _own_hostmask from a line's source prefix, if it's us.

        Catches JOIN, self MODE, self PART, KICK of us, etc. — anything
        with a full "nick!user@host" prefix matching our current nick.
        Doesn't catch CHGHOST (the new host isn't in the prefix) or the
        396 cloak-assignment numeric (no "!user@host" prefix at all);
        those are handled explicitly in _handle_line.
        """
        prefix = parts[0]
        if not prefix.startswith(":") or "!" not in prefix:
            return
        prefix = prefix[1:]
        nick = prefix.split("!", 1)[0]
        if nick == self.nick:
            self._own_hostmask = prefix

    # ------------------------------------------------------------ admin auth

    def is_authed(self, hostmask: str) -> bool:
        return hostmask in self.authed

    def deauth(self, nick: str):
        self.authed = {h for h in self.authed if _nick_of(h) != nick}

    # ----------------------------------------------------------- line handling

    async def _handle_line(self, line: str):
        log.debug("[%s] << %s", self.name, line)
        parts = line.split(" ")
        command = parts[1] if len(parts) >= 2 else ""

        # Keep _own_hostmask fresh from *any* line the server attributes to
        # us — cloaks/vhosts can be assigned or changed well after our
        # initial JOIN (delayed cloak-on-connect, HostServ/oper vhost
        # changes mid-session), so a one-time JOIN snapshot goes stale.
        self._learn_own_hostmask(parts)

        # PING
        if parts[0] == "PING":
            self._raw("PONG %s" % (parts[1] if len(parts) >= 2 else ":gitbot"))
            return

        # Numeric 001 – welcome
        if command == "001":
            self._registered = True
            if len(parts) >= 3:
                self.nick = parts[2]
            log.info("[%s] Registered as %s", self.name, self.nick)
            self._on_registered()
            return

        # Nick taken or refused while registering: try another one.
        if command in NICK_REJECTED and not self._registered:
            self._next_nick()
            return

        # CAP handling for SASL
        if len(parts) >= 4 and command == "CAP":
            await self._handle_cap(parts)
            return

        # AUTHENTICATE
        if parts[0] == "AUTHENTICATE":
            if len(parts) >= 2 and parts[1] == "+":
                account = self.config.sasl_username or self.config.nick
                creds = f"{account}\x00{account}\x00{self.config.sasl_password}"
                encoded = base64.b64encode(creds.encode()).decode()
                self._raw(f"AUTHENTICATE {encoded}")
            return

        # 903 SASL success
        if command == "903":
            self._raw("CAP END")
            return

        # SASL failed: carry on without it rather than hang in registration.
        if command in SASL_FAILURES:
            log.error("[%s] SASL authentication failed: %s",
                      self.name, " ".join(parts[3:]).lstrip(":"))
            if not self._registered:
                self._raw("CAP END")
            return

        # 396 – RPL_HOSTHIDDEN, sent when a cloak/vhost is (re-)assigned.
        # No "!user@host" prefix here, only "nick new.host", so this can't
        # be picked up by _learn_own_hostmask — handle it explicitly. The
        # username isn't given, so keep whatever we last knew (or config
        # default before we've ever learned one).
        if len(parts) >= 4 and command == "396":
            nick = parts[2]
            new_host = parts[3]
            if nick == self.nick:
                user = self.config.username
                if self._own_hostmask and "@" in self._own_hostmask:
                    user = self._own_hostmask.split("!", 1)[1].split("@", 1)[0]
                self._own_hostmask = f"{nick}!{user}@{new_host}"
            return

        # CHGHOST – user/host changed mid-session (HostServ, oper vhost,
        # etc.). The prefix still carries the OLD host; the new user/host
        # are the command parameters, so handle it explicitly too.
        if len(parts) >= 4 and command == "CHGHOST":
            old_nick = _nick_of(parts[0])
            if old_nick == self.nick:
                new_user = parts[2]
                new_host = parts[3].lstrip(":")
                self._own_hostmask = f"{old_nick}!{new_user}@{new_host}"
            return

        # NICK – someone (maybe us) changed nick
        if len(parts) >= 3 and command == "NICK":
            self._handle_nick(parts[0].lstrip(":"), parts[2].lstrip(":"))
            return

        # JOIN
        if len(parts) >= 3 and command == "JOIN":
            channel = parts[2].lstrip(":")
            nick = _nick_of(parts[0])
            if nick == self.nick:
                self.channels.add(channel.lower())
                log.info("[%s] Joined %s", self.name, channel)
                # Ask for the current channel modes so we know about +c etc.
                self._raw(f"MODE {channel}")
            self.channel_members.setdefault(channel.lower(), set()).add(nick)
            return

        # PART / KICK
        if len(parts) >= 3 and command in ("PART", "KICK"):
            channel = parts[2].lstrip(":")
            nick = _nick_of(parts[0])
            target_nick = parts[3] if command == "KICK" and len(parts) >= 4 else nick
            self.channel_members.get(channel.lower(), set()).discard(target_nick)
            if target_nick == self.nick:
                self.channels.discard(channel.lower())
                self.channel_members.pop(channel.lower(), None)
            return

        # QUIT
        if command == "QUIT":
            nick = _nick_of(parts[0])
            for members in self.channel_members.values():
                members.discard(nick)
            self.deauth(nick)
            if nick == self.config.nick and self.nick != self.config.nick:
                self._raw(f"NICK {self.config.nick}")   # our nick is free
            return

        # 353 NAMES list
        if len(parts) >= 6 and command == "353":
            channel = parts[4].lower()
            # First token may have a leading : from the IRC framing.
            # After that, strip all possible status prefixes (~&@%+! and
            # similar) to get the bare nick regardless of server prefix set.
            raw_nicks = parts[5:]
            raw_nicks[0] = raw_nicks[0].lstrip(":")
            nicks = [n.lstrip("~&@%+!") for n in raw_nicks if n]
            self.channel_members.setdefault(channel, set()).update(nicks)
            return

        # MODE – track channel mode changes
        # :server MODE #chan +c   or   :nick!u@h MODE #chan -c
        if len(parts) >= 4 and command == "MODE":
            target = parts[2]
            if target.startswith(("#", "&", "+", "!")):
                modestr = parts[3].lstrip(":")
                self._apply_channel_modes(target.lower(), modestr)
            return

        # 324 – mode reply sent when we join or ask with MODE #chan
        # :server 324 botnick #chan +nrtc
        if len(parts) >= 5 and command == "324":
            channel = parts[3].lower()
            modestr = parts[4].lstrip(":")
            self.channel_modes[channel] = modestr
            return

        # PRIVMSG – commands
        if len(parts) >= 4 and command == "PRIVMSG":
            await self._handle_privmsg(parts, line)
            return

    def _next_nick(self):
        """Our nick was refused during registration: try with a trailing
        underscore, or replace the last character once the nick is long."""
        nick = self.nick + "_" if len(self.nick) < 16 else self.nick[:-1] + "_"
        if nick == self.nick:
            nick = self.config.nick[:8] + str(int(time.time()) % 10000)
        log.warning("[%s] Nick %s unavailable, trying %s", self.name, self.nick, nick)
        self.nick = nick
        self._raw(f"NICK {nick}")

    def _handle_nick(self, prefix: str, new_nick: str):
        old_nick = prefix.split("!", 1)[0]
        if old_nick == self.nick:
            self.nick = new_nick
            if "!" in prefix:
                self._own_hostmask = new_nick + "!" + prefix.split("!", 1)[1]
            log.info("[%s] Now known as %s", self.name, new_nick)
        for members in self.channel_members.values():
            if old_nick in members:
                members.discard(old_nick)
                members.add(new_nick)
        # An authed admin keeps their session under the new nick.
        self.authed = {
            (new_nick + "!" + h.split("!", 1)[1]) if _nick_of(h) == old_nick else h
            for h in self.authed}
        if old_nick == self.config.nick and self.nick != self.config.nick:
            self._raw(f"NICK {self.config.nick}")   # our nick is free

    async def _handle_cap(self, parts):
        sub = parts[3]
        if sub == "ACK":
            caps = " ".join(parts[4:]).lstrip(":").split()
            if "sasl" in caps:
                self._raw("AUTHENTICATE PLAIN")
        elif sub == "NAK":
            log.error("[%s] Server does not support SASL", self.name)
            self._raw("CAP END")

    def _on_registered(self):
        for channel in self.config.channels:
            self._raw(f"JOIN {channel}")
        if self.nick != self.config.nick:
            # After the JOINs: the nick's holder is often our own old
            # session, and seeing its QUIT in a shared channel lets us take
            # the nick back at once (see QUIT), whatever these lines do.
            for line in self.config.nick_recover:
                self._raw(line.replace("$nick", self.config.nick))
        # Messages that arrived while we were away go out after the JOINs.
        pending, self._pending = self._pending, []
        for line in pending:
            self._raw(line)
        self._last_nick_try = time.monotonic()

    def try_regain_nick(self):
        """Called periodically: ask for our configured nick back, at most
        nick_regain_tries times per connection (0 = no limit). The holder
        quitting or changing nick is still acted on after that."""
        if (not self._registered or self.nick == self.config.nick or
                time.monotonic() - self._last_nick_try < NICK_REGAIN_EVERY):
            return
        limit = self.config.nick_regain_tries
        if limit and self._nick_tries >= limit:
            if self._nick_tries == limit:
                self._nick_tries += 1   # log this only once
                log.warning("[%s] Gave up getting nick %s back after %d tries; "
                            "staying %s (use: raw NICK %s)", self.name,
                            self.config.nick, limit, self.nick, self.config.nick)
            return
        self._nick_tries += 1
        self._last_nick_try = time.monotonic()
        self._raw(f"NICK {self.config.nick}")

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
        msg = " ".join(parts[3:])
        if msg.startswith(":"):
            msg = msg[1:]

        pfx = self.config.command_prefix

        # Detect whether this is a PM (target is the bot's own nick).
        # Channel names always start with # & + ! on IRC servers.
        is_pm = not target.startswith(("#", "&", "+", "!"))

        if is_pm:
            # In PMs the prefix is optional — bare "join #foo" works the
            # same as "!join #foo", since there's no channel chatter to
            # disambiguate from.
            if msg.startswith(pfx):
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
