"""Event handlers - one function per event type the bot cares about."""

import json
import logging
import re

from gitbot import llm, gitlab_client as glc
from gitbot.config import settings
from gitbot.models import Task
from gitbot.prompts import issue_analysis, implement, mr_summary, code_review, mention_response

log = logging.getLogger(__name__)

# Keywords that suggest an issue is asking for code implementation rather than just discussion
_IMPL_SIGNALS = re.compile(
    r"\b(write|create|add|build|implement|make|set up|setup|develop|scaffold)\b",
    re.IGNORECASE,
)


def _looks_like_implementation_request(title: str, description: str) -> bool:
    """Heuristic: does this issue ask for code to be written?"""
    text = f"{title} {description}"
    return bool(_IMPL_SIGNALS.search(text))


def _parse_impl_response(raw: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences."""
    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return json.loads(cleaned)


async def handle_issue_assigned(payload: dict) -> None:
    """Bot was assigned to an issue - analyze it and either implement or discuss."""
    attrs = payload["object_attributes"]
    project_id = payload["project"]["id"]
    issue_iid = attrs["iid"]
    title = attrs["title"]
    description = attrs.get("description", "") or ""

    if _looks_like_implementation_request(title, description):
        await _handle_issue_implement(project_id, issue_iid, title, description)
    else:
        await _handle_issue_discuss(project_id, issue_iid, title, description)


async def _handle_issue_discuss(
    project_id: int, issue_iid: int, title: str, description: str
) -> None:
    """Respond to an issue with analysis and questions."""
    system, prompt = issue_analysis(
        settings.llm_family,
        title=title,
        description=description,
    )

    response = await llm.complete(Task.ISSUE_ANALYSIS, system=system, prompt=prompt)
    glc.post_note_on_issue(project_id, issue_iid, response)
    log.info("Responded to issue #%s in project %s", issue_iid, project_id)


async def _handle_issue_implement(
    project_id: int, issue_iid: int, title: str, description: str
) -> None:
    """Implement the issue: create branch, write files, open MR."""
    # Get repo tree for context
    try:
        tree = glc.list_repo_tree(project_id)
        tree_str = "\n".join(
            f"{'[dir] ' if item['type'] == 'tree' else ''}{item['path']}"
            for item in tree
        )
    except Exception:
        tree_str = "(empty repository)"

    # Figure out default branch
    gl = glc.get_client()
    project = gl.projects.get(project_id)
    default_branch = project.default_branch or "main"

    glc.post_note_on_issue(
        project_id, issue_iid,
        "🔧 Working on this — I'll create a branch and open an MR shortly."
    )

    system, prompt = implement(
        settings.llm_family,
        title=title,
        description=description,
        repo_tree=tree_str,
        default_branch=default_branch,
    )

    raw = await llm.complete(Task.IMPLEMENT, system=system, prompt=prompt)

    try:
        plan = _parse_impl_response(raw)
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to parse implementation response: %s", e)
        glc.post_note_on_issue(
            project_id, issue_iid,
            f"I tried to implement this but had trouble structuring the output. "
            f"Here's what I came up with — I can retry if needed:\n\n{raw[:3000]}"
        )
        return

    branch = plan["branch_name"]
    files = plan["files"]
    commit_msg = plan["commit_message"]
    mr_title = plan["mr_title"]
    mr_desc = plan["mr_description"]

    try:
        # Create branch
        glc.create_branch(project_id, branch, ref=default_branch)
        log.info("Created branch %s in project %s", branch, project_id)

        # Commit files
        glc.commit_files(project_id, branch, commit_msg, files)
        log.info("Committed %d files to %s", len(files), branch)

        # Open MR
        mr_desc_full = f"{mr_desc}\n\nCloses #{issue_iid}"
        mr = glc.create_merge_request(
            project_id, branch, default_branch, mr_title, mr_desc_full
        )
        log.info("Created MR !%s: %s", mr["iid"], mr["web_url"])

        glc.post_note_on_issue(
            project_id, issue_iid,
            f"✅ I've implemented this and opened **!{mr['iid']}** — "
            f"[view the merge request]({mr['web_url']})\n\n"
            f"**Files created/modified:** {', '.join('`' + f['file_path'] + '`' for f in files)}"
        )

    except Exception as e:
        log.exception("Failed to create branch/MR for issue #%s", issue_iid)
        glc.post_note_on_issue(
            project_id, issue_iid,
            f"I hit an error while creating the branch/MR: `{e}`\n\n"
            f"Branch: `{branch}`, files: {len(files)}"
        )


async def handle_mr_assigned(payload: dict) -> None:
    """Bot was assigned to an MR - look at the diff and comment."""
    attrs = payload["object_attributes"]
    project_id = payload["project"]["id"]
    mr_iid = attrs["iid"]

    diff = glc.get_mr_diff(project_id, mr_iid)

    system, prompt = mr_summary(
        settings.llm_family,
        title=attrs["title"],
        description=attrs.get("description", "") or "",
        diff=diff,
    )

    response = await llm.complete(Task.MR_SUMMARY, system=system, prompt=prompt)
    glc.post_note_on_mr(project_id, mr_iid, response)
    log.info("Responded to MR !%s in project %s", mr_iid, project_id)


async def handle_mr_review_requested(payload: dict) -> None:
    """Bot was added as a reviewer - do a code review."""
    attrs = payload["object_attributes"]
    project_id = payload["project"]["id"]
    mr_iid = attrs["iid"]

    diff = glc.get_mr_diff(project_id, mr_iid)

    system, prompt = code_review(
        settings.llm_family,
        title=attrs["title"],
        description=attrs.get("description", "") or "",
        diff=diff,
    )

    response = await llm.complete(Task.CODE_REVIEW, system=system, prompt=prompt)
    glc.post_note_on_mr(project_id, mr_iid, response)
    log.info("Reviewed MR !%s in project %s", mr_iid, project_id)


async def handle_mention(payload: dict) -> None:
    """Bot was @mentioned in a note/comment."""
    project_id = payload["project"]["id"]
    note = payload["object_attributes"]
    note_body = note.get("note", "")
    noteable_type = note.get("noteable_type", "")
    discussion_id = note.get("discussion_id")

    noteable_title = ""
    noteable_iid = None
    if noteable_type == "MergeRequest" and "merge_request" in payload:
        mr = payload["merge_request"]
        noteable_title = mr.get("title", "")
        noteable_iid = mr["iid"]
    elif noteable_type == "Issue" and "issue" in payload:
        issue = payload["issue"]
        noteable_title = issue.get("title", "")
        noteable_iid = issue["iid"]

    system, prompt = mention_response(
        settings.llm_family,
        note_body=note_body,
        noteable_type=noteable_type,
        noteable_title=noteable_title,
    )

    response = await llm.complete(Task.MENTION_RESPONSE, system=system, prompt=prompt)

    if discussion_id and noteable_iid and noteable_type in ("MergeRequest", "Issue"):
        glc.reply_to_discussion(
            project_id, noteable_type, noteable_iid, discussion_id, response
        )
        log.info("Replied to discussion %s", discussion_id)
    else:
        log.warning("Could not find discussion context to reply to, dropping response")
