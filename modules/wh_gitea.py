"""
Gitea webhook payload handler.
Ported from bitbot's modules/git_webhooks/gitea.py
"""

from src import formatting as fmt

EVENT_CATEGORIES = {
    "ping":  ["ping"],
    "code":  ["push"],
    "pr-minimal": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened"
    ],
    "pr": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened",
        "pull_request/edited", "pull_request/assigned", "pull_request/unassigned",
        "pull_request_review_comment",
    ],
    "pr-all": [
        "pull_request", "pull_request_review", "pull_request_review_comment",
    ],
    "pr-review-minimal": [
        "pull_request_review/approved", "pull_request_review/reject",
    ],
    "issue-minimal": [
        "issues/opened", "issues/closed", "issues/reopened", "issues/deleted"
    ],
    "issue": [
        "issues/opened", "issues/closed", "issues/reopened", "issues/deleted",
        "issues/edited", "issues/assigned", "issues/unassigned", "issue_comment",
    ],
    "issue-all":              ["issues", "issue_comment"],
    "issue-comment-minimal":  ["issue_comment/created", "issue_comment/deleted"],
    "repo": ["create", "delete", "release", "fork", "repository"],
}

COMMENT_ACTIONS = {
    "created": "commented",
    "edited":  "edited a comment",
    "deleted": "deleted a comment",
}
RELEASE_ACTIONS = {
    "updated":   "published",
    "published": "published",
    "deleted":   "deleted",
}


class Gitea:
    COMMIT_LINES_MAX = 3

    def is_private(self, data, headers):
        return data.get("repository", {}).get("private", False)

    def names(self, data, headers):
        full_name = repo_username = repo_name = organisation = None
        if "repository" in data:
            full_name = data["repository"]["full_name"]
            repo_username, repo_name = full_name.split("/", 1)
        if "organization" in data:
            organisation = data["organization"]["login"]
        return full_name, repo_username, repo_name, organisation

    def branch(self, data, headers):
        if "ref" in data:
            return data["ref"].rpartition("/")[2]
        return None

    def event(self, data, headers):
        event = headers.get("X-Gitea-Event", "")
        action = data.get("action")
        event_action = "%s/%s" % (event, action) if action else None
        return [event] + ([event_action] if event_action else [])

    def event_categories(self, event):
        return EVENT_CATEGORIES.get(event, [event])

    def webhook(self, full_name, event, data, headers):
        dispatch = {
            "push":             lambda: self._push(full_name, data),
            "pull_request":     lambda: self._pull_request(full_name, data),
            "pull_request_review":         lambda: self._pull_request_review(full_name, data),
            "pull_request_review_comment": lambda: self._pull_request_review_comment(full_name, data),
            "issues":           lambda: self._issues(full_name, data),
            "issue_comment":    lambda: self._issue_comment(full_name, data),
            "create":           lambda: self._create(full_name, data),
            "delete":           lambda: self._delete(full_name, data),
            "repository":       lambda: [],
            "release":          lambda: self._release(full_name, data),
            "fork":             lambda: self._fork(full_name, data),
            "ping":             lambda: [("Received new webhook", None)],
        }
        fn = dispatch.get(event)
        return fn() if fn else []

    def _short(self, h): return h[:7]

    def _push(self, full_name, data):
        outputs = []
        branch = fmt.color(data["ref"].rpartition("/")[2], fmt.COLOR_BRANCH)
        author = fmt.bold(data["pusher"]["login"])
        commits = data.get("commits", [])
        if len(commits) <= self.COMMIT_LINES_MAX:
            for c in commits:
                h = fmt.color(self._short(c["id"]), fmt.COLOR_ID)
                msg = c["message"].split("\n")[0].strip()
                outputs.append(("%s pushed %s to %s: %s"
                    % (author, h, branch, msg), c["url"]))
        else:
            url = data.get("compare_url", "")
            outputs.append(("%s pushed %d commits to %s"
                % (author, len(commits), branch), url))
            shown = commits[:self.COMMIT_LINES_MAX]
            for c in shown:
                h = fmt.color(self._short(c["id"]), fmt.COLOR_ID)
                msg = c["message"].split("\n")[0].strip()
                outputs.append(("%s %s - %s" % (author, h, msg), c["url"]))
            hidden = len(commits) - len(shown)
            if hidden > 0:
                outputs.append(("(+%d hidden commits)" % hidden, None))
        return outputs

    def _pull_request(self, full_name, data):
        pr = data["pull_request"]
        number = fmt.color("#%s" % pr["number"], fmt.COLOR_ID)
        branch = fmt.color(pr["base"]["ref"], fmt.COLOR_BRANCH)
        action = data["action"]
        action_desc = "%s %s" % (action, number)
        if action == "opened":
            action_desc = "requested %s merge into %s" % (number, branch)
        elif action == "closed":
            if pr.get("merged"):
                action_desc = "%s %s into %s" % (
                    fmt.color("merged", fmt.COLOR_POSITIVE), number, branch)
            else:
                action_desc = "%s %s" % (fmt.color("closed", fmt.COLOR_NEGATIVE), number)
        elif action == "ready_for_review":
            action_desc = "marked %s ready for review" % number
        elif action == "synchronize":
            action_desc = "committed to %s" % number
        author = fmt.bold(data["sender"]["login"])
        url = pr["html_url"]
        return [("[PR] %s %s: %s" % (author, action_desc, pr["title"]), url)]

    def _pull_request_review(self, full_name, data):
        # Gitea sends this when a review is submitted (approved / request changes / comment)
        review = data.get("review", {})
        pr = data.get("pull_request", {})
        number = fmt.color("#%s" % pr.get("number", "?"), fmt.COLOR_ID)
        reviewer = fmt.bold(data.get("sender", {}).get("login", "?"))
        state = review.get("type", "")
        url = review.get("html_url", pr.get("html_url", ""))
        state_desc = {
            "approved":          "approved",
            "reject":            "requested changes on",
            "comment":           "reviewed",
        }.get(state, state or "reviewed")
        pr_title = pr.get("title", "")
        return [("[PR] %s %s %s: %s" % (reviewer, state_desc, number, pr_title), url)]

    def _pull_request_review_comment(self, full_name, data):
        # Inline review comment on a specific line of a PR diff
        pr = data.get("pull_request", {})
        number = fmt.color("#%s" % pr.get("number", "?"), fmt.COLOR_ID)
        commenter = fmt.bold(data.get("sender", {}).get("login", "?"))
        action = COMMENT_ACTIONS.get(data.get("action", ""), data.get("action", ""))
        url = data.get("comment", {}).get("html_url", pr.get("html_url", ""))
        pr_title = pr.get("title", "")
        return [("[PR] %s %s on %s: %s" % (commenter, action, number, pr_title), url)]

    def _issues(self, full_name, data):
        number = fmt.color("#%s" % data["issue"]["number"], fmt.COLOR_ID)
        action = data["action"]
        author = fmt.bold(data["sender"]["login"])
        url = "%s/issues/%d" % (data["repository"]["html_url"], data["issue"]["number"])
        return [("[issue] %s %s %s: %s"
            % (author, action, number, data["issue"]["title"]), url)]

    def _issue_comment(self, full_name, data):
        if "changes" in data:
            if data["changes"].get("body", {}).get("from") == data["comment"]["body"]:
                return []
        number = fmt.color("#%s" % data["issue"]["number"], fmt.COLOR_ID)
        action = data.get("action", "")
        type_ = "PR" if data["issue"].get("pull_request") else "issue"
        commenter = fmt.bold(data["sender"]["login"])
        url = data["comment"]["html_url"]
        return [("[%s] %s %s on %s: %s"
            % (type_, commenter, COMMENT_ACTIONS.get(action, action),
               number, data["issue"]["title"]), url)]

    def _create(self, full_name, data):
        ref = fmt.color(data["ref"], fmt.COLOR_BRANCH)
        sender = fmt.bold(data["sender"]["login"])
        return [("%s created a %s: %s" % (sender, data["ref_type"], ref), None)]

    def _delete(self, full_name, data):
        ref = fmt.color(data["ref"], fmt.COLOR_BRANCH)
        sender = fmt.bold(data["sender"]["login"])
        return [("%s deleted a %s: %s" % (sender, data["ref_type"], ref), None)]

    def _release(self, full_name, data):
        action = RELEASE_ACTIONS.get(data["action"], data["action"])
        name = data["release"].get("name") or ""
        if name:
            name = ": %s" % name
        author = fmt.bold(data["release"]["author"]["login"])
        return [("%s %s a release%s" % (author, action, name), None)]

    def _fork(self, full_name, data):
        forker = fmt.bold(data["sender"]["login"])
        fork_name = fmt.color(data["repository"]["full_name"], fmt.LIGHTBLUE)
        url = data["repository"]["html_url"]
        return [("%s forked into %s" % (forker, fork_name), url)]
