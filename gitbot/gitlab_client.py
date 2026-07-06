"""GitLab API helpers - post comments, fetch diffs, etc."""

import re

import gitlab

from gitbot.config import settings

_gl: gitlab.Gitlab | None = None

# Anything shaped like a GitLab PAT / bot token. git subprocess errors embed
# the authenticated clone URL (https://oauth2:<token>@host/...), so error text
# posted to GitLab could leak the bot's own credential. Redact at the post
# boundary — belt and suspenders over careful per-call handling.
_TOKEN_RE = re.compile(r"glpat-[A-Za-z0-9_.\-]+|(?<=oauth2:)[A-Za-z0-9_.\-]+")


def redact(text: str) -> str:
    """Strip anything that looks like a bot token from user-facing text."""
    if not text:
        return text
    out = _TOKEN_RE.sub("***", text)
    tok = settings.gitlab_token
    if tok and tok in out:
        out = out.replace(tok, "***")
    return out


def get_client() -> gitlab.Gitlab:
    global _gl
    if _gl is None:
        _gl = gitlab.Gitlab(settings.gitlab_url, private_token=settings.gitlab_token, ssl_verify=settings.gitlab_ssl_verify)
    return _gl


def post_note_on_issue(project_id: int, issue_iid: int, body: str) -> int:
    """Post a note and return its ID (for later editing)."""
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    note = issue.notes.create({"body": redact(body)})
    return note.get_id() or note.attributes.get("id", 0)


def update_note_on_issue(project_id: int, issue_iid: int, note_id: int, body: str) -> None:
    """Edit an existing note in-place."""
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    note = issue.notes.get(note_id)
    note.body = redact(body)
    note.save()


def post_note_on_mr(project_id: int, mr_iid: int, body: str) -> int:
    """Post a note and return its ID (for later editing)."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    note = mr.notes.create({"body": redact(body)})
    return note.get_id() or note.attributes.get("id", 0)


def update_note_on_mr(project_id: int, mr_iid: int, note_id: int, body: str) -> None:
    """Edit an existing note in-place."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    note = mr.notes.get(note_id)
    note.body = redact(body)
    note.save()


def set_issue_labels(project_id: int, issue_iid: int, labels: list[str]) -> None:
    """Add labels to an issue (merges with existing)."""
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    existing = [l for l in (issue.labels or [])]
    merged = list(set(existing + labels))
    issue.labels = merged
    issue.save()


def remove_issue_labels(project_id: int, issue_iid: int, labels: list[str]) -> None:
    """Remove labels from an issue."""
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    issue.labels = [l for l in (issue.labels or []) if l not in labels]
    issue.save()


def start_discussion(
    project_id: int, noteable_type: str, noteable_iid: int, body: str
) -> tuple[str, int]:
    """Start a new discussion thread on an issue/MR.

    Returns (discussion_id, first_note_id). The first note anchors the
    session: edit it for status updates, reply to it for everything else.
    """
    gl = get_client()
    project = gl.projects.get(project_id)
    if noteable_type == "MergeRequest":
        noteable = project.mergerequests.get(noteable_iid)
    else:
        noteable = project.issues.get(noteable_iid)
    discussion = noteable.discussions.create({"body": redact(body)})
    first_note = discussion.attributes.get("notes", [{}])[0]
    return discussion.id, first_note.get("id", 0)


def reply_to_discussion(
    project_id: int, noteable_type: str, noteable_iid: int, discussion_id: str, body: str
) -> int:
    """Post a threaded reply in an existing discussion. Returns the new note id."""
    gl = get_client()
    project = gl.projects.get(project_id)
    if noteable_type == "MergeRequest":
        noteable = project.mergerequests.get(noteable_iid)
    else:
        noteable = project.issues.get(noteable_iid)
    discussion = noteable.discussions.get(discussion_id)
    note = discussion.notes.create({"body": redact(body)})
    return note.id


def create_mr_inline_discussion(
    project_id: int, mr_iid: int, body: str,
    new_path: str, new_line: int | None = None,
    old_path: str | None = None, old_line: int | None = None,
) -> str:
    """Start a discussion anchored to a specific line of the MR diff.

    new_line is the line number in the file AFTER the change (added/context
    lines); for a removed line pass old_line instead. The line must appear in
    the MR diff or GitLab rejects the position. Returns the discussion id.
    """
    gl = get_client()
    mr = gl.projects.get(project_id).mergerequests.get(mr_iid)
    refs = mr.diff_refs or {}
    position = {
        "position_type": "text",
        "base_sha": refs.get("base_sha"),
        "start_sha": refs.get("start_sha"),
        "head_sha": refs.get("head_sha"),
        "new_path": new_path,
        "old_path": old_path or new_path,
    }
    if new_line is not None:
        position["new_line"] = new_line
    if old_line is not None:
        position["old_line"] = old_line
    discussion = mr.discussions.create({"body": body, "position": position})
    return discussion.id


def get_mr_diff(project_id: int, mr_iid: int) -> str:
    """Get the diff of an MR as a unified diff string."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    changes = mr.changes()
    parts = []
    for change in changes.get("changes", []):
        parts.append(f"--- a/{change['old_path']}")
        parts.append(f"+++ b/{change['new_path']}")
        parts.append(change.get("diff", ""))
    return "\n".join(parts)


def get_file_content(project_id: int, file_path: str, ref: str = "main") -> str:
    """Fetch a file from the repo at a given ref."""
    gl = get_client()
    project = gl.projects.get(project_id)
    f = project.files.get(file_path=file_path, ref=ref)
    return f.decode().decode("utf-8")


def list_repo_tree(project_id: int, path: str = "", ref: str = "main") -> list[dict]:
    """List files/dirs in a repo path."""
    gl = get_client()
    project = gl.projects.get(project_id)
    return list(project.repository_tree(path=path, ref=ref, all=True))


def create_branch(project_id: int, branch_name: str, ref: str = "main") -> None:
    """Create a new branch from ref."""
    gl = get_client()
    project = gl.projects.get(project_id)
    project.branches.create({"branch": branch_name, "ref": ref})


def commit_files(
    project_id: int,
    branch: str,
    message: str,
    actions: list[dict],
) -> dict:
    """Create a commit with multiple file actions.

    Each action is a dict like:
        {"action": "create", "file_path": "path/to/file", "content": "..."}
    Valid actions: create, update, delete, move
    """
    gl = get_client()
    project = gl.projects.get(project_id)
    return project.commits.create({
        "branch": branch,
        "commit_message": message,
        "actions": actions,
    })


# ---------------------------------------------------------------------------
# Runner-project helpers for Claude Code in CI (#41)
# ---------------------------------------------------------------------------

def get_project(id_or_path: str) -> dict | None:
    """Resolve a project by numeric id or path/with/namespace. None if absent."""
    gl = get_client()
    try:
        p = gl.projects.get(id_or_path)
    except Exception:
        return None
    return {"id": p.id, "path_with_namespace": p.path_with_namespace,
            "default_branch": p.default_branch or "main", "web_url": p.web_url,
            "registry_prefix": (getattr(p, "container_registry_image_prefix", "") or "")}


def create_project(name: str, namespace_path: str | None = None) -> dict:
    """Create a project (the Claude CI runner project). `namespace_path` is a
    group path to create it under (required for tokens/service accounts that
    can't own personal-namespace projects); omit for the token's own space."""
    gl = get_client()
    attrs = {"name": name, "path": name, "visibility": "private",
             "description": "GitBot Claude Code CI runner (managed by GitBot)"}
    if namespace_path:
        attrs["namespace_id"] = gl.groups.get(namespace_path).id
    p = gl.projects.create(attrs)
    return {"id": p.id, "path_with_namespace": p.path_with_namespace,
            "default_branch": p.default_branch or "main", "web_url": p.web_url,
            "registry_prefix": (getattr(p, "container_registry_image_prefix", "") or "")}


def set_project_variable(project_id: int, key: str, value: str,
                         masked: bool = True) -> None:
    """Create or update a (masked) CI/CD variable on a project. Idempotent."""
    gl = get_client()
    project = gl.projects.get(project_id)
    # GitLab rejects masking values that don't meet its masking rules; fall
    # back to unmasked-but-still-protected rather than failing setup.
    for attempt_masked in (masked, False):
        data = {"value": value, "masked": attempt_masked, "protected": False}
        try:
            try:
                var = project.variables.get(key)
                for k, v in data.items():
                    setattr(var, k, v)
                var.save()
            except Exception:
                project.variables.create({"key": key, **data})
            return
        except Exception:
            if not attempt_masked:
                raise  # unmasked also failed — surface it


def list_project_runners(project_id: int) -> list[dict]:
    """Runners available to a project (its own + inherited group/instance)."""
    gl = get_client()
    project = gl.projects.get(project_id)
    runners = project.runners.list(get_all=True)
    return [{"id": r.id, "description": getattr(r, "description", ""),
             "active": getattr(r, "active", getattr(r, "paused", None) is False),
             "online": getattr(r, "online", None),
             "is_shared": getattr(r, "is_shared", None)} for r in runners]


def trigger_pipeline(project_id: int, ref: str, variables: dict) -> dict:
    """Trigger a pipeline on ref with variables. Returns id/status/web_url."""
    gl = get_client()
    project = gl.projects.get(project_id)
    var_list = [{"key": k, "value": str(v)} for k, v in variables.items()]
    pipeline = project.pipelines.create({"ref": ref, "variables": var_list})
    return {"id": pipeline.id, "status": pipeline.status, "web_url": pipeline.web_url}


def create_merge_request(
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
) -> dict:
    """Create an MR and return its attributes."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.create({
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
    })
    return {"iid": mr.iid, "web_url": mr.web_url}


def assign_mr(project_id: int, mr_iid: int, user_ids: list[int]) -> None:
    """Assign users to an MR."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    mr.assignee_ids = user_ids
    mr.save()


def get_bot_user_id() -> int:
    """Get the bot's own GitLab user ID."""
    gl = get_client()
    return gl.user.id


def get_mr_details(project_id: int, mr_iid: int) -> dict:
    """Get MR details including source branch, author, and assignees."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    return {
        "iid": mr.iid,
        "title": mr.title,
        "source_branch": mr.source_branch,
        "target_branch": mr.target_branch,
        "author": mr.author.get("username") if isinstance(mr.author, dict) else "?",
        "assignees": [a.get("username") for a in (mr.assignees or [])],
        "reviewers": [r.get("username") for r in (getattr(mr, "reviewers", None) or [])],
        "state": mr.state,
        "web_url": mr.web_url,
    }


def get_issue_details(project_id: int, issue_iid: int) -> dict:
    """Issue author + assignees (for the comment-follow trigger gate, #40)."""
    gl = get_client()
    issue = gl.projects.get(project_id).issues.get(issue_iid)
    return {
        "iid": issue.iid,
        "author": issue.author.get("username") if isinstance(issue.author, dict) else "?",
        "assignees": [a.get("username") for a in (issue.assignees or [])],
        "state": issue.state,
    }


def get_related_mrs(project_id: int, issue_iid: int) -> list[dict]:
    """Get MRs related to an issue (linked via 'Closes #N' or manual link)."""
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    mrs = issue.related_merge_requests()
    return [
        {
            "iid": mr["iid"],
            "title": mr["title"],
            "state": mr["state"],
            "source_branch": mr["source_branch"],
            "web_url": mr["web_url"],
            "author": mr.get("author", {}).get("username", "?"),
        }
        for mr in mrs
    ]


def get_closing_mrs(project_id: int, issue_iid: int) -> list[dict]:
    """Get MRs that will close this issue when merged."""
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    mrs = issue.closed_by()
    return [
        {
            "iid": mr["iid"],
            "title": mr["title"],
            "state": mr["state"],
            "web_url": mr["web_url"],
        }
        for mr in mrs
    ]


def get_pending_todos() -> list[dict]:
    """Get all pending todos for the bot user."""
    gl = get_client()
    todos = gl.todos.list(state="pending", get_all=True)
    return [
        {
            "id": t.id,
            "action": t.action_name,
            "target_type": t.target_type,
            "target_iid": t.target.get("iid") if isinstance(t.target, dict) else getattr(t.target, "iid", None),
            "target_title": t.target.get("title") if isinstance(t.target, dict) else getattr(t.target, "title", None),
            "project_id": t.project.get("id") if isinstance(t.project, dict) else getattr(t.project, "id", None),
            "body": t.body,
            "created_at": t.created_at,
            "author": (t.author or {}).get("username") if isinstance(t.author, dict) else None,
        }
        for t in todos
    ]


def get_done_todos(limit: int = 100) -> list[dict]:
    """Recently completed todos for the bot user (newest first) — the deep
    audit's input (#30): a DONE mention todo that nobody actually handled is
    the only trace of an invisibly lost callout."""
    gl = get_client()
    todos = gl.todos.list(state="done", per_page=limit, get_all=False)
    return [
        {
            "id": t.id,
            "action": t.action_name,
            "target_type": t.target_type,
            "target_iid": t.target.get("iid") if isinstance(t.target, dict) else getattr(t.target, "iid", None),
            "target_title": t.target.get("title") if isinstance(t.target, dict) else getattr(t.target, "title", None),
            "project_id": t.project.get("id") if isinstance(t.project, dict) else getattr(t.project, "id", None),
            "body": t.body,
            "created_at": t.created_at,
            "author": (t.author or {}).get("username") if isinstance(t.author, dict) else None,
        }
        for t in todos
    ]


def mark_todo_done(todo_id: int) -> None:
    """Mark a todo as done."""
    gl = get_client()
    gl.http_post(f"/todos/{todo_id}/mark_as_done")


def set_mr_labels(project_id: int, mr_iid: int, labels: list[str]) -> None:
    """Add labels to an MR (merges with existing)."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    existing = [l for l in (mr.labels or [])]
    merged = list(set(existing + labels))
    mr.labels = merged
    mr.save()


def remove_mr_labels(project_id: int, mr_iid: int, labels: list[str]) -> None:
    """Remove labels from an MR."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    mr.labels = [l for l in (mr.labels or []) if l not in labels]
    mr.save()


# ---------------------------------------------------------------------------
# Group webhook management (admin onboarding — issue #35)
#
# Lets the admin panel discover the groups a token owns and turn GitBot on/off
# per group by creating/deleting the group webhook itself. GitBot owns the hook
# lifecycle so the event set, URL and secret are always correct, and the
# double-delivery footgun (#17) is prevented by the overlap guard.
# ---------------------------------------------------------------------------

# Mirrors the per-project hooks GitBot has always used, so a group hook behaves
# identically to today's setup. No push events — GitBot acts on issues, notes
# and MRs.
GROUP_HOOK_EVENTS = {
    "issues_events": True,
    "confidential_issues_events": True,
    "note_events": True,
    "confidential_note_events": True,
    "merge_requests_events": True,
    "push_events": False,
}


def _make_client(token: str | None = None, url: str | None = None) -> gitlab.Gitlab:
    """A client using an override token/url (onboarding), or the configured one.

    Passing no override returns the cached, configured client (the common
    single-tenant path). A pasted onboarding token builds a transient client.
    """
    if not token and not url:
        return get_client()
    return gitlab.Gitlab(
        url or settings.gitlab_url,
        private_token=token or settings.gitlab_token,
        ssl_verify=settings.gitlab_ssl_verify,
    )


def _hook_is_ours(hook_url: str, our_url: str) -> bool:
    """Match a hook to this GitBot instance by URL (host + /webhook path)."""
    if not hook_url or not our_url:
        return False
    if hook_url.rstrip("/") == our_url.rstrip("/"):
        return True
    try:
        from urllib.parse import urlparse
        a, b = urlparse(hook_url), urlparse(our_url)
        return a.netloc == b.netloc and a.path.rstrip("/") == b.path.rstrip("/")
    except Exception:
        return False


def list_owned_groups(token: str | None = None, url: str | None = None) -> list[dict]:
    """Groups (incl. subgroups) where the token has Owner — the ones whose
    webhooks it can manage."""
    gl = _make_client(token, url)
    groups = gl.groups.list(min_access_level=50, all_available=False, get_all=True)
    return [
        {
            "id": g.id,
            "name": g.name,
            "full_path": g.full_path,
            "parent_id": getattr(g, "parent_id", None),
            "web_url": g.web_url,
        }
        for g in groups
    ]


def list_group_hooks(group_id: int, token: str | None = None) -> list[dict]:
    gl = _make_client(token)
    group = gl.groups.get(group_id)
    return [{"id": h.id, "url": h.url} for h in group.hooks.list(get_all=True)]


def group_parent_id(group_id: int, token: str | None = None) -> int | None:
    gl = _make_client(token)
    return getattr(gl.groups.get(group_id), "parent_id", None)


def create_group_hook(
    group_id: int, hook_url: str, secret: str, token: str | None = None
) -> int:
    gl = _make_client(token)
    group = gl.groups.get(group_id)
    attrs = {
        "url": hook_url,
        "token": secret,
        "enable_ssl_verification": settings.gitlab_ssl_verify,
        **GROUP_HOOK_EVENTS,
    }
    hook = group.hooks.create(attrs)
    return hook.id


def delete_group_hook(group_id: int, hook_id: int, token: str | None = None) -> None:
    gl = _make_client(token)
    gl.groups.get(group_id).hooks.delete(hook_id)


def delete_project_hook(project_id: int, hook_id: int, token: str | None = None) -> None:
    gl = _make_client(token)
    gl.projects.get(project_id).hooks.delete(hook_id)


def scan_project_hook_overlaps(
    group_id: int, our_url: str, token: str | None = None, cap: int = 300
) -> dict:
    """Find project webhooks (in this group + subgroups) already pointing at us.

    A group hook plus a project hook on the same project = two deliveries = two
    paid runs. Callers refuse or clean these before creating the group hook.

    The per-project hook reads run concurrently (each worker uses its own
    client) — a large group is 50+ API calls that are otherwise seconds of
    sequential latency.
    """
    import concurrent.futures as cf

    gl = _make_client(token)
    group = gl.groups.get(group_id)
    projects = group.projects.list(include_subgroups=True, get_all=True)
    truncated = len(projects) > cap
    targets = [(p.id, p.path_with_namespace) for p in projects[:cap]]

    def _check(item):
        pid, path = item
        try:
            cl = _make_client(token)  # own client per thread (session safety)
            for h in cl.projects.get(pid).hooks.list(get_all=True):
                if _hook_is_ours(h.url, our_url):
                    return {"project_id": pid, "path": path, "hook_id": h.id}
        except Exception:
            return None
        return None

    overlaps = []
    if targets:
        with cf.ThreadPoolExecutor(max_workers=min(12, len(targets))) as ex:
            for r in ex.map(_check, targets):
                if r:
                    overlaps.append(r)
    return {"overlaps": overlaps, "scanned": len(targets), "truncated": truncated}


# ---------------------------------------------------------------------------
# Service-account provisioning (onboarding — issue #36, self-hosted Tier A)
#
# An admin token, used once at onboarding, creates/adopts GitBot's single
# dedicated bot user, mints a scoped day-to-day token for it, and grants it
# membership on the groups being enabled. The admin token is never persisted.
# ---------------------------------------------------------------------------

def token_is_admin(token: str, url: str | None = None) -> bool:
    gl = _make_client(token, url)
    gl.auth()
    return bool(getattr(gl.user, "is_admin", False))


def list_all_groups(token: str | None = None, url: str | None = None) -> list[dict]:
    """Every group visible to the token (admin sees all) — for onboarding."""
    gl = _make_client(token, url)
    groups = gl.groups.list(all_available=True, get_all=True)
    return [
        {"id": g.id, "name": g.name, "full_path": g.full_path,
         "parent_id": getattr(g, "parent_id", None), "web_url": g.web_url}
        for g in groups
    ]


def find_user_by_username(username: str, token: str | None = None) -> dict | None:
    gl = _make_client(token)
    users = gl.users.list(username=username)
    if users:
        u = users[0]
        return {"id": u.id, "username": u.username, "name": u.name}
    return None


def is_service_account(user_id: int, token: str | None = None) -> bool:
    """True if the user id is an instance service account (admin-only list)."""
    gl = _make_client(token)
    try:
        for sa in gl.http_list("/service_accounts", get_all=True):
            if sa.get("id") == user_id:
                return True
    except Exception:
        pass
    return False


def create_service_account(name: str, username: str, token: str | None = None) -> dict:
    gl = _make_client(token)
    data = {}
    if name:
        data["name"] = name
    if username:
        data["username"] = username
    sa = gl.http_post("/service_accounts", post_data=data)
    return {"id": sa["id"], "username": sa.get("username"), "name": sa.get("name")}


def create_user_token(
    user_id: int, name: str, token: str | None = None,
    scopes: list[str] | None = None, days: int = 364,
) -> dict:
    """Mint a personal access token for a user (admin). Returns the raw token
    string (shown once by GitLab)."""
    import datetime
    gl = _make_client(token)
    expires = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    user = gl.users.get(user_id, lazy=True)
    pat = user.personal_access_tokens.create(
        {"name": name, "scopes": scopes or ["api"], "expires_at": expires}
    )
    return {"token": pat.token, "id": pat.id, "expires_at": getattr(pat, "expires_at", expires)}


def ensure_group_membership(
    group_id: int, user_id: int, access_level: int, token: str | None = None
) -> str:
    """Add (or update) a user's membership in a group. Idempotent."""
    gl = _make_client(token)
    try:
        gl.http_post(
            f"/groups/{group_id}/members",
            post_data={"user_id": user_id, "access_level": access_level},
        )
        return "added"
    except Exception as e:
        msg = str(e).lower()
        if "409" in msg or "already" in msg or "member" in msg:
            try:
                gl.http_put(
                    f"/groups/{group_id}/members/{user_id}",
                    post_data={"access_level": access_level},
                )
                return "updated"
            except Exception:
                return "exists"
        raise


def find_items_by_label(label: str) -> list[dict]:
    """Find open issues and MRs across all accessible projects with a given label.

    Returns a list of dicts with project_id, target_type, target_iid, title.
    """
    gl = get_client()
    results = []

    # Search issues with this label
    try:
        issues = gl.issues.list(labels=[label], state="opened", get_all=True, scope="all")
        for issue in issues:
            results.append({
                "project_id": issue.project_id,
                "target_type": "Issue",
                "target_iid": issue.iid,
                "title": issue.title,
            })
    except Exception:
        pass

    # Search MRs with this label
    try:
        mrs = gl.mergerequests.list(labels=[label], state="opened", get_all=True, scope="all")
        for mr in mrs:
            pid = mr.project_id if hasattr(mr, "project_id") else mr.source_project_id
            results.append({
                "project_id": pid,
                "target_type": "MergeRequest",
                "target_iid": mr.iid,
                "title": mr.title,
            })
    except Exception:
        pass

    return results
