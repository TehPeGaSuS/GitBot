"""
GitLab webhook payload handler.
Ported from bitbot's modules/git_webhooks/gitlab.py
"""

from src import formatting as fmt

EVENT_CATEGORIES = {
    "ping":  ["ping"],
    "code":  ["push"],
    "pr-minimal": [
        "merge_request/open", "merge_request/close",
        "merge_request/reopen", "merge_request/merge",
    ],
    "pr": [
        "merge_request/open", "merge_request/close",
        "merge_request/reopen", "merge_request/update",
        "merge_request/merge", "note+mergerequest",
        "confidential_note+mergerequest",
    ],
    "pr-all": [
        "merge_request", "note+mergerequest",
        "confidential_note+mergerequest",
    ],
    "issue-minimal": [
        "issue/open", "issue/close", "issue/reopen",
        "confidential_issue/open", "confidential_issue/close",
        "confidential_issue/reopen",
    ],
    "issue": [
        "issue/open", "issue/close", "issue/reopen", "issue/update",
        "confidential_issue/open", "confidential_issue/close",
        "confidential_issue/reopen", "confidential_issue/update",
        "note+issue", "confidential_note+issue",
    ],
    "issue-all": [
        "issue", "confidential_issue",
        "note+issue", "confidential_note+issue",
    ],
    "repo": ["tag_push"],
}

ISSUE_ACTIONS = {
    "open":   "opened",
    "close":  "closed",
    "reopen": "reopened",
    "update": "updated",
    "merge":  "merged",
}
COMMENT_ACTIONS = {
    "created": "commented",
    "edited":  "edited a comment",
    "deleted": "deleted a comment",
}
MR_ACTIONS = {
    "open":   "opened",
    "close":  "closed",
    "reopen": "reopened",
    "update": "updated",
    "merge":  "merged",
}


class GitLab:
    def is_private(self, data, headers):
        proj = data.get("project", {})
        return proj.get("visibility_level", 0) != 20

    def names(self, data, headers):
        full_name = repo_username = repo_name = organisation = None
        proj = data.get("project") or data.get("repository", {})
        if proj:
            full_name = proj.get("path_with_namespace") or proj.get("name", "")
            if "/" in full_name:
                repo_username, repo_name = full_name.split("/", 1)
        return full_name, repo_username, repo_name, organisation

    def branch(self, data, headers):
        ref = data.get("ref")
        if ref:
            return ref.rpartition("/")[2]
        return None

    def event(self, data, headers):
        obj_kind = data.get("object_kind", "")
        action = data.get("object_attributes", {}).get("action")

        # Notes (comments)
        noteable = data.get("object_attributes", {}).get("noteable_type", "")
        if obj_kind == "note" and noteable:
            noteable_map = {
                "MergeRequest":  "mergerequest",
                "Issue":         "issue",
                "Commit":        "commit",
                "Snippet":       "snippet",
            }
            suffix = noteable_map.get(noteable, noteable.lower())
            event = "note+%s" % suffix
            return [event]

        event_action = "%s/%s" % (obj_kind, action) if action else None
        return [obj_kind] + ([event_action] if event_action else [])

    def event_categories(self, event):
        return EVENT_CATEGORIES.get(event, [event])

    def webhook(self, full_name, event, data, headers):
        obj_kind = data.get("object_kind", "")
        dispatch = {
            "push":          lambda: self._push(full_name, data),
            "tag_push":      lambda: self._tag_push(full_name, data),
            "merge_request": lambda: self._merge_request(full_name, data),
            "issue":         lambda: self._issue(full_name, data),
            "confidential_issue": lambda: self._issue(full_name, data),
        }
        # Notes
        if obj_kind == "note":
            return self._note(full_name, data)
        if obj_kind == "confidential_note":
            return self._note(full_name, data)

        fn = dispatch.get(obj_kind)
        return fn() if fn else []

    def _short(self, h): return h[:8] if h else "?"

    def _push(self, full_name, data):
        outputs = []
        branch = fmt.color(data["ref"].split("/", 2)[-1], fmt.COLOR_BRANCH)
        author = fmt.bold(data.get("user_name", "unknown"))
        commits = data.get("commits", [])
        if len(commits) <= 3:
            for c in commits:
                h = fmt.color(self._short(c["id"]), fmt.COLOR_ID)
                msg = c["message"].split("\n")[0].strip()
                outputs.append(("%s pushed %s to %s: %s"
                    % (author, h, branch, msg), c.get("url")))
        else:
            url = data.get("compare", "")
            outputs.append(("%s pushed %d commits to %s"
                % (author, len(commits), branch), url))
        return outputs

    def _tag_push(self, full_name, data):
        tag = fmt.color(data["ref"].rpartition("/")[2], fmt.COLOR_BRANCH)
        author = fmt.bold(data.get("user_name", "unknown"))
        before = data.get("before", "0" * 40)
        after  = data.get("after",  "0" * 40)
        zero = "0" * 40
        if before == zero and after != zero:
            action = "created tag"
        elif before != zero and after == zero:
            action = "deleted tag"
        else:
            action = "pushed tag"
        return [("%s %s %s" % (author, action, tag), None)]

    def _merge_request(self, full_name, data):
        attrs = data.get("object_attributes", {})
        number = fmt.color("!%s" % attrs.get("iid", "?"), fmt.COLOR_ID)
        action = attrs.get("action", "")
        action_str = MR_ACTIONS.get(action, action)
        branch = fmt.color(attrs.get("target_branch", ""), fmt.COLOR_BRANCH)
        author = fmt.bold(data.get("user", {}).get("name", "unknown"))
        title = attrs.get("title", "")
        url = attrs.get("url", "")

        if action == "open":
            desc = "requested %s merge into %s" % (number, branch)
        elif action == "merge":
            desc = "%s %s into %s" % (
                fmt.color("merged", fmt.COLOR_POSITIVE), number, branch)
        elif action == "close":
            desc = "%s %s" % (fmt.color("closed", fmt.COLOR_NEGATIVE), number)
        else:
            desc = "%s %s" % (action_str, number)

        return [("[MR] %s %s: %s" % (author, desc, title), url)]

    def _issue(self, full_name, data):
        attrs = data.get("object_attributes", {})
        number = fmt.color("#%s" % attrs.get("iid", "?"), fmt.COLOR_ID)
        action = ISSUE_ACTIONS.get(attrs.get("action", ""), attrs.get("action", ""))
        author = fmt.bold(data.get("user", {}).get("name", "unknown"))
        title = attrs.get("title", "")
        url = attrs.get("url", "")
        return [("[issue] %s %s %s: %s" % (author, action, number, title), url)]

    def _note(self, full_name, data):
        attrs = data.get("object_attributes", {})
        noteable = attrs.get("noteable_type", "")
        author = fmt.bold(data.get("user", {}).get("name", "unknown"))
        url = attrs.get("url", "")
        note = attrs.get("note", "")[:100]

        label = {
            "MergeRequest": "MR",
            "Issue":        "issue",
            "Commit":       "commit",
        }.get(noteable, noteable)

        ref = ""
        if noteable == "MergeRequest":
            mr = data.get("merge_request", {})
            ref = fmt.color("!%s" % mr.get("iid", "?"), fmt.COLOR_ID)
        elif noteable == "Issue":
            issue = data.get("issue", {})
            ref = fmt.color("#%s" % issue.get("iid", "?"), fmt.COLOR_ID)
        elif noteable == "Commit":
            ref = fmt.color(self._short(attrs.get("commit_id", "")), fmt.COLOR_ID)

        return [("[%s] %s commented on %s: %s" % (label, author, ref, note), url)]
