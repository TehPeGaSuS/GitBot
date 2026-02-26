"""Gitea webhook payload parser."""

from irc_format import (
    color, bold,
    COLOR_BRANCH, COLOR_ID, COLOR_POSITIVE, COLOR_NEGATIVE,
    LIGHTBLUE,
)

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

EVENT_CATEGORIES = {
    "ping":  ["ping"],
    "code":  ["push"],
    "pr-minimal": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened",
    ],
    "pr": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened",
        "pull_request/edited", "pull_request/assigned", "pull_request/unassigned",
    ],
    "pr-all": ["pull_request"],
    "issue-minimal": [
        "issues/opened", "issues/closed", "issues/reopened", "issues/deleted",
    ],
    "issue": [
        "issues/opened", "issues/closed", "issues/reopened", "issues/deleted",
        "issues/edited", "issues/assigned", "issues/unassigned", "issue_comment",
    ],
    "issue-all": ["issues", "issue_comment"],
    "repo": ["create", "delete", "release", "fork", "repository"],
}


def _short(h):
    return h[:7]


def names(data, headers):
    full_name = repo_user = repo_name = organisation = None
    if "repository" in data:
        full_name = data["repository"]["full_name"]
        repo_user, repo_name = full_name.split("/", 1)
    if "organization" in data:
        organisation = data["organization"]["login"]
    return full_name, repo_user, repo_name, organisation


def branch(data, headers):
    if "ref" in data:
        return data["ref"].rpartition("/")[2]
    return None


def is_private(data, headers):
    return data.get("repository", {}).get("private", False)


def event(data, headers):
    ev = headers.get("X-Gitea-Event", "")
    action = data.get("action")
    parts = [ev]
    if action:
        parts.append(f"{ev}/{action}")
    return parts


def event_categories(ev):
    return EVENT_CATEGORIES.get(ev, [ev])


def parse(full_name, ev, data, headers):
    dispatch = {
        "push":             _push,
        "pull_request":     _pull_request,
        "issues":           _issues,
        "issue_comment":    _issue_comment,
        "create":           _create,
        "delete":           _delete,
        "repository":       lambda fn, d: [],
        "release":          _release,
        "fork":             _fork,
        "ping":             lambda fn, d: [("Received new webhook", None)],
    }
    fn = dispatch.get(ev)
    if fn:
        return fn(full_name, data)
    return []


def _push(full_name, data):
    branch_str = color(data["ref"].rpartition("/")[2], COLOR_BRANCH)
    author = bold(data["pusher"]["login"])
    commits = data.get("commits", [])
    outputs = []
    if len(commits) <= 3:
        for c in commits:
            h = color(_short(c["id"]), COLOR_ID)
            msg = c["message"].split("\n")[0].strip()
            outputs.append((f"{author} pushed {h} to {branch_str}: {msg}", c["url"]))
    else:
        url = data.get("compare_url")
        outputs.append((f"{author} pushed {len(commits)} commits to {branch_str}", url))
    return outputs


def _pull_request(full_name, data):
    pr = data["pull_request"]
    num = color(f"#{pr['number']}", COLOR_ID)
    action = data["action"]
    branch_str = color(pr["base"]["ref"], COLOR_BRANCH)
    author = bold(data["sender"]["login"])
    title = pr["title"]
    url = pr["html_url"]

    if action == "opened":
        desc = f"requested {num} merge into {branch_str}"
    elif action == "closed":
        if pr.get("merged"):
            desc = f"{color('merged', COLOR_POSITIVE)} {num} into {branch_str}"
        else:
            desc = f"{color('closed', COLOR_NEGATIVE)} {num}"
    elif action == "ready_for_review":
        desc = f"marked {num} ready for review"
    elif action == "synchronize":
        desc = f"committed to {num}"
    else:
        desc = f"{action} {num}"

    return [(f"[PR] {author} {desc}: {title}", url)]


def _issues(full_name, data):
    num = color(f"#{data['issue']['number']}", COLOR_ID)
    action = data["action"]
    title = data["issue"]["title"]
    author = bold(data["sender"]["login"])
    url = f"{data['repository']['html_url']}/issues/{data['issue']['number']}"
    return [(f"[issue] {author} {action} {num}: {title}", url)]


def _issue_comment(full_name, data):
    if "changes" in data:
        if data["changes"].get("body", {}).get("from") == data["comment"]["body"]:
            return []
    num = color(f"#{data['issue']['number']}", COLOR_ID)
    action = data["action"]
    title = data["issue"]["title"]
    type_ = "PR" if data["issue"].get("pull_request") else "issue"
    commenter = bold(data["sender"]["login"])
    url = data["comment"]["html_url"]
    return [(f"[{type_}] {commenter} {COMMENT_ACTIONS[action]} on {num}: {title}", url)]


def _create(full_name, data):
    ref = color(data["ref"], COLOR_BRANCH)
    sender = bold(data["sender"]["login"])
    return [(f"{sender} created a {data['ref_type']}: {ref}", None)]


def _delete(full_name, data):
    ref = color(data["ref"], COLOR_BRANCH)
    sender = bold(data["sender"]["login"])
    return [(f"{sender} deleted a {data['ref_type']}: {ref}", None)]


def _release(full_name, data):
    action = RELEASE_ACTIONS.get(data["action"], data["action"])
    name = data["release"].get("name") or ""
    if name:
        name = f": {name}"
    author = bold(data["release"]["author"]["login"])
    return [(f"{author} {action} a release{name}", None)]


def _fork(full_name, data):
    forker = bold(data["sender"]["login"])
    fork_name = color(data["repository"]["full_name"], LIGHTBLUE)
    url = data["repository"]["html_url"]
    return [(f"{forker} forked into {fork_name}", url)]
