"""IRC connection: flood control, registration, SASL, nick handling, admin
sessions and ping timeouts. Lines are fed to Network._handle_line and the
replies read back from its send queue; nothing touches the network."""

import asyncio
import unittest
from unittest import mock

from src import network as netmod
from src.config import NetworkConfig
from src.network import FloodBucket, Network, _split_by_bytes


def make_network(**overrides):
    cfg = {"name": "test", "host": "irc.example", "nick": "gitbot",
           "channels": ["#a", "#b"]}
    cfg.update(overrides)
    bot = mock.Mock()
    bot.dispatch_command = mock.AsyncMock()
    return Network(NetworkConfig(cfg), bot)


def sent(net):
    """Drain and return everything queued for the server."""
    lines = []
    while not net._send_queue.empty():
        lines.append(net._send_queue.get_nowait())
    return lines


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestFloodBucket(unittest.TestCase):
    def test_burst_then_steady_rate(self):
        clock = FakeClock()
        bucket = FloodBucket(burst=5, delay=1.2, clock=clock)
        self.assertEqual([bucket.wait_time() for _ in range(5)], [0.0] * 5)
        # 6th line right away: wait one delay
        self.assertAlmostEqual(bucket.wait_time(), 1.2)
        # The old code reset its counter here and allowed another burst of
        # 5; a busy bot must stay at one line per delay instead.
        for _ in range(10):
            clock.now += 1.2
            self.assertAlmostEqual(bucket.wait_time(), 1.2)

    def test_refills_when_quiet(self):
        clock = FakeClock()
        bucket = FloodBucket(burst=5, delay=1.2, clock=clock)
        for _ in range(5):
            bucket.wait_time()
        clock.now += 60
        self.assertEqual([bucket.wait_time() for _ in range(5)], [0.0] * 5)


class TestSplit(unittest.TestCase):
    def test_never_cuts_a_character(self):
        chunks = _split_by_bytes("é" * 10, 5)
        self.assertEqual("".join(chunks), "é" * 10)
        self.assertTrue(all(len(c.encode()) <= 5 for c in chunks))


class TestRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_nick_in_use_tries_another(self):
        net = make_network()
        await net._handle_line(":irc.example 433 * gitbot :Nickname is already in use")
        self.assertEqual(sent(net), ["NICK gitbot_"])
        self.assertEqual(net.nick, "gitbot_")
        await net._handle_line(":irc.example 433 * gitbot_ :Nickname is already in use")
        self.assertEqual(sent(net), ["NICK gitbot__"])

    async def test_welcome_joins_then_flushes_pending(self):
        net = make_network()
        net.send_privmsg("#a", "queued while away")
        self.assertEqual(sent(net), [])          # not sent before 001
        await net._handle_line(":irc.example 001 gitbot_ :Welcome")
        self.assertEqual(net.nick, "gitbot_")
        self.assertEqual(sent(net), ["JOIN #a", "JOIN #b",
                                     "PRIVMSG #a :queued while away"])
        net.send_privmsg("#a", "now")
        self.assertEqual(sent(net), ["PRIVMSG #a :now"])

    async def test_pending_is_capped(self):
        net = make_network()
        for i in range(netmod.MAX_PENDING + 10):
            net.send_privmsg("#a", str(i))
        self.assertEqual(len(net._pending), netmod.MAX_PENDING)
        self.assertEqual(net._pending[0], "PRIVMSG #a :10")

    async def test_nick_recover_lines(self):
        net = make_network(nick_recover=[
            "PRIVMSG NickServ@services.dal.net :GHOST $nick hunter2",
            "NICK $nick"])
        await net._handle_line(":irc.example 433 * gitbot :in use")
        sent(net)
        await net._handle_line(":irc.example 001 gitbot_ :Welcome")
        self.assertEqual(sent(net), [
            "JOIN #a", "JOIN #b",
            "PRIVMSG NickServ@services.dal.net :GHOST gitbot hunter2",
            "NICK gitbot"])

    async def test_nick_recover_only_when_needed(self):
        net = make_network(nick_recover="PRIVMSG NickServ :REGAIN $nick")
        self.assertEqual(net.config.nick_recover, ["PRIVMSG NickServ :REGAIN $nick"])
        await net._handle_line(":irc.example 001 gitbot :Welcome")
        self.assertEqual(sent(net), ["JOIN #a", "JOIN #b"])

    async def test_nick_retries_stop_after_limit(self):
        net = make_network(nick_regain_tries=2)
        await net._handle_line(":irc.example 433 * gitbot :in use")
        await net._handle_line(":irc.example 001 gitbot_ :Welcome")
        sent(net)
        with mock.patch.object(netmod, "NICK_REGAIN_EVERY", 0):
            for _ in range(5):
                net.try_regain_nick()
        self.assertEqual(sent(net), ["NICK gitbot"] * 2)
        # The holder leaving still gets the nick back.
        await net._handle_line(":gitbot!old@host QUIT :bye")
        self.assertEqual(sent(net), ["NICK gitbot"])

    async def test_nick_retries_unlimited(self):
        net = make_network(nick_regain_tries=0)
        await net._handle_line(":irc.example 433 * gitbot :in use")
        await net._handle_line(":irc.example 001 gitbot_ :Welcome")
        sent(net)
        with mock.patch.object(netmod, "NICK_REGAIN_EVERY", 0):
            for _ in range(20):
                net.try_regain_nick()
        self.assertEqual(len(sent(net)), 20)

    async def test_regains_nick_when_holder_leaves(self):
        net = make_network()
        await net._handle_line(":irc.example 433 * gitbot :in use")
        await net._handle_line(":irc.example 001 gitbot_ :Welcome")
        sent(net)
        await net._handle_line(":gitbot!old@host QUIT :Ping timeout")
        self.assertEqual(sent(net), ["NICK gitbot"])
        await net._handle_line(":gitbot_!bot@host NICK :gitbot")
        self.assertEqual(net.nick, "gitbot")

    async def test_odd_lines_do_not_raise(self):
        net = make_network()
        for line in ("PING", "FOO", ":server", ":nick!u@h QUIT",
                     ":nick!u@h KICK #a", ":s 353 gitbot = #a :"):
            await net._handle_line(line)


class TestSasl(unittest.IsolatedAsyncioTestCase):
    async def test_uses_account_name(self):
        net = make_network(sasl_password="pw", sasl_username="account")
        await net._handle_line(":irc.example CAP * ACK :sasl")
        self.assertEqual(sent(net), ["AUTHENTICATE PLAIN"])
        await net._handle_line("AUTHENTICATE +")
        import base64
        payload = sent(net)[0].split(" ", 1)[1]
        self.assertEqual(base64.b64decode(payload), b"account\0account\0pw")

    async def test_failure_ends_cap_negotiation(self):
        net = make_network(sasl_password="pw")
        await net._handle_line(":irc.example 904 gitbot :SASL authentication failed")
        self.assertEqual(sent(net), ["CAP END"])


class TestAdminSessions(unittest.IsolatedAsyncioTestCase):
    async def test_session_follows_nick_change(self):
        net = make_network()
        net.authed.add("alice!a@home.example")
        await net._handle_line(":alice!a@home.example NICK :alice_away")
        self.assertTrue(net.is_authed("alice_away!a@home.example"))
        # Someone else taking the old nick is not an admin.
        self.assertFalse(net.is_authed("alice!evil@elsewhere.example"))

    async def test_quit_drops_session(self):
        net = make_network()
        net.authed.add("alice!a@home.example")
        await net._handle_line(":alice!a@home.example QUIT :bye")
        self.assertFalse(net.is_authed("alice!a@home.example"))

    async def test_members_follow_nick_change(self):
        net = make_network()
        net.channel_members["#a"] = {"alice"}
        await net._handle_line(":alice!a@h NICK :bob")
        self.assertEqual(net.channel_members["#a"], {"bob"})


class TestPingTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_pings_then_gives_up(self):
        net = make_network()
        reader = asyncio.StreamReader()
        with mock.patch.object(netmod, "READ_TIMEOUT", 0.05), \
             mock.patch.object(netmod, "PING_TIMEOUT", 0.05):
            with self.assertRaisesRegex(ConnectionError, "Ping timeout"):
                await net._read_loop(reader)
        self.assertEqual(sent(net), ["PING :gitbot"])

    async def test_any_line_resets_the_timer(self):
        net = make_network()
        reader = asyncio.StreamReader()

        async def feed():
            for _ in range(3):
                await asyncio.sleep(0.04)
                reader.feed_data(b":irc.example NOTICE gitbot :hi\r\n")
            reader.feed_eof()

        with mock.patch.object(netmod, "READ_TIMEOUT", 0.06), \
             mock.patch.object(netmod, "PING_TIMEOUT", 0.06):
            task = asyncio.ensure_future(feed())
            with self.assertRaisesRegex(ConnectionError, "closed"):
                await net._read_loop(reader)
            await task
        self.assertNotIn("PING :gitbot", sent(net))


if __name__ == "__main__":
    unittest.main()
