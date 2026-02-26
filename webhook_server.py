"""Tiny async HTTP server for receiving git forge webhooks."""

import asyncio
import hashlib
import hmac
import json
import logging
import urllib.parse
from typing import Callable, Optional

log = logging.getLogger("webhook")

# Max payload size: 10 MB
MAX_BODY = 10 * 1024 * 1024


class WebhookServer:
    def __init__(self, host: str, port: int, deliver: Callable,
                 secrets: Optional[dict] = None):
        """
        deliver: async callable(forge, headers, data)
                 forge is 'github' | 'gitea' | 'gitlab'
        secrets: per-forge secrets, e.g.:
                 {'github': 'abc', 'gitea': 'xyz', 'gitlab': 'def'}

        Verification supports two modes (both optional, both can coexist):

          1. URL token  — append ?secret=<value> to the webhook URL you
                          configure in the forge. Simplest to set up.
                          e.g. https://yourhost/github?secret=abc

          2. HMAC header — configure the same secret in the forge's webhook
                          settings. GitHub/Gitea send HMAC-SHA256; GitLab
                          sends the token directly as X-Gitlab-Token.

        If a secret is configured for a forge, the URL token is checked first.
        If that passes, the request is accepted without checking HMAC headers.
        If no URL token is present, HMAC header verification is attempted.
        If neither passes, the request is rejected with 403.
        If no secret is configured for a forge, all requests are accepted.
        """
        self._host    = host
        self._port    = port
        self._deliver = deliver
        self._secrets = {
            forge: s.encode()
            for forge, s in (secrets or {}).items()
            if s
        }

    async def run(self):
        server = await asyncio.start_server(
            self._handle, self._host, self._port)
        addr = server.sockets[0].getsockname()
        log.info("Webhook server listening on %s:%d", *addr)
        async with server:
            await server.serve_forever()

    # ── HTTP parsing ──────────────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        try:
            await self._dispatch(reader, writer)
        except Exception as e:
            log.exception("Error handling webhook request: %s", e)
            self._respond(writer, 500, "Internal Server Error")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch(self, reader, writer):
        try:
            request_line = (await asyncio.wait_for(
                reader.readline(), timeout=10)).decode()
        except asyncio.TimeoutError:
            self._respond(writer, 408, "Request Timeout")
            return

        parts = request_line.strip().split(" ")
        if len(parts) < 2:
            self._respond(writer, 400, "Bad Request")
            return

        method, path = parts[0], parts[1]

        # Read headers
        headers = {}
        while True:
            try:
                line = (await asyncio.wait_for(
                    reader.readline(), timeout=10)).decode().strip()
            except asyncio.TimeoutError:
                self._respond(writer, 408, "Request Timeout")
                return
            if not line:
                break
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip()] = v.strip()

        if method != "POST":
            self._respond(writer, 405, "Method Not Allowed")
            return

        # Parse path and query string
        # Supports /github?secret=abc, /gitea?secret=xyz, etc.
        parsed     = urllib.parse.urlparse(path)
        clean_path = parsed.path.rstrip("/").lstrip("/").lower()
        if clean_path not in ("github", "gitea", "gitlab"):
            self._respond(writer, 404, "Not Found")
            return
        forge      = clean_path
        qs_params  = urllib.parse.parse_qs(parsed.query)
        url_secret = qs_params.get("secret", [None])[0]

        # Read body
        content_length = int(headers.get("Content-Length", 0))
        if content_length > MAX_BODY:
            self._respond(writer, 413, "Payload Too Large")
            return

        try:
            body = await asyncio.wait_for(
                reader.read(content_length), timeout=30)
        except asyncio.TimeoutError:
            self._respond(writer, 408, "Request Timeout")
            return

        # Verification — only enforced if a secret is configured for this forge
        expected_secret = self._secrets.get(forge)
        if expected_secret:
            if url_secret is not None:
                # Mode 1: ?secret= URL token — simple constant-time compare
                if not hmac.compare_digest(url_secret.encode(), expected_secret):
                    log.warning("[%s] URL secret mismatch", forge)
                    self._respond(writer, 403, "Forbidden")
                    return
            else:
                # Mode 2: HMAC signature header
                if not self._verify_hmac(forge, headers, body, expected_secret):
                    log.warning("[%s] HMAC signature verification failed", forge)
                    self._respond(writer, 403, "Forbidden")
                    return

        # Parse body
        content_type = headers.get("Content-Type", "")
        if "x-www-form-urlencoded" in content_type:
            qs  = urllib.parse.parse_qs(body.decode())
            raw = qs.get("payload", ["{}"])[0]
        else:
            raw = body.decode()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("[%s] JSON parse error: %s", forge, e)
            self._respond(writer, 400, "Bad JSON")
            return

        self._respond(writer, 200, "OK")
        asyncio.create_task(self._deliver(forge, headers, data))

    @staticmethod
    def _verify_hmac(forge: str, headers: dict, body: bytes,
                     secret: bytes) -> bool:
        if forge == "github":
            sig_header = headers.get("X-Hub-Signature-256", "")
            if not sig_header.startswith("sha256="):
                return False
            expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig_header[7:], expected)
        elif forge == "gitea":
            sig_header = headers.get("X-Gitea-Signature", "")
            expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig_header, expected)
        elif forge == "gitlab":
            token = headers.get("X-Gitlab-Token", "")
            return hmac.compare_digest(token.encode(), secret)
        return True

    @staticmethod
    def _respond(writer, code: int, message: str):
        body = message.encode()
        response = (
            f"HTTP/1.1 {code} {message}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body
        writer.write(response)
