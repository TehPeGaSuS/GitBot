"""Webhook handlers turn real example payloads into the expected IRC lines."""

import json
import pathlib
import unittest

from modules.wh_github import GitHub
from src import formatting as fmt

PAYLOADS = pathlib.Path(__file__).parent / "payloads"


def github(name):
    event = name.split(".")[0]
    data = json.loads((PAYLOADS / "github" / (name + ".json")).read_text())
    headers = {"X-GitHub-Event": event}
    handler = GitHub()
    full_name = handler.names(data, headers)[0]
    lines = handler.webhook(full_name, event, data, headers)
    return handler, data, headers, [(fmt.strip_formatting(t), url) for t, url in lines]


class TestGitHub(unittest.TestCase):
    def test_issue_opened(self):
        handler, data, headers, lines = github("issues.opened")
        self.assertEqual(handler.event(data, headers), ["issues", "issues/opened"])
        self.assertEqual(lines, [(
            "[issue] Codertocat opened #1: Spelling error in the README file",
            "https://github.com/Codertocat/Hello-World/issues/1")])

    def test_issue_comment(self):
        _, _, _, lines = github("issue_comment.created")
        (text, url), = lines
        self.assertTrue(text.startswith("[issue] Codertocat commented on #1"))
        self.assertTrue(url.endswith("#issuecomment-492700400"))

    def test_pull_request_opened(self):
        _, _, _, lines = github("pull_request.opened")
        self.assertEqual(lines, [(
            "[PR] Codertocat requested #2 merge into master: "
            "Update the README with new information.",
            "https://github.com/Codertocat/Hello-World/pull/2")])

    def test_push(self):
        handler, data, headers, lines = github("push")
        self.assertEqual(handler.branch(data, headers), "master")
        self.assertEqual(lines, [(
            "Codertocat pushed 6113728 to master: Initial commit",
            "https://github.com/Codertocat/Hello-World/commit/"
            "6113728f27ae82c7b1a177c8d03f9e96e0adf246")])

    def test_release_published(self):
        _, _, _, lines = github("release.published")
        (text, url), = lines
        self.assertIn("Codertocat published", text)
        self.assertEqual(url, "https://github.com/Codertocat/Hello-World/"
                              "releases/tag/0.0.1")


if __name__ == "__main__":
    unittest.main()
