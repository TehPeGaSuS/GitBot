"""Async IRC client for one network connection."""

import asyncio
import logging
import ssl
import time
from typing import Callable, Optional

log = logging.getLogger("irc")

RECONNECT_DELAY_MIN = 5
RECONNECT_DELAY_MAX = 300


class IRCClient:
    def __init__(self, config: dict, on_message: Callable,
                 on_connected: Callable):
        """
        config keys:
            name, host, port, tls (bool), nickname, username, realname,
            sasl_plain (optional: {user, password}),
            nickserv_password (optional),
            channels (list of str)
        """
        self.config = config
        self.name = config["name"]
        self.on_message = on_message    # async fn(network, channel, nick, msg)
        self.on_connected = on_connected  # async fn(network)

        self._writer: Optional[asyncio.StreamWriter] = None
        self._channels: set = set()
        self._reconnect_delay = RECONNECT_DELAY_MIN
        self._running = True
        self._ready = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self):
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                log.warning("[%s] Connection error: %s", self.name, e)
            if not self._running:
                break
            log.info("[%s] Reconnecting in %ds…", self.name, self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, RECONNECT_DELAY_MAX)

    async def stop(self, message: str = "Disconnecting"):
        """Gracefully disconnect and stop the reconnect loop."""
        self._running = False
        if self._writer:
            try:
                self.send(f"QUIT :{message}")
                await asyncio.sleep(0.5)
                self._writer.close()
            except Exception:
                pass

    def send(self, line: str):
        if self._writer:
            log.debug("[%s] >> %s", self.name, line)
            self._writer.write((line + "\r\n").encode())

    def privmsg(self, target: str, text: str):
        # IRC lines are limited to 512 bytes including \r\n; split if needed
        prefix = f"PRIVMSG {target} :"
        limit = 510 - len(prefix.encode())
        encoded = text.encode("utf-8", errors="replace")
        while encoded:
            chunk, encoded = encoded[:limit], encoded[limit:]
            self.send(prefix + chunk.decode("utf-8", errors="replace"))

    def join(self, channel: str):
        self.send(f"JOIN {channel}")

    def in_channel(self, channel: str) -> bool:
        return channel.lower() in self._channels

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _connect(self):
        host = self.config["host"]
        port = self.config["port"]
        use_tls = self.config.get("tls", False)

        log.info("[%s] Connecting to %s:%d (tls=%s)…", self.name, host, port, use_tls)

        # Optional local bind address — per-network "bind" overrides global "_global_bind"
        bind = self.config.get("bind") or self.config.get("_global_bind")
        local_addr = (bind, 0) if bind else None

        if use_tls:
            ctx = ssl.create_default_context()
            if not self.config.get("tls_verify", True):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.open_connection(
                host, port, ssl=ctx, local_addr=local_addr)
        else:
            reader, writer = await asyncio.open_connection(
                host, port, local_addr=local_addr)

        self._writer = writer
        self._channels = set()
        self._ready = False

        nick = self.config["nickname"]
        username = self.config.get("username", nick)
        realname = self.config.get("realname", nick)

        # SASL PLAIN
        if "sasl_plain" in self.config:
            self.send("CAP REQ :sasl")

        self.send(f"NICK {nick}")
        self.send(f"USER {username} 0 * :{realname}")

        try:
            async for raw in self._read_lines(reader):
                await self._handle(raw)
        finally:
            writer.close()
            self._writer = None
            self._ready = False

    async def _read_lines(self, reader: asyncio.StreamReader):
        buf = b""
        while True:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=300)
            except asyncio.TimeoutError:
                log.warning("[%s] Read timeout, disconnecting", self.name)
                return
            if not data:
                log.info("[%s] Connection closed by server", self.name)
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8", errors="replace")

    async def _handle(self, raw: str):
        log.debug("[%s] << %s", self.name, raw)

        if raw.startswith("PING"):
            token = raw.split(":", 1)[-1] if ":" in raw else raw.split(" ", 1)[-1]
            self.send(f"PONG :{token}")
            return

        parts = raw.split(" ")

        # CAP negotiation for SASL
        if len(parts) >= 3 and parts[1] == "CAP":
            sub = parts[3] if len(parts) > 3 else ""
            if sub in ("ACK", ":ACK") and "sasl" in raw:
                self.send("AUTHENTICATE PLAIN")
            return

        if len(parts) >= 2 and parts[0] == "AUTHENTICATE":
            sasl = self.config["sasl_plain"]
            import base64
            token = base64.b64encode(
                f"\x00{sasl['user']}\x00{sasl['password']}".encode()
            ).decode()
            self.send(f"AUTHENTICATE {token}")
            return

        # Numeric: 903 = SASL success
        if len(parts) >= 2 and parts[1] == "903":
            self.send("CAP END")
            return

        # Numeric: 904/905 = SASL fail
        if len(parts) >= 2 and parts[1] in ("904", "905"):
            log.error("[%s] SASL authentication failed!", self.name)
            self.send("CAP END")
            return

        # 001 = welcome → we're registered
        if len(parts) >= 2 and parts[1] == "001":
            self._reconnect_delay = RECONNECT_DELAY_MIN
            log.info("[%s] Registered as %s", self.name, self.config["nickname"])

            if "nickserv_password" in self.config:
                ns_pw = self.config["nickserv_password"]
                self.send(f"PRIVMSG NickServ :IDENTIFY {ns_pw}")

            for ch in self.config.get("channels", []):
                self.join(ch)

            self._ready = True
            await self.on_connected(self.name)
            return

        # JOIN
        if len(parts) >= 3 and parts[1] == "JOIN":
            channel = parts[2].lstrip(":")
            nick_part = parts[0].lstrip(":")
            joiner = nick_part.split("!")[0]
            own_nick = self.config["nickname"]
            if joiner.lower() == own_nick.lower():
                self._channels.add(channel.lower())
                log.info("[%s] Joined %s", self.name, channel)
            return

        # KICK / PART (our own)
        if len(parts) >= 3 and parts[1] in ("KICK", "PART"):
            channel = parts[2].lstrip(":")
            self._channels.discard(channel.lower())
            if parts[1] == "KICK":
                # rejoin if kicked
                await asyncio.sleep(5)
                self.join(channel)
            return

        # PRIVMSG
        if len(parts) >= 4 and parts[1] == "PRIVMSG":
            full_prefix = parts[0].lstrip(":")
            nick = full_prefix.split("!")[0]
            target = parts[2]
            text = " ".join(parts[3:]).lstrip(":")
            await self.on_message(self.name, target, nick, full_prefix, text)
            return

        # 433 = nick in use
        if len(parts) >= 2 and parts[1] == "433":
            new_nick = self.config["nickname"] + "_"
            log.warning("[%s] Nick in use, trying %s", self.name, new_nick)
            self.send(f"NICK {new_nick}")
            return
