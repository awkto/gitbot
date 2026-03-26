"""GitLab API helpers - post comments, fetch diffs, etc."""

import gitlab

from gitbot.config import settings

_gl: gitlab.Gitlab | None = None


def get_client() -> gitlab.Gitlab:
    global _gl
    if _gl is None:
        _gl = gitlab.Gitlab(settings.gitlab_url, private_token=settings.gitlab_token, ssl_verify=settings.gitlab_ssl_verify)
    return _gl


def post_note_on_issue(project_id: int, issue_iid: int, body: str) -> None:
    gl = get_client()
    project = gl.projects.get(project_id)
    issue = project.issues.get(issue_iid)
    issue.notes.create({"body": body})


def post_note_on_mr(project_id: int, mr_iid: int, body: str) -> None:
    gl = get_client()
    project = gl.projects.get(project_id)
    mr = project.mergerequests.get(mr_iid)
    mr.notes.create({"body": body})


def reply_to_discussion(
    project_id: int, noteable_type: str, noteable_iid: int, discussion_id: str, body: str
) -> None:
    gl = get_client()
    project = gl.projects.get(project_id)
    if noteable_type == "MergeRequest":
        noteable = project.mergerequests.get(noteable_iid)
    else:
        noteable = project.issues.get(noteable_iid)
    discussion = noteable.discussions.get(discussion_id)
    discussion.notes.create({"body": body})


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
