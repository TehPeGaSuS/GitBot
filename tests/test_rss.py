"""RSS fetching: HTTP errors and oversized feeds count as failures, and
parsing happens off the event loop."""

import unittest
from unittest import mock

from modules import rss

FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Example</title>
<item><title>First</title><link>https://example.org/1</link><guid>1</guid></item>
</channel></rss>"""


class FakeContent:
    def __init__(self, body):
        self.body = body

    async def read(self, n):
        return self.body[:n]


class FakeResponse:
    def __init__(self, status=200, body=FEED):
        self.status = status
        self.content = FakeContent(body)


class TestFeedFetching(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_is_a_failure(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            await rss._read_feed(FakeResponse(status=404))

    async def test_oversized_feed_is_refused(self):
        with mock.patch.object(rss, "MAX_FEED_BYTES", 100):
            with self.assertRaisesRegex(RuntimeError, "larger than"):
                await rss._read_feed(FakeResponse(body=b"x" * 101))

    async def test_parse_in_thread(self):
        with mock.patch("asyncio.to_thread", wraps=rss.asyncio.to_thread) as to_thread:
            parsed = await rss._parse(await rss._read_feed(FakeResponse()))
        to_thread.assert_called_once()
        self.assertEqual(parsed["feed"]["title"], "Example")
        self.assertEqual(parsed["entries"][0]["title"], "First")


if __name__ == "__main__":
    unittest.main()
