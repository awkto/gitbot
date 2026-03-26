"""Iterative context gathering.

Starts with minimal context (always cheap to get), then lets the LLM
request additional context sources as needed. Each round, Haiku decides
whether it has enough to act or needs more info.
"""

import logging
import re
from dataclasses import dataclass, field

from gitbot import gitlab_client as glc, state
from gitbot.config import settings

log = logging.getLogger(__name__)

MAX_ROUNDS = 5


# ---------------------------------------------------------------------------
# Available context sources the LLM can request
# ---------------------------------------------------------------------------

AVAILABLE_SOURCES = {
    "diff": "The full diff of the merge request",
    "repo_tree": "List of files/directories in the repository",
    "readme": "Contents of the project README",
    "related_mrs": "Merge requests linked to this issue",
    "related_issues": "Issues linked to this MR (closing issues)",
    "mr_details": "Full MR details (branch, author, assignees, state)",
    "conversation_history": "Recent comments on this issue/MR",
    "file_content:<path>": "Contents of a specific file in the repo (e.g. file_content:src/main.rs)",
    "milestone": "The milestone this issue/MR is part of, if any",
    "labels": "Labels on this issue/MR and their descriptions",
    "issue_details:<iid>": "Full details of a specific issue by IID",
}


@dataclass
class Situation:
    """Everything the bot knows so far. Grows as context is gathered."""

    # What triggered this — always available
    event_type: str = ""
    trigger: str = ""
    actor: str = ""
    bot_username: str = ""

    # Target basics — always available
    project_id: int = 0
    project_name: str = ""
    target_type: str = ""
    target_iid: int = 0
    target_title: str = ""
    target_description: str = ""
    target_state: str = ""

    # Bot's relationship — always available
    bot_is_assignee: bool = False
    bot_is_reviewer: bool = False
    bot_is_author: bool = False

    # Triggering comment (for Note Hooks) — always available
    comment_body: str = ""
    discussion_id: str = ""

    # Pending work from state DB — always available (cheap local lookup)
    pending_question: dict | None = None

    # --- Fetched on demand ---
    gathered: dict[str, str] = field(default_factory=dict)

    # MR branch info (fetched with mr_details)
    mr_source_branch: str = ""
    mr_target_branch: str = ""

    # Track what's been fetched
    fetched_sources: set[str] = field(default_factory=set)

    def to_prompt(self) -> str:
        """Render current knowledge as prompt."""
        parts = []

        parts.append(f"<event type=\"{self.event_type}\" trigger=\"{self.trigger}\" actor=\"{self.actor}\" />")

        parts.append(f"<target type=\"{self.target_type}\" iid=\"{self.target_iid}\" state=\"{self.target_state}\">")
        parts.append(f"  <title>{self.target_title}</title>")
        if self.target_description:
            parts.append(f"  <description>{self.target_description}</description>")
        parts.append("</target>")

        roles = []
        if self.bot_is_author:
            roles.append("author")
        if self.bot_is_assignee:
            roles.append("assignee")
        if self.bot_is_reviewer:
            roles.append("reviewer")
        parts.append(f"<bot_role>{', '.join(roles) if roles else 'none'}</bot_role>")

        if self.comment_body:
            parts.append(f"<comment by=\"{self.actor}\">{self.comment_body}</comment>")

        if self.pending_question:
            parts.append(f"<pending_question asked_user=\"{self.pending_question.get('asked_user', '?')}\">")
            parts.append(f"  {self.pending_question.get('question', '')}")
            parts.append("</pending_question>")

        # Gathered context
        for source, content in self.gathered.items():
            parts.append(f"<{source}>\n{content}\n</{source}>")

        # What's available but not yet fetched
        available = set(AVAILABLE_SOURCES.keys()) - self.fetched_sources
        # Remove parameterized sources from the set display, keep the templates
        base_available = set()
        for s in available:
            if ":" in s and s.split(":")[0] + ":" in [k.split(":")[0] + ":" for k in AVAILABLE_SOURCES if ":" in k]:
                base_available.add(s)
            else:
                base_available.add(s)

        if base_available:
            parts.append("<available_context_sources>")
            for s in sorted(base_available):
                parts.append(f"  - {s}: {AVAILABLE_SOURCES.get(s, 'additional context')}")
            parts.append("</available_context_sources>")

        return "\n".join(parts)


def build_minimal(event_type: str, payload: dict) -> Situation:
    """Build a situation with only the basics — always cheap."""
    sit = Situation()
    sit.event_type = event_type
    sit.bot_username = settings.bot_username
    sit.actor = payload.get("user", {}).get("username", "unknown")
    sit.project_id = payload.get("project", {}).get("id", 0)
    sit.project_name = payload.get("project", {}).get("name", "")

    if event_type == "Issue Hook":
        _extract_issue_basics(sit, payload)
    elif event_type == "Merge Request Hook":
        _extract_mr_basics(sit, payload)
    elif event_type == "Note Hook":
        _extract_note_basics(sit, payload)

    # State DB lookup is local, always do it
    if sit.target_type and sit.target_iid:
        sit.pending_question = state.get_pending_question(
            sit.project_id, sit.target_type, sit.target_iid
        )

    return sit


def fetch_source(sit: Situation, source: str) -> None:
    """Fetch a specific context source and add it to the situation."""
    if source in sit.fetched_sources:
        return

    log.info("Fetching context: %s for %s #%s", source, sit.target_type, sit.target_iid)

    try:
        if source == "diff":
            if sit.target_type == "MergeRequest":
                content = glc.get_mr_diff(sit.project_id, sit.target_iid)
                sit.gathered["diff"] = content[:8000]  # cap for token sanity
            else:
                sit.gathered["diff"] = "(not a merge request)"

        elif source == "repo_tree":
            gl = glc.get_client()
            project = gl.projects.get(sit.project_id)
            default_branch = project.default_branch or "main"
            tree = glc.list_repo_tree(sit.project_id, ref=default_branch)
            sit.gathered["repo_tree"] = "\n".join(
                f"{'[dir] ' if item['type'] == 'tree' else ''}{item['path']}"
                for item in tree
            )

        elif source == "readme":
            gl = glc.get_client()
            project = gl.projects.get(sit.project_id)
            ref = project.default_branch or "main"
            for name in ["README.md", "README.rst", "README.txt", "README"]:
                try:
                    content = glc.get_file_content(sit.project_id, name, ref=ref)
                    sit.gathered["readme"] = content[:3000]
                    break
                except Exception:
                    continue
            else:
                sit.gathered["readme"] = "(no README found)"

        elif source == "related_mrs":
            if sit.target_type == "Issue":
                mrs = glc.get_related_mrs(sit.project_id, sit.target_iid)
                if mrs:
                    lines = []
                    for mr in mrs:
                        lines.append(f"!{mr['iid']}: {mr['title']} (state={mr['state']}, branch={mr.get('source_branch', '?')}, author={mr.get('author', '?')})")
                    sit.gathered["related_mrs"] = "\n".join(lines)
                else:
                    sit.gathered["related_mrs"] = "(none)"
            else:
                sit.gathered["related_mrs"] = "(target is not an issue)"

        elif source == "related_issues":
            if sit.target_type == "MergeRequest":
                close_match = re.search(r"[Cc]loses?\s+#(\d+)", sit.target_description)
                if close_match:
                    iid = int(close_match.group(1))
                    gl = glc.get_client()
                    project = gl.projects.get(sit.project_id)
                    issue = project.issues.get(iid)
                    sit.gathered["related_issues"] = f"#{iid}: {issue.title}\n{issue.description or ''}"
                else:
                    sit.gathered["related_issues"] = "(no closing issue referenced)"
            else:
                sit.gathered["related_issues"] = "(target is not an MR)"

        elif source == "mr_details":
            if sit.target_type == "MergeRequest":
                details = glc.get_mr_details(sit.project_id, sit.target_iid)
                sit.mr_source_branch = details.get("source_branch", "")
                sit.mr_target_branch = details.get("target_branch", "")
                sit.bot_is_author = (details.get("author") == sit.bot_username)
                sit.bot_is_assignee = (sit.bot_username in details.get("assignees", []))
                sit.gathered["mr_details"] = (
                    f"branch: {sit.mr_source_branch} → {sit.mr_target_branch}\n"
                    f"author: {details.get('author')}\n"
                    f"assignees: {details.get('assignees')}\n"
                    f"state: {details.get('state')}"
                )
            else:
                sit.gathered["mr_details"] = "(not a merge request)"

        elif source == "conversation_history":
            gl = glc.get_client()
            project = gl.projects.get(sit.project_id)
            if sit.target_type == "Issue":
                target = project.issues.get(sit.target_iid)
            elif sit.target_type == "MergeRequest":
                target = project.mergerequests.get(sit.target_iid)
            else:
                sit.gathered["conversation_history"] = "(unknown target type)"
                sit.fetched_sources.add(source)
                return

            notes = target.notes.list(per_page=20, sort="asc")
            lines = []
            for n in notes:
                if not n.system:
                    author = n.author.get("username", "?") if isinstance(n.author, dict) else "?"
                    lines.append(f"@{author}: {n.body[:300]}")
            sit.gathered["conversation_history"] = "\n\n".join(lines) if lines else "(no comments)"

        elif source.startswith("file_content:"):
            path = source.split(":", 1)[1]
            gl = glc.get_client()
            project = gl.projects.get(sit.project_id)
            ref = sit.mr_source_branch or project.default_branch or "main"
            content = glc.get_file_content(sit.project_id, path, ref=ref)
            sit.gathered[source] = content[:5000]

        elif source == "milestone":
            gl = glc.get_client()
            project = gl.projects.get(sit.project_id)
            if sit.target_type == "Issue":
                target = project.issues.get(sit.target_iid)
            elif sit.target_type == "MergeRequest":
                target = project.mergerequests.get(sit.target_iid)
            else:
                sit.gathered["milestone"] = "(unknown target)"
                sit.fetched_sources.add(source)
                return

            ms = target.milestone
            if ms:
                sit.gathered["milestone"] = f"{ms.get('title', '?')}: {ms.get('description', '')}\nDue: {ms.get('due_date', 'none')}"
            else:
                sit.gathered["milestone"] = "(no milestone)"

        elif source == "labels":
            gl = glc.get_client()
            project = gl.projects.get(sit.project_id)
            if sit.target_type == "Issue":
                target = project.issues.get(sit.target_iid)
            else:
                target = project.mergerequests.get(sit.target_iid)
            sit.gathered["labels"] = ", ".join(target.labels) if target.labels else "(none)"

        elif source.startswith("issue_details:"):
            iid = int(source.split(":", 1)[1])
            gl = glc.get_client()
            project = gl.projects.get(sit.project_id)
            issue = project.issues.get(iid)
            sit.gathered[source] = f"#{iid}: {issue.title}\nState: {issue.state}\n{issue.description or ''}"

        else:
            sit.gathered[source] = f"(unknown source: {source})"

    except Exception as e:
        log.warning("Failed to fetch %s: %s", source, e)
        sit.gathered[source] = f"(error fetching: {e})"

    sit.fetched_sources.add(source)


# ---------------------------------------------------------------------------
# Basic extractors (no API calls)
# ---------------------------------------------------------------------------

def _extract_issue_basics(sit: Situation, payload: dict) -> None:
    attrs = payload.get("object_attributes", {})
    sit.target_type = "Issue"
    sit.target_iid = attrs.get("iid", 0)
    sit.target_title = attrs.get("title", "")
    sit.target_description = attrs.get("description", "") or ""
    sit.target_state = attrs.get("state", "opened")

    assignees = payload.get("assignees", [])
    sit.bot_is_assignee = any(a.get("username") == sit.bot_username for a in assignees)
    sit.trigger = "assigned" if sit.bot_is_assignee else "updated"


def _extract_mr_basics(sit: Situation, payload: dict) -> None:
    attrs = payload.get("object_attributes", {})
    sit.target_type = "MergeRequest"
    sit.target_iid = attrs.get("iid", 0)
    sit.target_title = attrs.get("title", "")
    sit.target_description = attrs.get("description", "") or ""
    sit.target_state = attrs.get("state", "opened")
    sit.mr_source_branch = attrs.get("source_branch", "")
    sit.mr_target_branch = attrs.get("target_branch", "")

    assignees = payload.get("assignees", [])
    reviewers = payload.get("reviewers", [])
    sit.bot_is_assignee = any(a.get("username") == sit.bot_username for a in assignees)
    sit.bot_is_reviewer = any(r.get("username") == sit.bot_username for r in reviewers)

    mr_author = attrs.get("author", {}).get("username") if isinstance(attrs.get("author"), dict) else None
    sit.bot_is_author = (mr_author == sit.bot_username)

    if sit.bot_is_reviewer:
        sit.trigger = "review_requested"
    elif sit.bot_is_assignee:
        sit.trigger = "assigned"
    else:
        sit.trigger = "updated"


def _extract_note_basics(sit: Situation, payload: dict) -> None:
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

    elif noteable_type == "MergeRequest" and "merge_request" in payload:
        mr = payload["merge_request"]
        sit.target_type = "MergeRequest"
        sit.target_iid = mr.get("iid", 0)
        sit.target_title = mr.get("title", "")
        sit.target_description = mr.get("description", "") or ""
        sit.mr_source_branch = mr.get("source_branch", "")
        sit.trigger = "mentioned" if f"@{sit.bot_username}" in sit.comment_body else "comment"
