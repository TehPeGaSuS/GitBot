"""GitHub webhook payload parser."""

from irc_format import (
    color, bold,
    COLOR_BRANCH, COLOR_ID, COLOR_POSITIVE, COLOR_NEGATIVE, COLOR_NEUTRAL,
    LIGHTBLUE, PURPLE, RED,
)

COMMIT_URL       = "https://github.com/%s/commit/%s"
COMMIT_RANGE_URL = "https://github.com/%s/compare/%s...%s"
CREATE_URL       = "https://github.com/%s/tree/%s"
PR_URL           = "https://github.com/%s/pull/%s"

COMMENT_MAX = 100
COMMENT_ACTIONS = {
    "created": "commented",
    "edited":  "edited a comment",
    "deleted": "deleted a comment",
}

EVENT_CATEGORIES = {
    "ping":    ["ping"],
    "code":    ["push", "commit_comment"],
    "pr-minimal": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened",
    ],
    "pr": [
        "pull_request/opened", "pull_request/closed", "pull_request/reopened",
        "pull_request/edited", "pull_request/assigned",
        "pull_request/unassigned", "pull_request_review",
        "pull_request/locked", "pull_request/unlocked",
        "pull_request_review_comment",
    ],
    "pr-all": ["pull_request", "pull_request_review", "pull_request_review_comment"],
    "issue-minimal": [
        "issues/opened", "issues/closed", "issues/reopened",
        "issues/deleted", "issues/transferred",
    ],
    "issue": [
        "issues/opened", "issues/closed", "issues/reopened", "issues/deleted",
        "issues/edited", "issues/assigned", "issues/unassigned",
        "issues/locked", "issues/unlocked", "issues/transferred",
        "issue_comment",
    ],
    "issue-all": ["issues", "issue_comment"],
    "repo": ["create", "delete", "release", "fork"],
    "star": ["watch"],
}


def _short(h):
    return h[:7]


def _comment(s):
    line = s.split("\n")[0].strip()
    left, right = line[:COMMENT_MAX], line[COMMENT_MAX:]
    if not right:
        return left
    if " " in left:
        left = left.rsplit(" ", 1)[0]
    return f"{left}[...]"


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
    ev = headers.get("X-GitHub-Event", "")
    action = data.get("action")
    category = None
    if "review" in data and "state" in data.get("review", {}):
        category = f"{ev}+{data['review']['state']}"
    elif "check_suite" in data and "conclusion" in data.get("check_suite", {}):
        category = f"{ev}+{data['check_suite']['conclusion']}"
    parts = [ev]
    if action:
        parts.append(f"{ev}/{action}")
    if category:
        parts.append(category)
        if action:
            parts.append(f"{category}/{action}")
    return parts


def event_categories(ev):
    return EVENT_CATEGORIES.get(ev, [ev])


def parse(full_name, ev, data, headers, commit_limit=3):
    """Return list of (message, url) tuples."""
    dispatch = {
        "push":                       lambda fn, d: _push(fn, d, commit_limit),
        "commit_comment":             _commit_comment,
        "pull_request":               _pull_request,
        "pull_request_review":        _pr_review,
        "pull_request_review_comment": _pr_review_comment,
        "issue_comment":              _issue_comment,
        "issues":                     _issues,
        "create":                     _create,
        "delete":                     _delete,
        "release":                    _release,
        "fork":                       _fork,
        "ping":                       lambda fn, d: [("Received new webhook", None)],
        "watch":                      lambda fn, d: [(f"{d['sender']['login']} starred the repository", None)],
        "membership":                 lambda fn, d: [(f"{d['sender']['login']} {d['action']} {d['member']['login']} to team {d['team']['name']}", None)],
    }
    fn = dispatch.get(ev)
    if fn:
        return fn(full_name, data)
    return []


def _push(full_name, data, commit_limit=3):
    branch_str = color(data["ref"].split("/", 2)[2], COLOR_BRANCH)
    author     = bold(data["pusher"]["name"])
    forced     = data.get("forced", False)
    commits    = data.get("commits", [])
    forced_str = f"{color('force', RED)} " if forced else ""

    if not commits and forced:
        return [(f"{author} {forced_str}pushed to {branch_str}", None)]

    range_url = None
    if commits:
        range_url = COMMIT_RANGE_URL % (full_name, data["before"], commits[-1]["id"])

    n = len(commits)

    # Single commit: one clean line with hash, branch and message
    if n == 1:
        c   = commits[0]
        h   = color(_short(c["id"]), COLOR_ID)
        msg = c["message"].split("\n")[0].strip()
        url = COMMIT_URL % (full_name, c["id"])
        return [(f"{author} {forced_str}pushed {h} to {branch_str}: {msg}", url)]

    # Multiple commits: summary line + individual lines + optional hidden count
    outputs = [(f"{author} {forced_str}pushed {n} commits to {branch_str}", range_url)]
    shown   = commits[:commit_limit]
    for c in shown:
        msg = c["message"].split("\n")[0].strip()
        outputs.append((f"{author} {_short(c['id'])} - {msg}", None))
    hidden = n - len(shown)
    if hidden > 0:
        outputs.append((f"(+{hidden} hidden commit{'s' if hidden != 1 else ''})", None))
    return outputs


def _commit_comment(full_name, data):
    action = data["action"]
    commit = _short(data["comment"]["commit_id"])
    commenter = bold(data["comment"]["user"]["login"])
    url = data["comment"]["html_url"]
    return [(f"[commit/{commit}] {commenter} {action} a comment", url)]


def _pull_request(full_name, data):
    pr = data["pull_request"]
    raw_num = pr["number"]
    num = color(f"#{raw_num}", COLOR_ID)
    author = bold(pr["user"]["login"])
    sender = bold(data["sender"]["login"])
    branch_str = color(pr["base"]["ref"], COLOR_BRANCH)
    action = data["action"]
    title = pr["title"]
    url = pr["html_url"]

    if action == "opened":
        desc = f"requested {num} merge into {branch_str}"
    elif action == "closed":
        if pr.get("merged"):
            desc = f"{color('merged', COLOR_POSITIVE)} {num} by {author} into {branch_str}"
        else:
            desc = f"{color('closed', COLOR_NEGATIVE)} {num} by {author}"
    elif action == "ready_for_review":
        desc = f"marked {num} ready for review"
    elif action == "synchronize":
        desc = f"committed to {num} by {author}"
    elif action == "labeled":
        desc = f"labeled {num} as '{data['label']['name']}'"
    elif action == "edited" and "title" in data.get("changes", {}):
        desc = f"renamed {num}"
    else:
        desc = f"{action} {num} by {author}"

    return [(f"[PR] {sender} {desc}: {title}", url)]


def _pr_review(full_name, data):
    if data["action"] != "submitted":
        return []
    review = data["review"]
    if "submitted_at" not in review:
        return []
    state = review["state"]
    if state == "commented":
        return []
    num = color(f"#{data['pull_request']['number']}", COLOR_ID)
    title = data["pull_request"]["title"]
    reviewer = bold(data["sender"]["login"])
    url = review["html_url"]
    state_map = {
        "approved": "approved changes",
        "changes_requested": "requested changes",
        "dismissed": "dismissed a review",
    }
    return [(f"[PR] {reviewer} {state_map.get(state, state)} on {num}: {title}", url)]


def _pr_review_comment(full_name, data):
    num = color(f"#{data['pull_request']['number']}", COLOR_ID)
    action = data["action"]
    title = data["pull_request"]["title"]
    sender = bold(data["sender"]["login"])
    url = data["comment"]["html_url"]
    return [(f"[PR] {sender} {COMMENT_ACTIONS[action]} on a review on {num}: {title}", url)]


def _issues(full_name, data):
    num = color(f"#{data['issue']['number']}", COLOR_ID)
    action = data["action"]
    if action == "labeled":
        action_str = f"labeled {num} as '{data['label']['name']}'"
    elif action == "edited" and "title" in data.get("changes", {}):
        action_str = f"renamed {num}"
    else:
        action_str = f"{action} {num}"
    author = bold(data["sender"]["login"])
    title = data["issue"]["title"]
    url = data["issue"]["html_url"]
    return [(f"[issue] {author} {action_str}: {title}", url)]


def _issue_comment(full_name, data):
    if "changes" in data:
        if data["changes"].get("body", {}).get("from") == data["comment"]["body"]:
            return []
    num = color(f"#{data['issue']['number']}", COLOR_ID)
    action = data["action"]
    title = data["issue"]["title"]
    type_ = "PR" if "pull_request" in data["issue"] else "issue"
    commenter = bold(data["sender"]["login"])
    url = data["comment"]["html_url"]
    body = f": {_comment(data['comment']['body'])}" if action != "deleted" else ""
    return [(f"[{type_}] {commenter} {COMMENT_ACTIONS[action]} on {num} ({title}){body}", url)]


def _create(full_name, data):
    ref = color(data["ref"], COLOR_BRANCH)
    sender = bold(data["sender"]["login"])
    url = CREATE_URL % (full_name, data["ref"])
    return [(f"{sender} created a {data['ref_type']}: {ref}", url)]


def _delete(full_name, data):
    ref = color(data["ref"], COLOR_BRANCH)
    sender = bold(data["sender"]["login"])
    return [(f"{sender} deleted a {data['ref_type']}: {ref}", None)]


def _release(full_name, data):
    action = data["action"]
    name = data["release"].get("name") or ""
    if name:
        name = f": {name}"
    author = bold(data["release"]["author"]["login"])
    url = data["release"]["html_url"]
    return [(f"{author} {action} a release{name}", url)]


def _fork(full_name, data):
    forker = bold(data["sender"]["login"])
    fork_name = color(data["forkee"]["full_name"], LIGHTBLUE)
    url = data["forkee"]["html_url"]
    return [(f"{forker} forked into {fork_name}", url)]
