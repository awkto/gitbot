"""Event handlers - one function per event type the bot cares about."""

import json
import logging
import re

from gitbot import llm, gitlab_client as glc, state
from gitbot.config import settings
from gitbot.models import Task
from gitbot.prompts import (
    triage, implement, issue_analysis, mr_summary,
    code_review, mention_response, followup_response,
)

log = logging.getLogger(__name__)


_GITBOT_LABELS = ["gitbot::thinking", "gitbot::working", "gitbot::waiting"]


def _clear_status_label(project_id: int, issue_iid: int) -> None:
    """Remove all gitbot scoped labels (work is done)."""
    glc.remove_issue_labels(project_id, issue_iid, _GITBOT_LABELS)


def _parse_json_response(raw: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return json.loads(cleaned)


def _get_assigner(payload: dict) -> str:
    """Extract the username of whoever triggered the event."""
    user = payload.get("user", {})
    return user.get("username", "unknown")


def _get_recent_comments(project_id: int, target_type: str, target_iid: int) -> str:
    """Get recent non-system comments on a target."""
    gl = glc.get_client()
    project = gl.projects.get(project_id)
    if target_type == "Issue":
        target = project.issues.get(target_iid)
    else:
        target = project.mergerequests.get(target_iid)

    notes = target.notes.list(per_page=10, sort="desc")
    comments = []
    for n in notes:
        if not n.system:
            author = n.author.get("username", "?") if isinstance(n.author, dict) else "?"
            comments.append(f"@{author}: {n.body[:200]}")
    return "\n".join(comments[:5]) if comments else "(no comments yet)"


def _get_repo_tree_str(project_id: int) -> tuple[str, str]:
    """Get repo tree string and default branch."""
    gl = glc.get_client()
    project = gl.projects.get(project_id)
    default_branch = project.default_branch or "main"
    try:
        tree = glc.list_repo_tree(project_id, ref=default_branch)
        tree_str = "\n".join(
            f"{'[dir] ' if item['type'] == 'tree' else ''}{item['path']}"
            for item in tree
        )
    except Exception:
        tree_str = "(empty repository)"
    return tree_str, default_branch


def _get_existing_mrs_str(project_id: int, issue_iid: int) -> str:
    """Get existing MRs for an issue as a string."""
    try:
        mrs = glc.get_related_mrs(project_id, issue_iid)
        if not mrs:
            return "(none)"
        return "\n".join(
            f"!{mr['iid']}: {mr['title']} (state={mr['state']}, "
            f"branch={mr['source_branch']}, author={mr['author']})"
            for mr in mrs
        )
    except Exception:
        return "(could not fetch)"


# ---------------------------------------------------------------------------
# Issue handler — now goes through triage first
# ---------------------------------------------------------------------------

async def handle_issue_assigned(payload: dict) -> None:
    """Bot was assigned to an issue — triage first, then act."""
    attrs = payload["object_attributes"]
    project_id = payload["project"]["id"]
    issue_iid = attrs["iid"]
    title = attrs["title"]
    description = attrs.get("description", "") or ""
    assigner = _get_assigner(payload)

    # Immediate feedback: placeholder comment + label
    placeholder_id = glc.post_note_on_issue(
        project_id, issue_iid,
        ":hourglass_flowing_sand: **GitBot is looking at this issue...**"
    )
    glc.set_issue_labels(project_id, issue_iid, ["gitbot::thinking"])

    # Gather context for triage
    existing_mrs = _get_existing_mrs_str(project_id, issue_iid)
    recent_comments = _get_recent_comments(project_id, "Issue", issue_iid)
    repo_tree, default_branch = _get_repo_tree_str(project_id)

    system, prompt = triage(
        settings.llm_family,
        title=title,
        description=description,
        target_type="Issue",
        assigner=assigner,
        existing_mrs=existing_mrs,
        recent_comments=recent_comments,
        repo_tree=repo_tree,
    )

    raw = await llm.complete(Task.TRIAGE, system=system, prompt=prompt)

    try:
        decision = _parse_json_response(raw)
    except (json.JSONDecodeError, KeyError):
        log.warning("Failed to parse triage response, falling back to discuss")
        decision = {"action": "discuss"}

    action = decision.get("action", "discuss")
    reasoning = decision.get("reasoning", "")
    log.info("Triage for issue #%s: action=%s reason=%s", issue_iid, action, reasoning)

    if action == "implement":
        # Update placeholder to show we're now building (replaces gitbot::thinking)
        glc.update_note_on_issue(
            project_id, issue_iid, placeholder_id,
            ":hammer_and_wrench: **Working on this** — I'll create a branch and open an MR shortly."
        )
        glc.set_issue_labels(project_id, issue_iid, ["gitbot::working"])
        await _do_implement(
            project_id, issue_iid, title, description,
            repo_tree, default_branch,
            extra_notes=decision.get("implementation_notes", ""),
            placeholder_note_id=placeholder_id,
        )

    elif action == "ask":
        question = decision.get("question", "Could you provide more details?")
        mention = decision.get("mention", f"@{assigner}")
        # Replace placeholder with the question (replaces gitbot::thinking)
        glc.update_note_on_issue(
            project_id, issue_iid, placeholder_id,
            f"{mention} {question}"
        )
        glc.set_issue_labels(project_id, issue_iid, ["gitbot::waiting"])
        await _do_ask(
            project_id, "Issue", issue_iid,
            question=question,
            mention=mention,
            workflow="issue_implementation",
            context={
                "title": title, "description": description,
                "assigner": assigner, "reasoning": reasoning,
            },
            _skip_post=True,  # already posted via placeholder edit
        )

    else:  # discuss
        await _do_discuss(project_id, issue_iid, title, description,
                          placeholder_note_id=placeholder_id)
        _clear_status_label(project_id, issue_iid)


async def _do_implement(
    project_id: int, issue_iid: int, title: str, description: str,
    repo_tree: str, default_branch: str, extra_notes: str = "",
    placeholder_note_id: int | None = None,
) -> None:
    """Create branch, write files, open MR."""
    work_id = state.create_work_item(
        project_id, "Issue", issue_iid, "issue_implementation",
    )

    # If no placeholder was passed (e.g. from follow-up), post one now
    if not placeholder_note_id:
        placeholder_note_id = glc.post_note_on_issue(
            project_id, issue_iid,
            ":hammer_and_wrench: **Working on this** — I'll create a branch and open an MR shortly."
        )
        glc.set_issue_labels(project_id, issue_iid, ["gitbot::working"])

    impl_description = description
    if extra_notes:
        impl_description += f"\n\nAdditional guidance: {extra_notes}"

    system, prompt = implement(
        settings.llm_family,
        title=title,
        description=impl_description,
        repo_tree=repo_tree,
        default_branch=default_branch,
    )

    raw = await llm.complete(Task.IMPLEMENT, system=system, prompt=prompt)

    try:
        plan = _parse_json_response(raw)
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to parse implementation response: %s", e)
        glc.update_note_on_issue(
            project_id, issue_iid, placeholder_note_id,
            f":x: I tried to implement this but had trouble structuring the output. "
            f"Here's what I came up with — I can retry if needed:\n\n{raw[:3000]}"
        )
        _clear_status_label(project_id, issue_iid)
        state.fail_work_item(work_id)
        return

    branch = plan["branch_name"]
    files = plan["files"]

    try:
        glc.create_branch(project_id, branch, ref=default_branch)
        log.info("Created branch %s in project %s", branch, project_id)

        glc.commit_files(project_id, branch, plan["commit_message"], files)
        log.info("Committed %d files to %s", len(files), branch)

        mr_desc_full = f"{plan['mr_description']}\n\nCloses #{issue_iid}"
        mr = glc.create_merge_request(
            project_id, branch, default_branch, plan["mr_title"], mr_desc_full
        )
        log.info("Created MR !%s: %s", mr["iid"], mr["web_url"])

        file_list = ", ".join("`" + f["file_path"] + "`" for f in files)
        glc.update_note_on_issue(
            project_id, issue_iid, placeholder_note_id,
            f":white_check_mark: I've implemented this and opened **!{mr['iid']}** — "
            f"[view the merge request]({mr['web_url']})\n\n"
            f"**Files created/modified:** {file_list}"
        )
        _clear_status_label(project_id, issue_iid)
        state.complete_work_item(work_id)

    except Exception as e:
        log.exception("Failed to create branch/MR for issue #%s", issue_iid)
        glc.update_note_on_issue(
            project_id, issue_iid, placeholder_note_id,
            f":x: I hit an error while creating the branch/MR: `{e}`\n\n"
            f"Branch: `{branch}`, files: {len(files)}"
        )
        _clear_status_label(project_id, issue_iid)
        state.fail_work_item(work_id)


async def _do_ask(
    project_id: int, target_type: str, target_iid: int,
    question: str, mention: str, workflow: str, context: dict,
    _skip_post: bool = False,
) -> None:
    """Post a question and wait for a response."""
    work_id = state.create_work_item(
        project_id, target_type, target_iid, workflow, context=context,
    )

    if not _skip_post:
        comment = f"{mention} {question}"
        if target_type == "Issue":
            glc.post_note_on_issue(project_id, target_iid, comment)
        else:
            glc.post_note_on_mr(project_id, target_iid, comment)

    state.set_pending_response(work_id, question, mention.lstrip("@"), context)
    log.info("Asked question on %s #%s, waiting for response (work_id=%s)",
             target_type, target_iid, work_id)


async def _do_discuss(
    project_id: int, issue_iid: int, title: str, description: str,
    placeholder_note_id: int | None = None,
) -> None:
    """Respond with analysis/discussion."""
    system, prompt = issue_analysis(
        settings.llm_family,
        title=title,
        description=description,
    )
    response = await llm.complete(Task.ISSUE_ANALYSIS, system=system, prompt=prompt)
    if placeholder_note_id:
        glc.update_note_on_issue(project_id, issue_iid, placeholder_note_id, response)
    else:
        glc.post_note_on_issue(project_id, issue_iid, response)
    log.info("Discussed issue #%s in project %s", issue_iid, project_id)


# ---------------------------------------------------------------------------
# MR handlers (unchanged for now)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Mention handler — now checks for pending questions (follow-ups)
# ---------------------------------------------------------------------------

async def handle_mention(payload: dict) -> None:
    """Bot was @mentioned — check if this is a follow-up to a pending question."""
    project_id = payload["project"]["id"]
    note = payload["object_attributes"]
    note_body = note.get("note", "")
    noteable_type = note.get("noteable_type", "")
    discussion_id = note.get("discussion_id")

    noteable_title = ""
    noteable_iid = None
    if noteable_type == "MergeRequest" and "merge_request" in payload:
        noteable_iid = payload["merge_request"]["iid"]
        noteable_title = payload["merge_request"].get("title", "")
    elif noteable_type == "Issue" and "issue" in payload:
        noteable_iid = payload["issue"]["iid"]
        noteable_title = payload["issue"].get("title", "")

    if not noteable_iid:
        log.warning("Could not determine noteable for mention, skipping")
        return

    # Check if we have a pending question on this target
    pending = state.get_pending_question(project_id, noteable_type, noteable_iid)

    if pending:
        await _handle_followup(
            project_id, noteable_type, noteable_iid,
            note_body, pending, discussion_id,
        )
    else:
        await _handle_fresh_mention(
            project_id, noteable_type, noteable_iid,
            noteable_title, note_body, discussion_id,
        )


async def _handle_followup(
    project_id: int, noteable_type: str, noteable_iid: int,
    reply_body: str, pending: dict, discussion_id: str | None,
) -> None:
    """Handle a reply to a question the bot previously asked."""
    log.info("Follow-up reply on %s #%s (work_id=%s)", noteable_type, noteable_iid, pending["id"])

    system, prompt = followup_response(
        settings.llm_family,
        original_question=pending["question"],
        user_reply=reply_body,
        workflow=pending["workflow"],
        context=json.dumps(pending["context"]),
    )

    raw = await llm.complete(Task.TRIAGE, system=system, prompt=prompt)

    try:
        decision = _parse_json_response(raw)
    except (json.JSONDecodeError, KeyError):
        decision = {"action": "discuss"}

    action = decision.get("action", "discuss")
    log.info("Follow-up decision: action=%s", action)

    if action == "implement":
        if noteable_type == "Issue":
            glc.set_issue_labels(project_id, noteable_iid, ["gitbot::working"])
        ctx = pending["context"]
        repo_tree, default_branch = _get_repo_tree_str(project_id)
        state.complete_work_item(pending["id"])
        await _do_implement(
            project_id, noteable_iid,
            ctx.get("title", ""), ctx.get("description", ""),
            repo_tree, default_branch,
            extra_notes=decision.get("implementation_notes", ""),
        )

    elif action == "ask":
        question = decision.get("question", "Could you clarify further?")
        mention = decision.get("mention", f"@{pending.get('asked_user', 'unknown')}")
        state.set_pending_response(
            pending["id"], question, mention.lstrip("@"),
            pending["context"],
        )
        comment = f"{mention} {question}"
        if noteable_type == "Issue":
            glc.post_note_on_issue(project_id, noteable_iid, comment)
        else:
            glc.post_note_on_mr(project_id, noteable_iid, comment)

    else:  # discuss
        state.complete_work_item(pending["id"])
        response = decision.get("reasoning", "Let me look into this further.")
        if noteable_type == "Issue" and discussion_id:
            glc.reply_to_discussion(project_id, noteable_type, noteable_iid, discussion_id, response)
        elif noteable_type == "Issue":
            glc.post_note_on_issue(project_id, noteable_iid, response)
        else:
            glc.post_note_on_mr(project_id, noteable_iid, response)


async def _handle_fresh_mention(
    project_id: int, noteable_type: str, noteable_iid: int,
    noteable_title: str, note_body: str, discussion_id: str | None,
) -> None:
    """Handle a fresh @mention (not a follow-up)."""
    system, prompt = mention_response(
        settings.llm_family,
        note_body=note_body,
        noteable_type=noteable_type,
        noteable_title=noteable_title,
    )

    response = await llm.complete(Task.MENTION_RESPONSE, system=system, prompt=prompt)

    if discussion_id and noteable_type in ("MergeRequest", "Issue"):
        glc.reply_to_discussion(
            project_id, noteable_type, noteable_iid, discussion_id, response
        )
        log.info("Replied to discussion %s", discussion_id)
    else:
        log.warning("Could not find discussion context to reply to, dropping response")
