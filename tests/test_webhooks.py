"""Webhook HTTP server: secrets and signatures, and the !webhook command's
validation. Uses aiohttp's test client, so no port is opened."""

import hashlib
import hmac
import io
import json
import pathlib
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from modules import webhooks
from src.config import WebhookConfig

PAYLOADS = pathlib.Path(__file__).parent / "payloads"


class FakeNet:
    def __init__(self):
        self.channel_members = {}
        self.lines = []

    def has_channel_mode(self, channel, flag):
        return False

    def send_privmsg(self, target, text):
        self.lines.append((target, text))


class FakeDB:
    def __init__(self, hooks):
        self.hooks = hooks

    def find_by_channel_key(self, key):
        return [("net", "#chan", self.hooks)] if key == "git-webhooks" else []

    def get_channel(self, net, channel, key, default=None):
        return default


def make_bot(secret="", allow_unsigned=False, platform="github",
             repo="codertocat/hello-world"):
    bot = mock.Mock()
    bot.config.webhook = WebhookConfig(
        {"secret": secret, "allow_unsigned": allow_unsigned})
    bot.db = FakeDB({"%s:%s" % (platform, repo):
                     {"events": webhooks.DEFAULT_EVENT_CATEGORIES, "branches": []}})
    bot.networks = {"net": FakeNet()}
    return bot


def github_body(name="issues.opened"):
    return (PAYLOADS / "github" / (name + ".json")).read_bytes()


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookCase(unittest.IsolatedAsyncioTestCase):
    async def post(self, bot, path, body, headers):
        server = webhooks.WebhookServer(bot)
        async with TestClient(TestServer(server.make_app())) as client:
            resp = await client.post(path, data=body, headers=headers)
            return resp.status, await resp.text()


class TestSecrets(WebhookCase):
    async def test_no_secret_refuses_by_default(self):
        bot = make_bot()
        status, _ = await self.post(bot, "/github", github_body(),
                                    {"X-GitHub-Event": "issues",
                                     "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(bot.networks["net"].lines, [])

    async def test_server_does_not_start_without_secret(self):
        server = webhooks.WebhookServer(make_bot())
        with mock.patch.object(webhooks.log, "error") as error:
            await server.start()      # returns instead of listening
        self.assertIn("NOT started", error.call_args[0][0])

    async def test_allow_unsigned(self):
        bot = make_bot(allow_unsigned=True)
        status, text = await self.post(bot, "/github", github_body(),
                                       {"X-GitHub-Event": "issues",
                                        "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["deliveries"], 1)

    async def test_github_signature(self):
        bot = make_bot(secret="topsecret")
        body = github_body()
        base = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}
        status, _ = await self.post(bot, "/github", body, base)
        self.assertEqual(status, 403)                       # missing
        status, _ = await self.post(bot, "/github", body,
                                    dict(base, **{"X-Hub-Signature-256": sign("wrong", body)}))
        self.assertEqual(status, 403)                       # wrong
        status, _ = await self.post(bot, "/github", body,
                                    dict(base, **{"X-Hub-Signature-256": sign("topsecret", body)}))
        self.assertEqual(status, 200)
        (target, line), = bot.networks["net"].lines
        self.assertEqual(target, "#chan")
        self.assertIn("Spelling error in the README file", line)

    async def test_gitlab_token(self):
        bot = make_bot(secret="tok", platform="gitlab", repo="group/proj")
        body = json.dumps({"object_kind": "push", "ref": "refs/heads/main",
                           "project": {"path_with_namespace": "group/proj"},
                           "commits": []}).encode()
        base = {"X-Gitlab-Event": "Push Hook", "Content-Type": "application/json"}
        status, _ = await self.post(bot, "/gitlab", body, dict(base, **{"X-Gitlab-Token": "nope"}))
        self.assertEqual(status, 403)
        status, _ = await self.post(bot, "/gitlab", body, dict(base, **{"X-Gitlab-Token": "tok"}))
        self.assertEqual(status, 200)

    async def test_large_body_accepted(self):
        bot = make_bot(allow_unsigned=True)
        data = json.loads(github_body())
        data["issue"]["body"] = "x" * (2 * 1024 * 1024)    # over aiohttp's 1 MB default
        status, _ = await self.post(bot, "/github", io.BytesIO(json.dumps(data).encode()),
                                    {"X-GitHub-Event": "issues",
                                     "Content-Type": "application/json"})
        self.assertEqual(status, 200)


class TestCommandValidation(unittest.IsolatedAsyncioTestCase):
    def test_event_names(self):
        u = webhooks._unknown_events
        self.assertEqual(u("github", ["pr", "push", "issues/opened",
                                      "check_suite+failure", "star", "prs"]), ["prs"])
        self.assertEqual(u("gitea", ["star", "repo"]), ["star"])    # no stars on Gitea
        self.assertEqual(u("gitlab", ["note+mergerequest", "merge_request/merge"]), [])

    async def run_command(self, args):
        ctx = mock.Mock()
        ctx.is_admin = True
        ctx.is_pm = False
        ctx.args = args
        ctx.bot.db.get_channel.return_value = {
            "github:o/r": {"events": ["code"], "branches": []}}
        await webhooks.handle_command(ctx)
        return ctx

    async def test_events_typo_rejected(self):
        ctx = await self.run_command(["events", "github", "o/r", "code", "prs"])
        self.assertIn("Unknown event category prs", ctx.error.call_args[0][0])
        ctx.bot.db.set_channel.assert_not_called()

    async def test_events_accepted(self):
        ctx = await self.run_command(["events", "github", "o/r", "code", "pr"])
        ctx.error.assert_not_called()
        ctx.bot.db.set_channel.assert_called_once()

    async def test_settings_need_true_or_false(self):
        ctx = await self.run_command(["settings", "git-shlink", "ture"])
        self.assertIn("Use true or false", ctx.error.call_args[0][0])
        ctx.bot.db.set_channel.assert_not_called()
        ctx = await self.run_command(["settings", "git-shlink", "off"])
        ctx.bot.db.set_channel.assert_called_once_with(
            ctx.net_name, ctx.channel, "git-shlink", False)


if __name__ == "__main__":
    unittest.main()
