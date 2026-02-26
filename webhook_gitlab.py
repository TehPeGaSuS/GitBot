"""GitLab webhook payload parser."""

from irc_format import (
    color, bold,
    COLOR_BRANCH, COLOR_ID, COLOR_POSITIVE, COLOR_NEGATIVE,
)

EVENT_CATEGORIES = {
    "ping":  ["ping"],
    "code":  ["push"],
    "pr-minimal": [
        "merge_request/open", "merge_request/close",
        "merge_request/reopen", "merge_request/merge",
    ],
    "pr": [
        "merge_request/open", "merge_request/close",
        "merge_request/reopen", "merge_request/update", "merge_request/merge",
        "note+mergerequest", "confidential_note+mergerequest",
    ],
    "pr-all": ["merge_request", "note+mergerequest", "confidential_note+mergerequest"],
    "issue-minimal": [
        "issue/open", "issue/close", "issue/reopen",
        "confidential_issue/open", "confidential_issue/close", "confidential_issue/reopen",
    ],
    "issue": [
        "issue/open", "issue/close", "issue/reopen", "issue/update",
        "confidential_issue/open", "confidential_issue/close",
        "confidential_issue/reopen", "confidential_issue/update",
        "note+issue", "confidential_note+issue",
    ],
    "issue-all": ["issue", "confidential_issue", "note+issue", "confidential_note+issue"],
    "repo": ["tag_push"],
}

ISSUE_ACTIONS = {
    "open": "opened", "close": "closed",
    "reopen": "reopened", "update": "updated", "merge": "merged",
}
WIKI_ACTIONS = {
    "create": "created", "update": "updated", "delete": "deleted",
}


def _short(h):
    return h[:7]


def names(data, headers):
    if "project" in data:
        full_name = data["project"]["path_with_namespace"]
    else:
        full_name = data.get("project_name", "").replace(" ", "")
    parts = full_name.split("/", 1)
    repo_user = parts[0] if parts else ""
    repo_name = parts[1] if len(parts) > 1 else ""
    organisation = None
    if full_name.count("/") == 2:
        organisation = repo_user
        repo_user = full_name.rsplit("/", 1)[0]
    return full_name, repo_user, repo_name, organisation


def branch(data, headers):
    if "ref" in data:
        return data["ref"].rpartition("/")[2]
    return None


def is_private(data, headers):
    project = data.get("project", {})
    return project.get("visibility_level", 0) != 20


def event(data, headers):
    ev = headers.get("X-GitLab-Event", "").rsplit(" ", 1)[0].lower().replace(" ", "_")
    action = None
    category = None
    oa = data.get("object_attributes", {})
    if "action" in oa:
        action = oa["action"]
    if "noteable_type" in oa:
        nt = oa["noteable_type"].lower()
        category = f"{ev}+{nt}"
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
    dispatch = {
        "push": lambda fn, d: _push(fn, d, commit_limit),
        "tag_push":          _tag_push,
        "merge_request":     _merge_request,
        "issue":             _issues,
        "confidential_issue": _issues,
        "note":              _note,
        "confidential_note": _note,
        "wiki_page":         lambda fn, d: _wiki(d),
    }
    fn = dispatch.get(ev)
    if fn:
        return fn(full_name, data)
    return []


def _push(full_name, data, commit_limit=3):
    branch_str = color(data["ref"].rpartition("/")[2], COLOR_BRANCH)
    author     = bold(data["user_username"])
    commits    = data.get("commits", [])
    n          = len(commits)

    if not commits:
        return [(f"{author} pushed to {branch_str}", None)]

    # Single commit: one clean line
    if n == 1:
        c   = commits[0]
        h   = color(_short(c["id"]), COLOR_ID)
        msg = c["message"].split("\n")[0].strip()
        return [(f"{author} pushed {h} to {branch_str}: {msg}", c.get("url"))]

    # Multiple commits (GitLab has no compare URL in push payloads)
    outputs = [(f"{author} pushed {n} commits to {branch_str}", None)]
    shown   = commits[:commit_limit]
    for c in shown:
        msg = c["message"].split("\n")[0].strip()
        outputs.append((f"{author} {_short(c['id'])} - {msg}", c.get("url")))
    hidden = n - len(shown)
    if hidden > 0:
        outputs.append((f"(+{hidden} hidden commit{'s' if hidden != 1 else ''})", None))
    return outputs


def _tag_push(full_name, data):
    after = data.get("after", "")
    create = bool(after.strip("0"))
    tag = color(data["ref"].rsplit("/", 1)[-1], COLOR_BRANCH)
    author = bold(data["user_username"])
    action = "created" if create else "deleted"
    return [(f"{author} {action} a tag: {tag}", None)]


def _merge_request(full_name, data):
    oa = data["object_attributes"]
    num = color(f"!{oa['iid']}", COLOR_ID)
    action = oa["action"]
    branch_str = color(oa["target_branch"], COLOR_BRANCH)
    author = bold(data["user"]["username"])
    title = oa["title"]
    url = oa["url"]

    if action == "open":
        desc = f"requested {num} merge into {branch_str}"
    elif action == "close":
        desc = f"{color('closed', COLOR_NEGATIVE)} {num}"
    elif action == "merge":
        desc = f"{color('merged', COLOR_POSITIVE)} {num} into {branch_str}"
    else:
        desc = f"{ISSUE_ACTIONS.get(action, action)} {num}"

    return [(f"[MR] {author} {desc}: {title}", url)]


def _issues(full_name, data):
    oa = data["object_attributes"]
    if "action" not in oa:
        return []
    num = color(f"#{oa['iid']}", COLOR_ID)
    action = ISSUE_ACTIONS.get(oa["action"], oa["action"])
    title = oa["title"]
    author = bold(data["user"]["username"])
    url = oa["url"]
    return [(f"[issue] {author} {action} {num}: {title}", url)]


def _note(full_name, data):
    oa = data["object_attributes"]
    type_ = oa.get("noteable_type", "")
    if type_ == "Issue" and "issue" in data:
        obj = data["issue"]
        label = "issue"
    elif type_ == "MergeRequest" and "merge_request" in data:
        obj = data["merge_request"]
        label = "MR"
    else:
        return []
    num = color(f"#{obj['iid']}", COLOR_ID)
    title = obj["title"]
    commenter = bold(data["user"]["username"])
    url = oa["url"]
    return [(f"[{label}] {commenter} commented on {num}: {title}", url)]


def _wiki(data):
    oa = data["object_attributes"]
    author = bold(data["user"]["username"])
    action = WIKI_ACTIONS.get(oa["action"], oa["action"])
    title = oa["title"]
    url = oa["url"]
    return [(f"{author} {action} a wiki page: {title}", url)]
