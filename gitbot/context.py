"""Build a situation report for any incoming GitLab event.

This gives the LLM full awareness of where it is before deciding what to do.
"""

import logging
from dataclasses import dataclass, field

from gitbot import gitlab_client as glc, state
from gitbot.config import settings

log = logging.getLogger(__name__)


@dataclass
class Situation:
    """Everything the bot needs to know before deciding what to do."""

    # What triggered this
    event_type: str = ""           # "Issue Hook", "Merge Request Hook", "Note Hook"
    trigger: str = ""              # "assigned", "review_requested", "mentioned", "comment"

    # Who
    actor: str = ""                # username of whoever triggered the event
    bot_username: str = ""

    # The target (issue or MR)
    project_id: int = 0
    project_name: str = ""
    target_type: str = ""          # "Issue" or "MergeRequest"
    target_iid: int = 0
    target_title: str = ""
    target_description: str = ""
    target_state: str = ""         # "opened", "closed", "merged"
    target_labels: list[str] = field(default_factory=list)

    # Bot's relationship to the target
    bot_is_assignee: bool = False
    bot_is_reviewer: bool = False
    bot_is_author: bool = False

    # The comment that triggered this (if Note Hook)
    comment_body: str = ""
    discussion_id: str = ""

    # Conversation history
    recent_comments: list[dict] = field(default_factory=list)  # [{author, body}]

    # Related objects
    related_mrs: list[dict] = field(default_factory=list)  # for issues: linked MRs
    related_issue_iid: int | None = None                   # for MRs: closing issue

    # MR-specific context
    mr_source_branch: str = ""
    mr_diff: str = ""

    # Repository
    repo_tree: str = ""
    default_branch: str = "main"

    # Pending work from state DB
    pending_question: dict | None = None
    active_work: dict | None = None

    def to_prompt(self) -> str:
        """Render the situation as a structured prompt section."""
        parts = []

        parts.append(f"<event type=\"{self.event_type}\" trigger=\"{self.trigger}\" actor=\"{self.actor}\" />")

        # Target
        parts.append(f"<target type=\"{self.target_type}\" iid=\"{self.target_iid}\" state=\"{self.target_state}\">")
        parts.append(f"  <title>{self.target_title}</title>")
        if self.target_description:
            parts.append(f"  <description>{self.target_description}</description>")
        if self.target_labels:
            parts.append(f"  <labels>{', '.join(self.target_labels)}</labels>")
        parts.append("</target>")

        # Bot's role
        roles = []
        if self.bot_is_author:
            roles.append("author")
        if self.bot_is_assignee:
            roles.append("assignee")
        if self.bot_is_reviewer:
            roles.append("reviewer")
        parts.append(f"<bot_role>{', '.join(roles) if roles else 'none'}</bot_role>")

        # The triggering comment
        if self.comment_body:
            parts.append(f"<comment by=\"{self.actor}\">{self.comment_body}</comment>")

        # Conversation history
        if self.recent_comments:
            parts.append("<conversation_history>")
            for c in self.recent_comments:
                parts.append(f"  @{c['author']}: {c['body']}")
            parts.append("</conversation_history>")

        # Related MRs (for issues)
        if self.related_mrs:
            parts.append("<related_merge_requests>")
            for mr in self.related_mrs:
                parts.append(f"  !{mr['iid']}: {mr['title']} (state={mr['state']}, branch={mr.get('source_branch', '?')})")
            parts.append("</related_merge_requests>")

        # MR context
        if self.mr_source_branch:
            parts.append(f"<merge_request_branch>{self.mr_source_branch}</merge_request_branch>")
        if self.mr_diff:
            diff_preview = self.mr_diff[:5000]
            if len(self.mr_diff) > 5000:
                diff_preview += "\n... (truncated)"
            parts.append(f"<diff>\n{diff_preview}\n</diff>")

        # Related issue for MRs
        if self.related_issue_iid:
            parts.append(f"<closes_issue>#{self.related_issue_iid}</closes_issue>")

        # Repo
        if self.repo_tree:
            parts.append(f"<repository default_branch=\"{self.default_branch}\">\n{self.repo_tree}\n</repository>")

        # Pending work
        if self.pending_question:
            parts.append(f"<pending_question asked_user=\"{self.pending_question.get('asked_user', '?')}\">")
            parts.append(f"  {self.pending_question.get('question', '')}")
            parts.append("</pending_question>")

        return "\n".join(parts)


def build_situation(event_type: str, payload: dict) -> Situation:
    """Build a full situation report from a webhook payload."""
    sit = Situation()
    sit.event_type = event_type
    sit.bot_username = settings.bot_username
    sit.actor = payload.get("user", {}).get("username", "unknown")
    sit.project_id = payload.get("project", {}).get("id", 0)
    sit.project_name = payload.get("project", {}).get("name", "")

    if event_type == "Issue Hook":
        _build_issue_context(sit, payload)
    elif event_type == "Merge Request Hook":
        _build_mr_context(sit, payload)
    elif event_type == "Note Hook":
        _build_note_context(sit, payload)

    # Always gather: repo tree, pending work
    _add_repo_context(sit)
    _add_state_context(sit)

    return sit


def _build_issue_context(sit: Situation, payload: dict) -> None:
    attrs = payload.get("object_attributes", {})
    sit.target_type = "Issue"
    sit.target_iid = attrs.get("iid", 0)
    sit.target_title = attrs.get("title", "")
    sit.target_description = attrs.get("description", "") or ""
    sit.target_state = attrs.get("state", "opened")
    sit.target_labels = attrs.get("labels", [])

    assignees = payload.get("assignees", [])
    sit.bot_is_assignee = any(a.get("username") == sit.bot_username for a in assignees)
    sit.trigger = "assigned" if sit.bot_is_assignee else "updated"

    # Related MRs
    try:
        sit.related_mrs = glc.get_related_mrs(sit.project_id, sit.target_iid)
    except Exception:
        pass

    _add_comments(sit)


def _build_mr_context(sit: Situation, payload: dict) -> None:
    attrs = payload.get("object_attributes", {})
    sit.target_type = "MergeRequest"
    sit.target_iid = attrs.get("iid", 0)
    sit.target_title = attrs.get("title", "")
    sit.target_description = attrs.get("description", "") or ""
    sit.target_state = attrs.get("state", "opened")
    sit.mr_source_branch = attrs.get("source_branch", "")

    assignees = payload.get("assignees", [])
    reviewers = payload.get("reviewers", [])
    sit.bot_is_assignee = any(a.get("username") == sit.bot_username for a in assignees)
    sit.bot_is_reviewer = any(r.get("username") == sit.bot_username for r in reviewers)

    # Check if bot authored this MR
    mr_author = attrs.get("author", {}).get("username") if isinstance(attrs.get("author"), dict) else None
    sit.bot_is_author = (mr_author == sit.bot_username)

    if sit.bot_is_reviewer:
        sit.trigger = "review_requested"
    elif sit.bot_is_assignee:
        sit.trigger = "assigned"
    else:
        sit.trigger = "updated"

    # Get diff
    try:
        sit.mr_diff = glc.get_mr_diff(sit.project_id, sit.target_iid)
    except Exception:
        pass

    # Parse closing issue from description
    import re
    close_match = re.search(r"[Cc]loses?\s+#(\d+)", sit.target_description)
    if close_match:
        sit.related_issue_iid = int(close_match.group(1))

    _add_comments(sit)


def _build_note_context(sit: Situation, payload: dict) -> None:
    note = payload.get("object_attributes", {})
    sit.comment_body = note.get("note", "")
    sit.discussion_id = note.get("discussion_id", "")
    noteable_type = note.get("noteable_type", "")

    if noteable_type == "Issue" and "issue" in payload:
        issue = payload["issue"]
        sit.target_type = "Issue"
        sit.target_iid = issue.get("iid", 0)
        sit.target_title = issue.get("title", "")
        sit.target_description = issue.get("description", "") or ""
        sit.trigger = "mentioned" if f"@{sit.bot_username}" in sit.comment_body else "comment"

        try:
            sit.related_mrs = glc.get_related_mrs(sit.project_id, sit.target_iid)
        except Exception:
            pass

    elif noteable_type == "MergeRequest" and "merge_request" in payload:
        mr = payload["merge_request"]
        sit.target_type = "MergeRequest"
        sit.target_iid = mr.get("iid", 0)
        sit.target_title = mr.get("title", "")
        sit.target_description = mr.get("description", "") or ""
        sit.mr_source_branch = mr.get("source_branch", "")
        sit.trigger = "mentioned" if f"@{sit.bot_username}" in sit.comment_body else "comment"

        # Look up full MR details for author/assignee info
        try:
            details = glc.get_mr_details(sit.project_id, sit.target_iid)
            sit.bot_is_author = (details.get("author") == sit.bot_username)
            sit.bot_is_assignee = (sit.bot_username in details.get("assignees", []))
        except Exception:
            pass

        try:
            sit.mr_diff = glc.get_mr_diff(sit.project_id, sit.target_iid)
        except Exception:
            pass

    _add_comments(sit)


def _add_comments(sit: Situation) -> None:
    """Fetch recent comments on the target."""
    try:
        gl = glc.get_client()
        project = gl.projects.get(sit.project_id)
        if sit.target_type == "Issue":
            target = project.issues.get(sit.target_iid)
        elif sit.target_type == "MergeRequest":
            target = project.mergerequests.get(sit.target_iid)
        else:
            return

        notes = target.notes.list(per_page=15, sort="desc")
        sit.recent_comments = [
            {
                "author": n.author.get("username", "?") if isinstance(n.author, dict) else "?",
                "body": n.body[:300],
            }
            for n in notes
            if not n.system
        ]
        # Reverse so oldest first
        sit.recent_comments.reverse()
    except Exception:
        pass


def _add_repo_context(sit: Situation) -> None:
    """Get repo tree and default branch."""
    try:
        gl = glc.get_client()
        project = gl.projects.get(sit.project_id)
        sit.default_branch = project.default_branch or "main"
        tree = glc.list_repo_tree(sit.project_id, ref=sit.default_branch)
        sit.repo_tree = "\n".join(
            f"{'[dir] ' if item['type'] == 'tree' else ''}{item['path']}"
            for item in tree
        )
    except Exception:
        sit.repo_tree = "(empty or inaccessible)"


def _add_state_context(sit: Situation) -> None:
    """Check for pending work in the state DB."""
    if sit.target_type and sit.target_iid:
        sit.pending_question = state.get_pending_question(
            sit.project_id, sit.target_type, sit.target_iid
        )
        sit.active_work = state.get_active_work(
            sit.project_id, sit.target_type, sit.target_iid
        )
