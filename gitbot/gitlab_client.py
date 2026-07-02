"""GitLab API helpers - post comments, fetch diffs, etc."""

import gitlab

from gitbot.config import settings

_gl: gitlab.Gitlab | None = None


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
    note = issue.notes.create({"body": body})
    return note.get_id() or note.attributes.get("id", 0)


def update_note_on_issue(project_id: int, issue_iid: int, note_id: int, body: str) -> None:
    """Edit an existing note in-place."""
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    note = issue.notes.get(note_id)
    note.body = body
    note.save()


def post_note_on_mr(project_id: int, mr_iid: int, body: str) -> int:
    """Post a note and return its ID (for later editing)."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    note = mr.notes.create({"body": body})
    return note.get_id() or note.attributes.get("id", 0)


def update_note_on_mr(project_id: int, mr_iid: int, note_id: int, body: str) -> None:
    """Edit an existing note in-place."""
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    note = mr.notes.get(note_id)
    note.body = body
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
    discussion = noteable.discussions.create({"body": body})
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
    note = discussion.notes.create({"body": body})
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
        "state": mr.state,
        "web_url": mr.web_url,
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
