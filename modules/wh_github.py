"""
GitHub webhook payload handler.
Ported from bitbot's modules/git_webhooks/github.py
"""

from src import formatting as fmt

COMMIT_URL       = "https://github.com/%s/commit/%s"
COMMIT_RANGE_URL = "https://github.com/%s/compare/%s...%s"
CREATE_URL       = "https://github.com/%s/tree/%s"
PR_URL           = "https://github.com/%s/pull/%s"
PR_COMMIT_RANGE  = "https://github.com/%s/pull/%s/files/%s..%s"
PR_COMMIT_URL    = "https://github.com/%s/pull/%s/commits/%s"

COMMENT_MAX = 100

EVENT_CATEGORIES = {
    "ping":     ["ping"],
    "code":     ["push", "commit_comment"],
    "pr-minimal": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened"
    ],
    "pr": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened",
        "pull_request/edited", "pull_request/assigned",
        "pull_request/unassigned", "pull_request_review",
        "pull_request/locked", "pull_request/unlocked",
        "pull_request_review_comment",
    ],
    "pr-all":   ["pull_request", "pull_request_review",
                 "pull_request_review_comment"],
    "pr-review-minimal": [
        "pull_request_review/submitted", "pull_request_review/dismissed"
    ],
    "pr-review-comment-minimal": [
        "pull_request_review_comment/created",
        "pull_request_review_comment/deleted",
    ],
    "issue-minimal": [
        "issues/opened", "issues/closed", "issues/reopened",
        "issues/deleted", "issues/transferred",
    ],
    "issue": [
        "issues/opened", "issues/closed", "issues/reopened",
        "issues/deleted", "issues/edited", "issues/assigned",
        "issues/unassigned", "issues/locked", "issues/unlocked",
        "issues/transferred", "issue_comment",
    ],
    "issue-all": ["issues", "issue_comment"],
    "issue-comment-minimal": [
        "issue_comment/created", "issue_comment/deleted"
    ],
    "repo":  ["create", "delete", "release", "fork"],
    "team":  ["membership"],
    "star":  ["watch"],
}

CHECK_SUITE_CONCLUSION = {
    "success":        ("passed",           fmt.COLOR_POSITIVE),
    "failure":        ("failed",           fmt.COLOR_NEGATIVE),
    "neutral":        ("finished",         fmt.COLOR_NEUTRAL),
    "cancelled":      ("was cancelled",    fmt.COLOR_NEGATIVE),
    "timed_out":      ("timed out",        fmt.COLOR_NEGATIVE),
    "action_required":("requires action",  fmt.COLOR_NEUTRAL),
}

COMMENT_ACTIONS = {
    "created": "commented",
    "edited":  "edited a comment",
    "deleted": "deleted a comment",
}


class GitHub:
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
        event = headers.get("X-GitHub-Event", "")
        action = data.get("action")
        category = None
        if "review" in data and "state" in data.get("review", {}):
            category = "%s+%s" % (event, data["review"]["state"])
        elif "check_suite" in data and "conclusion" in data.get("check_suite", {}):
            category = "%s+%s" % (event, data["check_suite"]["conclusion"])
        event_action = "%s/%s" % (event, action) if action else None
        category_action = "%s/%s" % (category, action) if (category and action) else None
        return [event] + list(filter(None, [event_action, category, category_action]))

    def event_categories(self, event):
        return EVENT_CATEGORIES.get(event, [event])

    def webhook(self, full_name, event, data, headers):
        dispatch = {
            "push":                        lambda: self._push(full_name, data),
            "commit_comment":              lambda: self._commit_comment(full_name, data),
            "pull_request":                lambda: self._pull_request(full_name, data),
            "pull_request_review":         lambda: self._pull_request_review(full_name, data),
            "pull_request_review_comment": lambda: self._pull_request_review_comment(full_name, data),
            "issue_comment":               lambda: self._issue_comment(full_name, data),
            "issues":                      lambda: self._issues(full_name, data),
            "create":                      lambda: self._create(full_name, data),
            "delete":                      lambda: self._delete(full_name, data),
            "release":                     lambda: self._release(full_name, data),
            "check_suite":                 lambda: self._check_suite(full_name, data),
            "fork":                        lambda: self._fork(full_name, data),
            "ping":                        lambda: self._ping(data),
            "watch":                       lambda: self._watch(data),
            "membership":                  lambda: self._membership(data),
        }
        fn = dispatch.get(event)
        return fn() if fn else []

    # ---------------------------------------------------------------- helpers

    def _short(self, h): return h[:7]

    def _color_branch(self, b): return fmt.color(b, fmt.COLOR_BRANCH)
    def _color_id(self, s):     return fmt.color(str(s), fmt.COLOR_ID)
    def _bold(self, s):         return fmt.bold(str(s))
    def _pos(self, s):          return fmt.color(s, fmt.COLOR_POSITIVE)
    def _neg(self, s):          return fmt.color(s, fmt.COLOR_NEGATIVE)

    def _comment(self, s):
        line = s.split("\n")[0].strip()
        if len(line) <= COMMENT_MAX:
            return line
        left = line[:COMMENT_MAX]
        if " " in left:
            left = left.rsplit(" ", 1)[0]
        return left + "[...]"

    def _format_push(self, branch, author, commits, forced, single_url, range_url):
        outputs = []
        force_str = fmt.color("force", fmt.RED) + " " if forced else ""
        if not commits and forced:
            outputs.append(("%s %spushed to %s" % (author, force_str, branch), None))
        elif len(commits) <= 3:
            for c in commits:
                h = fmt.color(self._short(c["id"]), fmt.COLOR_ID)
                msg = c["message"].split("\n")[0].strip()
                url = single_url % c["id"]
                outputs.append(("%s %spushed %s to %s: %s"
                    % (author, force_str, h, branch, msg), url))
        else:
            url = range_url
            outputs.append(("%s %spushed %d commits to %s"
                % (author, force_str, len(commits), branch), url))
        return outputs

    # ---------------------------------------------------------------- events

    def _ping(self, data):
        return [("Received new webhook", None)]

    def _push(self, full_name, data):
        branch = self._color_branch(data["ref"].split("/", 2)[2])
        author = self._bold(data["pusher"]["name"])
        range_url = None
        if data["commits"]:
            range_url = COMMIT_RANGE_URL % (
                full_name, data["before"], data["commits"][-1]["id"])
        single_url = COMMIT_URL % (full_name, "%s")
        return self._format_push(branch, author, data["commits"],
                                 data.get("forced", False), single_url, range_url)

    def _commit_comment(self, full_name, data):
        action = data["action"]
        commit = self._short(data["comment"]["commit_id"])
        commenter = self._bold(data["comment"]["user"]["login"])
        url = data["comment"]["html_url"]
        return [("[commit/%s] %s %s a comment" % (commit, commenter, action), url)]

    def _pull_request(self, full_name, data):
        pr = data["pull_request"]
        raw_number = pr["number"]
        number = self._color_id("#%s" % raw_number)
        branch = self._color_branch(pr["base"]["ref"])
        sender = self._bold(data["sender"]["login"])
        author = self._bold(pr["user"]["login"])
        action = data["action"]
        identifier = "%s by %s" % (number, author)
        action_desc = "%s %s" % (action, identifier)

        if action == "opened":
            action_desc = "requested %s merge into %s" % (number, branch)
        elif action == "closed":
            if pr.get("merged"):
                action_desc = "%s %s into %s" % (self._pos("merged"), identifier, branch)
            else:
                action_desc = "%s %s" % (self._neg("closed"), identifier)
        elif action == "ready_for_review":
            action_desc = "marked %s ready for review" % number
        elif action == "synchronize":
            action_desc = "committed to %s" % identifier
        elif action == "labeled" and "label" in data:
            action_desc = "labeled %s as '%s'" % (identifier, data["label"]["name"])
        elif action == "edited" and "title" in data.get("changes", {}):
            action_desc = "renamed %s" % identifier

        url = pr["html_url"]
        return [("[PR] %s %s: %s" % (sender, action_desc, pr["title"]), url)]

    def _pull_request_review(self, full_name, data):
        if data.get("action") != "submitted":
            return []
        review = data.get("review", {})
        if not review.get("submitted_at"):
            return []
        state = review.get("state", "")
        if state == "commented":
            return []
        number = self._color_id("#%s" % data["pull_request"]["number"])
        reviewer = self._bold(data["sender"]["login"])
        url = review.get("html_url", "")
        state_desc = {
            "approved":           "approved changes",
            "changes_requested":  "requested changes",
            "dismissed":          "dismissed a review",
        }.get(state, state)
        return [("[PR] %s %s on %s: %s"
            % (reviewer, state_desc, number, data["pull_request"]["title"]), url)]

    def _pull_request_review_comment(self, full_name, data):
        number = self._color_id("#%s" % data["pull_request"]["number"])
        action = data["action"]
        sender = self._bold(data["sender"]["login"])
        url = data["comment"]["html_url"]
        return [("[PR] %s %s on a review on %s: %s"
            % (sender, COMMENT_ACTIONS.get(action, action),
               number, data["pull_request"]["title"]), url)]

    def _issues(self, full_name, data):
        number = self._color_id("#%s" % data["issue"]["number"])
        action = data["action"]
        action_str = "%s %s" % (action, number)
        if action == "labeled" and "label" in data:
            action_str = "labeled %s as '%s'" % (number, data["label"]["name"])
        elif action == "edited" and "title" in data.get("changes", {}):
            action_str = "renamed %s" % number
        author = self._bold(data["sender"]["login"])
        url = data["issue"]["html_url"]
        return [("[issue] %s %s: %s" % (author, action_str, data["issue"]["title"]), url)]

    def _issue_comment(self, full_name, data):
        if "changes" in data:
            if data["changes"].get("body", {}).get("from") == data["comment"]["body"]:
                return []
        number = self._color_id("#%s" % data["issue"]["number"])
        action = data["action"]
        type_ = "PR" if "pull_request" in data["issue"] else "issue"
        commenter = self._bold(data["sender"]["login"])
        url = data["comment"]["html_url"]
        body = ""
        if action != "deleted":
            body = ": %s" % self._comment(data["comment"]["body"])
        return [("[%s] %s %s on %s (%s)%s"
            % (type_, commenter, COMMENT_ACTIONS.get(action, action),
               number, data["issue"]["title"], body), url)]

    def _create(self, full_name, data):
        ref = fmt.color(data["ref"], fmt.COLOR_BRANCH)
        sender = self._bold(data["sender"]["login"])
        url = CREATE_URL % (full_name, data["ref"])
        return [("%s created a %s: %s" % (sender, data["ref_type"], ref), url)]

    def _delete(self, full_name, data):
        ref = fmt.color(data["ref"], fmt.COLOR_BRANCH)
        sender = self._bold(data["sender"]["login"])
        return [("%s deleted a %s: %s" % (sender, data["ref_type"], ref), None)]

    def _release(self, full_name, data):
        action = data["action"]
        name = data["release"].get("name") or ""
        if name:
            name = ": %s" % name
        author = self._bold(data["release"]["author"]["login"])
        url = data["release"]["html_url"]
        return [("%s %s a release%s" % (author, action, name), url)]

    def _check_suite(self, full_name, data):
        suite = data["check_suite"]
        commit = fmt.color(self._short(suite["head_sha"]), fmt.LIGHTBLUE)
        pr = ""
        url = ""
        if suite.get("pull_requests"):
            pr_num = suite["pull_requests"][0]["number"]
            pr = "/PR%s" % self._color_id("#%s" % pr_num)
            url = PR_URL % (full_name, pr_num)
        name = suite["app"]["name"]
        conclusion = suite.get("conclusion", "neutral")
        conclusion_str, conclusion_color = CHECK_SUITE_CONCLUSION.get(
            conclusion, ("finished", fmt.COLOR_NEUTRAL))
        conclusion_colored = fmt.color(conclusion_str, conclusion_color)
        return [("[build @%s%s] %s: %s" % (commit, pr, name, conclusion_colored), url)]

    def _fork(self, full_name, data):
        forker = self._bold(data["sender"]["login"])
        fork_name = fmt.color(data["forkee"]["full_name"], fmt.LIGHTBLUE)
        url = data["forkee"]["html_url"]
        return [("%s forked into %s" % (forker, fork_name), url)]

    def _membership(self, data):
        return [("%s %s %s to team %s" % (
            data["sender"]["login"], data["action"],
            data["member"]["login"], data["team"]["name"]), None)]

    def _watch(self, data):
        return [("%s starred the repository" % data["sender"]["login"], None)]
