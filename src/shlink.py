"""
src/shlink.py
=============

Async Shlink URL shortener integration.

Shlink is a self-hosted URL shortener (https://shlink.io).
When configured, every URL produced by the webhook formatters is
shortened before being posted to IRC.

Configuration (in config.json under "shlink"):
  {
    "url":    "https://s.example.com",   -- base URL of your Shlink instance
    "api_key": "YOUR-API-KEY"            -- Shlink REST API key
  }

If the "shlink" block is absent, or "url" / "api_key" are empty, URL
shortening is disabled and all URLs are passed through unchanged.

Errors are logged but never bubble up — if shortening fails the
original long URL is used as a fallback so IRC output is never lost.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Shlink REST endpoint for creating short URLs
_REST_PATH = "/rest/v3/short-urls"

# Per-request timeout in seconds
_TIMEOUT = 5.0


class ShlinkClient:
    """Async client for the Shlink REST API.

    Uses a single aiohttp.ClientSession for connection reuse.
    The session is created lazily on the first request.
    """

    def __init__(self, base_url: str, api_key: str):
        # Strip trailing slash for safe concatenation
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._session = None   # aiohttp.ClientSession, created on demand

    # ---------------------------------------------------------------- public

    async def shorten(self, long_url: str) -> str:
        """Return a short URL for *long_url*, or *long_url* on failure."""
        if not long_url:
            return long_url
        try:
            return await self._shorten(long_url)
        except Exception as exc:
            log.warning("[shlink] Failed to shorten %s: %s", long_url, exc)
            return long_url

    async def close(self):
        """Close the underlying HTTP session (call on bot shutdown)."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # --------------------------------------------------------------- internal

    async def _get_session(self):
        if self._session is None or self._session.closed:
            try:
                import aiohttp
            except ImportError:
                raise RuntimeError(
                    "aiohttp is required for Shlink support. "
                    "Install it with: pip install aiohttp"
                )
            self._session = aiohttp.ClientSession(
                headers={
                    "X-Api-Key": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )
        return self._session

    async def _shorten(self, long_url: str) -> str:
        import aiohttp
        session = await self._get_session()
        endpoint = self.base_url + _REST_PATH
        payload = {"longUrl": long_url}

        async with session.post(
            endpoint,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                short = data.get("shortUrl", "")
                if short:
                    log.debug("[shlink] %s -> %s", long_url, short)
                    return short
                log.warning("[shlink] No shortUrl in response: %s", data)
                return long_url

            # 422 = URL already exists — Shlink returns the existing short URL
            if resp.status == 422:
                data = await resp.json()
                detail = data.get("detail", "")
                # If it's a duplicate, Shlink may include the short URL in the
                # error body (depends on version).  Fall back to long URL.
                log.debug("[shlink] 422 for %s: %s", long_url, detail)
                return long_url

            body = await resp.text()
            log.warning(
                "[shlink] Unexpected status %d for %s: %s",
                resp.status, long_url, body[:200],
            )
            return long_url


# ---------------------------------------------------------------------------
# Module-level singleton — set up by the bot at startup via configure()
# ---------------------------------------------------------------------------

_client: Optional[ShlinkClient] = None


def configure(base_url: str, api_key: str):
    """Initialise the module-level client. Call once from bot startup."""
    global _client
    if base_url and api_key:
        _client = ShlinkClient(base_url, api_key)
        log.info("[shlink] URL shortening enabled via %s", base_url)
    else:
        _client = None
        log.info("[shlink] URL shortening disabled (no url/api_key configured)")


def is_enabled() -> bool:
    return _client is not None


async def shorten(url: str) -> str:
    """Shorten *url* if Shlink is configured; otherwise return *url*."""
    if _client is None or not url:
        return url
    return await _client.shorten(url)


async def close():
    """Close the client session. Call on bot shutdown."""
    if _client is not None:
        await _client.close()
