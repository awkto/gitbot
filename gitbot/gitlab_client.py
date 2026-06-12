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
