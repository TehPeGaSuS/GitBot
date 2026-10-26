"""auth/deauth: sessions tied to the full hostmask, lockout after wrong
passwords."""

import unittest
from unittest import mock

from modules import admin
from src.bot import Bot
from tests.test_network import make_network, sent


def make_ctx(net, hostmask, args, command="auth"):
    ctx = mock.Mock()
    ctx.is_pm = True
    ctx.network = net
    ctx.hostmask = hostmask
    ctx.nick = hostmask.split("!", 1)[0]
    ctx.command = command
    ctx.args = args
    ctx.bot.config.auth_password = "s3cret"
    return ctx


def replies(net):
    return [line.split(" :", 1)[1] for line in sent(net)]


class TestAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        admin._failures.clear()
        self.net = make_network()
        self.net._registered = True

    async def test_good_password_makes_hostmask_admin(self):
        await admin.handle_auth(make_ctx(self.net, "alice!a@home", ["s3cret"]))
        self.assertIn("now authenticated", replies(self.net)[0])
        is_admin = Bot._is_admin(None, self.net, "alice!a@home", "alice")
        self.assertTrue(is_admin)
        self.assertFalse(Bot._is_admin(None, self.net, "alice!x@other", "alice"))

    async def test_deauth(self):
        await admin.handle_auth(make_ctx(self.net, "alice!a@home", ["s3cret"]))
        await admin.handle_auth(make_ctx(self.net, "alice!a@home", [], "deauth"))
        self.assertFalse(self.net.is_authed("alice!a@home"))

    async def test_lockout_after_wrong_passwords(self):
        for _ in range(admin.AUTH_FREE_TRIES - 1):
            await admin.handle_auth(make_ctx(self.net, "eve!e@bad", ["guess"]))
        self.assertEqual(replies(self.net), ["Wrong password."] * 2)
        await admin.handle_auth(make_ctx(self.net, "eve!e@bad", ["guess"]))
        self.assertIn("wait 30 seconds", replies(self.net)[0])
        # Even the right password is refused while locked out, and a new
        # nick from the same user@host doesn't help.
        await admin.handle_auth(make_ctx(self.net, "eve2!e@bad", ["s3cret"]))
        self.assertIn("Try again in", replies(self.net)[0])
        self.assertFalse(self.net.is_authed("eve2!e@bad"))

    def test_lockout_grows_and_is_capped(self):
        key = ("test", "e@bad")
        locks = [admin._record_failure(key, now=0) for _ in range(12)]
        self.assertEqual(locks[:4], [0, 0, 30, 60])
        self.assertEqual(locks[-1], admin.AUTH_LOCK_MAX)
        self.assertEqual(admin._locked_for(key, now=admin.AUTH_LOCK_MAX), 0)

    async def test_not_in_channel(self):
        ctx = make_ctx(self.net, "alice!a@home", ["s3cret"])
        ctx.is_pm = False
        await admin.handle_auth(ctx)
        self.assertFalse(self.net.is_authed("alice!a@home"))


if __name__ == "__main__":
    unittest.main()
