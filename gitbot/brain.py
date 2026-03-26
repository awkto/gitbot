"""The bot's brain — decides what to do given a situation, then does it.

This replaces the old per-handler triage. One LLM call sees the full
context and picks an action from an open-ended menu.
"""

import json
import logging
import re

from gitbot import llm, gitlab_client as glc, state
from gitbot.config import settings
from gitbot.context import Situation
from gitbot.models import Task
from gitbot.prompts import implement, code_review, mr_change_request as mr_change_prompt

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are GitBot, an AI software developer embedded in a GitLab team.
You work entirely through GitLab — creating branches, pushing commits,
opening merge requests, reviewing code, and commenting on issues.

You have full access to the GitLab API. You can create branches, commit files,
open MRs, and push additional commits to existing branches.

You are thoughtful and deliberate. Before acting, you consider the full context:
what's being asked, what already exists, what your role is, and whether you have
enough information to proceed.

You MUST respond with valid JSON only."""

DECIDE_PROMPT = """\
<situation>
{situation}
</situation>

<instructions>
Given this situation, decide what to do. Pick ONE action.

Available actions:
- **"create_mr"** — Create a new branch, write code, and open a merge request.
  Use when: assigned to an issue that asks for code, and no suitable MR exists yet.

- **"push_commits"** — Push new commits to an existing MR branch.
  Use when: someone requests changes on an MR you authored/are assigned to,
  or when you need to add files to an existing MR.

- **"review"** — Do a thorough code review of a merge request.
  Use when: you're added as a reviewer on an MR.

- **"comment"** — Post a comment with analysis, a plan, or a response.
  Use when: the situation calls for discussion rather than code, or you're
  answering a question, or the task isn't a code task.

- **"ask"** — Ask a clarifying question before proceeding.
  Use when: the request is ambiguous, you need a decision from the team,
  or you're missing critical information. Be specific about what you need.

- **"nothing"** — Do nothing. This event doesn't require action from you.
  Use when: the event is not relevant to you, or someone else is handling it.

Think about:
1. What is my role here? (author, assignee, reviewer, mentioned, or just observing)
2. Do I have enough information to act?
3. Is there already work in progress that I should build on?
4. What would a thoughtful team member do in this situation?

Respond with JSON:
{{
  "action": "create_mr" | "push_commits" | "review" | "comment" | "ask" | "nothing",
  "reasoning": "Why you chose this action — show your thinking",
  "content": "The comment/question/review text (for comment, ask, review actions)",
  "mention": "@username (for ask action — who to direct the question to)",
  "implementation_notes": "Guidance for what to build (for create_mr, push_commits)"
}}
</instructions>"""


async def decide_and_act(sit: Situation) -> None:
    """The main entry point: look at the situation, decide, act."""
    # Quick filters — don't even call the LLM for obvious cases
    if _should_skip(sit):
        return

    # Post immediate feedback
    placeholder_id = _post_placeholder(sit)

    # If there's a pending question and the asked user is replying, this is a follow-up
    if sit.pending_question and sit.actor == sit.pending_question.get("asked_user"):
        log.info("Follow-up from %s on %s #%s", sit.actor, sit.target_type, sit.target_iid)

    # Ask the LLM what to do
    situation_text = sit.to_prompt()
    prompt = DECIDE_PROMPT.format(situation=situation_text)

    raw = await llm.complete(Task.TRIAGE, system=SYSTEM_PROMPT, prompt=prompt)

    try:
        decision = _parse_json(raw)
    except (json.JSONDecodeError, KeyError):
        log.warning("Failed to parse brain decision, raw: %s", raw[:200])
        decision = {"action": "comment", "content": raw}

    action = decision.get("action", "nothing")
    reasoning = decision.get("reasoning", "")
    log.info("Decision for %s #%s: action=%s reason=%s",
             sit.target_type, sit.target_iid, action, reasoning[:100])

    # Execute
    if action == "create_mr":
        await _do_create_mr(sit, decision, placeholder_id)
    elif action == "push_commits":
        await _do_push_commits(sit, decision, placeholder_id)
    elif action == "review":
        await _do_review(sit, decision, placeholder_id)
    elif action == "comment":
        _do_comment(sit, decision, placeholder_id)
    elif action == "ask":
        _do_ask(sit, decision, placeholder_id)
    else:
        # nothing — clean up placeholder
        _remove_placeholder(sit, placeholder_id)

    # Clean up pending question if we acted on it
    if sit.pending_question and action != "nothing":
        state.complete_work_item(sit.pending_question["id"])


def _should_skip(sit: Situation) -> bool:
    """Quick checks before calling the LLM."""
    # Bot triggered this event (self-ignore)
    if sit.actor == sit.bot_username:
        log.info("Ignoring self-triggered event")
        return True

    # Note Hook on a target where bot has no relationship and no pending question
    if sit.event_type == "Note Hook":
        has_mention = f"@{sit.bot_username}" in sit.comment_body
        has_role = sit.bot_is_assignee or sit.bot_is_reviewer or sit.bot_is_author
        has_pending = sit.pending_question is not None
        is_asked_user_replying = (
            has_pending and sit.actor == sit.pending_question.get("asked_user")
        )

        if not has_mention and not has_role and not is_asked_user_replying:
            log.debug("Ignoring note — bot has no relationship and no pending question")
            return True

        # Comment on target with pending question but from someone else
        if has_pending and not is_asked_user_replying and not has_mention:
            log.debug("Ignoring note — pending question but from different user")
            return True

    return False


def _post_placeholder(sit: Situation) -> int | None:
    """Post a thinking placeholder and set label."""
    try:
        if sit.target_type == "Issue":
            note_id = glc.post_note_on_issue(
                sit.project_id, sit.target_iid,
                ":hourglass_flowing_sand: **GitBot is thinking...**"
            )
            glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::thinking"])
            return note_id
        elif sit.target_type == "MergeRequest":
            return glc.post_note_on_mr(
                sit.project_id, sit.target_iid,
                ":hourglass_flowing_sand: **GitBot is thinking...**"
            )
    except Exception:
        log.warning("Could not post placeholder")
    return None


def _update_placeholder(sit: Situation, placeholder_id: int | None, body: str) -> None:
    if not placeholder_id:
        return
    try:
        if sit.target_type == "Issue":
            glc.update_note_on_issue(sit.project_id, sit.target_iid, placeholder_id, body)
        elif sit.target_type == "MergeRequest":
            glc.update_note_on_mr(sit.project_id, sit.target_iid, placeholder_id, body)
    except Exception:
        log.warning("Could not update placeholder")


def _remove_placeholder(sit: Situation, placeholder_id: int | None) -> None:
    """Remove placeholder by editing to empty-ish state or deleting."""
    if not placeholder_id:
        return
    # Can't delete notes via python-gitlab easily, just clear it
    _update_placeholder(sit, placeholder_id, "*(resolved)*")
    _clear_labels(sit)


def _clear_labels(sit: Situation) -> None:
    if sit.target_type == "Issue":
        try:
            glc.remove_issue_labels(
                sit.project_id, sit.target_iid,
                ["gitbot::thinking", "gitbot::working", "gitbot::waiting"]
            )
        except Exception:
            pass


def _parse_json(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------

async def _do_create_mr(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    """Create a new branch, implement code, open an MR."""
    _update_placeholder(sit, placeholder_id,
                        ":hammer_and_wrench: **Working on this** — creating a branch and writing code...")
    if sit.target_type == "Issue":
        glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::working"])

    work_id = state.create_work_item(
        sit.project_id, sit.target_type, sit.target_iid, "create_mr",
    )

    extra_notes = decision.get("implementation_notes", "")
    description = sit.target_description
    if extra_notes:
        description += f"\n\nAdditional guidance: {extra_notes}"

    system, prompt = implement(
        settings.llm_family,
        title=sit.target_title,
        description=description,
        repo_tree=sit.repo_tree,
        default_branch=sit.default_branch,
    )

    raw = await llm.complete(Task.IMPLEMENT, system=system, prompt=prompt)

    try:
        plan = _parse_json(raw)
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to parse implementation: %s", e)
        _update_placeholder(sit, placeholder_id,
                            f":x: I had trouble structuring my implementation. Here's what I came up with:\n\n{raw[:3000]}")
        _clear_labels(sit)
        state.fail_work_item(work_id)
        return

    branch = plan["branch_name"]
    files = plan["files"]

    try:
        glc.create_branch(sit.project_id, branch, ref=sit.default_branch)
        glc.commit_files(sit.project_id, branch, plan["commit_message"], files)

        mr_desc = f"{plan['mr_description']}\n\nCloses #{sit.target_iid}" if sit.target_type == "Issue" else plan["mr_description"]
        mr = glc.create_merge_request(
            sit.project_id, branch, sit.default_branch, plan["mr_title"], mr_desc
        )
        log.info("Created MR !%s: %s", mr["iid"], mr["web_url"])

        # Self-assign
        try:
            glc.assign_mr(sit.project_id, mr["iid"], [glc.get_bot_user_id()])
        except Exception:
            pass

        file_list = ", ".join("`" + f["file_path"] + "`" for f in files)
        _update_placeholder(sit, placeholder_id,
                            f":white_check_mark: Implemented and opened **!{mr['iid']}** — "
                            f"[view merge request]({mr['web_url']})\n\n"
                            f"**Files:** {file_list}")
        _clear_labels(sit)
        state.complete_work_item(work_id)

    except Exception as e:
        log.exception("Failed to create MR for %s #%s", sit.target_type, sit.target_iid)
        _update_placeholder(sit, placeholder_id, f":x: Failed to create branch/MR: `{e}`")
        _clear_labels(sit)
        state.fail_work_item(work_id)


async def _do_push_commits(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    """Push new commits to an existing MR branch."""
    branch = sit.mr_source_branch
    if not branch:
        _update_placeholder(sit, placeholder_id,
                            ":x: I can't push commits — no source branch found for this MR.")
        return

    _update_placeholder(sit, placeholder_id,
                        f":hammer_and_wrench: **Pushing changes** to `{branch}`...")

    impl_notes = decision.get("implementation_notes", "")
    request = sit.comment_body or impl_notes

    system, prompt = mr_change_prompt(
        settings.llm_family,
        mr_title=sit.target_title,
        request=request,
        current_diff=sit.mr_diff,
        repo_tree=sit.repo_tree,
        branch=branch,
    )

    raw = await llm.complete(Task.IMPLEMENT, system=system, prompt=prompt)

    try:
        plan = _parse_json(raw)
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to parse push plan: %s", e)
        _update_placeholder(sit, placeholder_id,
                            f":x: Had trouble structuring the changes:\n\n{raw[:3000]}")
        return

    try:
        glc.commit_files(sit.project_id, branch, plan["commit_message"], plan["files"])
        file_list = ", ".join("`" + f["file_path"] + "`" for f in plan["files"])
        _update_placeholder(sit, placeholder_id,
                            f":white_check_mark: Pushed to `{branch}`\n\n"
                            f"**{plan['commit_message']}**\n\nFiles: {file_list}")
        log.info("Pushed %d file(s) to %s", len(plan["files"]), branch)
    except Exception as e:
        log.exception("Failed to push to %s", branch)
        _update_placeholder(sit, placeholder_id, f":x: Failed to push: `{e}`")


async def _do_review(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    """Perform a code review."""
    _update_placeholder(sit, placeholder_id,
                        ":mag: **Reviewing this merge request...**")

    system, prompt = code_review(
        settings.llm_family,
        title=sit.target_title,
        description=sit.target_description,
        diff=sit.mr_diff,
    )

    response = await llm.complete(Task.CODE_REVIEW, system=system, prompt=prompt)
    _update_placeholder(sit, placeholder_id, response)
    log.info("Reviewed MR !%s", sit.target_iid)


def _do_comment(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    """Post a comment — analysis, response, or plan."""
    content = decision.get("content", decision.get("reasoning", ""))
    if not content:
        _remove_placeholder(sit, placeholder_id)
        return

    _update_placeholder(sit, placeholder_id, content)
    _clear_labels(sit)
    log.info("Commented on %s #%s", sit.target_type, sit.target_iid)


def _do_ask(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    """Ask a clarifying question."""
    question = decision.get("content", decision.get("question", "Could you provide more details?"))
    mention = decision.get("mention", f"@{sit.actor}")

    comment = f"{mention} {question}"
    _update_placeholder(sit, placeholder_id, comment)

    if sit.target_type == "Issue":
        glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::waiting"])

    # Track in state DB so we pick up the reply
    work_id = state.create_work_item(
        sit.project_id, sit.target_type, sit.target_iid, "asked_question",
        context={
            "title": sit.target_title,
            "description": sit.target_description,
            "actor": sit.actor,
        },
    )
    state.set_pending_response(work_id, question, mention.lstrip("@"), {
        "title": sit.target_title,
        "description": sit.target_description,
        "actor": sit.actor,
    })
    log.info("Asked question on %s #%s, waiting for reply from %s",
             sit.target_type, sit.target_iid, mention)
