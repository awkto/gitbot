"""The bot's brain — iteratively gathers context, then decides and acts.

Flow:
1. Start with minimal context (from webhook payload, no API calls)
2. Ask Haiku: "Do you have enough to act, or what do you need?"
3. If it needs more → fetch what it asked for → ask again (up to MAX_ROUNDS)
4. Once ready → execute the chosen action with a stronger model
"""

import json
import logging
import re

from gitbot import llm, gitlab_client as glc, state
from gitbot.config import settings
from gitbot.context import Situation, fetch_source, MAX_ROUNDS, AVAILABLE_SOURCES
from gitbot.models import Task, Tier
from gitbot.prompts import implement, code_review, mr_change_request as mr_change_prompt

log = logging.getLogger(__name__)


GATHER_SYSTEM = """\
You are GitBot's planning module. Your job is to look at an incoming GitLab
event and decide: do you have enough context to act, or do you need more info?

You MUST respond with valid JSON only."""

GATHER_PROMPT = """\
<situation>
{situation}
</situation>

<instructions>
Look at the available information. Decide ONE of:

1. **"ready"** — You have enough context to decide what action to take.
   Proceed to pick an action.

2. **"fetch"** — You need more context before deciding. Request specific sources.

3. **"skip"** — This event doesn't require the bot's attention at all.
   (e.g. a comment from someone else on an unrelated issue)

If "ready", also pick your action and explain your reasoning.
If "fetch", list exactly which sources you need and why.

Available actions (for when ready):
- "create_mr" — Create a new branch, write code, open a merge request
- "push_commits" — Push new commits to an existing MR branch
- "review" — Do a code review of a merge request
- "comment" — Post a comment with analysis or a response
- "ask" — Ask a clarifying question before proceeding
- "nothing" — No action needed

Respond with JSON:
{{
  "status": "ready" | "fetch" | "skip",
  "fetch_sources": ["source1", "source2"],
  "action": "create_mr" | "push_commits" | "review" | "comment" | "ask" | "nothing",
  "reasoning": "Your thinking process",
  "content": "Comment/question text (for comment, ask actions)",
  "mention": "@username (for ask action)",
  "implementation_notes": "What to build (for create_mr, push_commits)"
}}

Only include fields relevant to your status.
Round {round} of {max_rounds}.{already_fetched}
</instructions>"""


async def decide_and_act(sit: Situation) -> None:
    """Main entry: iteratively gather context, then act."""
    if _should_skip(sit):
        return

    # Post placeholder immediately
    placeholder_id = _post_placeholder(sit)

    # Iterative context gathering loop
    decision = None
    for round_num in range(1, MAX_ROUNDS + 1):
        already = ""
        if sit.fetched_sources:
            already = f"\nAlready fetched: {', '.join(sorted(sit.fetched_sources))}"

        prompt = GATHER_PROMPT.format(
            situation=sit.to_prompt(),
            round=round_num,
            max_rounds=MAX_ROUNDS,
            already_fetched=already,
        )

        # Use cheap model for context gathering
        raw = await llm.complete(Task.TRIAGE, system=GATHER_SYSTEM, prompt=prompt)

        try:
            result = _parse_json(raw)
        except (json.JSONDecodeError, KeyError):
            log.warning("Failed to parse gather response (round %d): %s", round_num, raw[:200])
            result = {"status": "ready", "action": "comment", "content": raw}

        status = result.get("status", "ready")
        log.info("Round %d/%d: status=%s", round_num, MAX_ROUNDS, status)

        if status == "skip":
            log.info("Brain says skip for %s #%s", sit.target_type, sit.target_iid)
            _remove_placeholder(sit, placeholder_id)
            return

        if status == "fetch":
            sources = result.get("fetch_sources", [])
            log.info("Fetching: %s", sources)
            for source in sources:
                fetch_source(sit, source)
            continue

        # status == "ready"
        decision = result
        break

    if not decision:
        log.warning("Exhausted %d rounds without deciding, forcing action", MAX_ROUNDS)
        decision = {"action": "comment", "content": "I'm having trouble figuring out what to do here. Could you give me more specific instructions?"}

    action = decision.get("action", "nothing")
    reasoning = decision.get("reasoning", "")
    log.info("Decision for %s #%s: action=%s reason=%s",
             sit.target_type, sit.target_iid, action, reasoning[:120])

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
        _remove_placeholder(sit, placeholder_id)

    # Clean up pending question if we acted
    if sit.pending_question and action != "nothing":
        state.complete_work_item(sit.pending_question["id"])


def _should_skip(sit: Situation) -> bool:
    """Quick pre-LLM filters."""
    if sit.actor == sit.bot_username:
        log.info("Ignoring self-triggered event")
        return True

    if sit.event_type == "Note Hook":
        has_mention = f"@{sit.bot_username}" in sit.comment_body
        has_pending = sit.pending_question is not None
        is_asked_user = has_pending and sit.actor == sit.pending_question.get("asked_user")

        # We might not know bot's role from webhook alone (Note Hooks don't
        # always include assignee info). If we have no mention, no pending
        # question from this user, do a quick API check for MR assignment.
        has_role = sit.bot_is_assignee or sit.bot_is_reviewer or sit.bot_is_author
        if not has_role and sit.target_type == "MergeRequest" and sit.target_iid:
            try:
                details = glc.get_mr_details(sit.project_id, sit.target_iid)
                sit.bot_is_author = (details.get("author") == sit.bot_username)
                sit.bot_is_assignee = (sit.bot_username in details.get("assignees", []))
                has_role = sit.bot_is_assignee or sit.bot_is_author
                sit.mr_source_branch = details.get("source_branch", sit.mr_source_branch)
            except Exception:
                pass

        if not has_mention and not has_role and not is_asked_user:
            log.debug("Ignoring note — no relationship and no pending question")
            return True
        if has_pending and not is_asked_user and not has_mention:
            log.debug("Ignoring note — pending question but from different user")
            return True

    return False


# ---------------------------------------------------------------------------
# Placeholders and labels
# ---------------------------------------------------------------------------

def _post_placeholder(sit: Situation) -> int | None:
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
    if not placeholder_id:
        return
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
    _update_placeholder(sit, placeholder_id,
                        ":hammer_and_wrench: **Working on this** — creating a branch and writing code...")
    if sit.target_type == "Issue":
        glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::working"])

    work_id = state.create_work_item(
        sit.project_id, sit.target_type, sit.target_iid, "create_mr",
    )

    # Make sure we have repo_tree
    if "repo_tree" not in sit.gathered:
        fetch_source(sit, "repo_tree")

    extra_notes = decision.get("implementation_notes", "")
    description = sit.target_description
    if extra_notes:
        description += f"\n\nAdditional guidance: {extra_notes}"

    # Figure out default branch
    gl = glc.get_client()
    project = gl.projects.get(sit.project_id)
    default_branch = project.default_branch or "main"

    system, prompt = implement(
        settings.llm_family,
        title=sit.target_title,
        description=description,
        repo_tree=sit.gathered.get("repo_tree", ""),
        default_branch=default_branch,
    )

    raw = await llm.complete(Task.IMPLEMENT, system=system, prompt=prompt)

    try:
        plan = _parse_json(raw)
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to parse implementation: %s", e)
        _update_placeholder(sit, placeholder_id,
                            f":x: I had trouble structuring my implementation:\n\n{raw[:3000]}")
        _clear_labels(sit)
        state.fail_work_item(work_id)
        return

    branch = plan["branch_name"]
    files = plan["files"]

    try:
        glc.create_branch(sit.project_id, branch, ref=default_branch)
        glc.commit_files(sit.project_id, branch, plan["commit_message"], files)

        mr_desc = plan["mr_description"]
        if sit.target_type == "Issue":
            mr_desc += f"\n\nCloses #{sit.target_iid}"
        mr = glc.create_merge_request(
            sit.project_id, branch, default_branch, plan["mr_title"], mr_desc
        )
        log.info("Created MR !%s: %s", mr["iid"], mr["web_url"])

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
        log.exception("Failed to create MR")
        _update_placeholder(sit, placeholder_id, f":x: Failed to create branch/MR: `{e}`")
        _clear_labels(sit)
        state.fail_work_item(work_id)


async def _do_push_commits(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    branch = sit.mr_source_branch
    if not branch:
        # Try to get it
        if "mr_details" not in sit.fetched_sources:
            fetch_source(sit, "mr_details")
        branch = sit.mr_source_branch

    if not branch:
        _update_placeholder(sit, placeholder_id,
                            ":x: Can't push — no source branch found.")
        return

    _update_placeholder(sit, placeholder_id,
                        f":hammer_and_wrench: **Pushing changes** to `{branch}`...")

    # Ensure we have the diff
    if "diff" not in sit.gathered:
        fetch_source(sit, "diff")
    if "repo_tree" not in sit.gathered:
        fetch_source(sit, "repo_tree")

    request = sit.comment_body or decision.get("implementation_notes", "")

    system, prompt = mr_change_prompt(
        settings.llm_family,
        mr_title=sit.target_title,
        request=request,
        current_diff=sit.gathered.get("diff", ""),
        repo_tree=sit.gathered.get("repo_tree", ""),
        branch=branch,
    )

    raw = await llm.complete(Task.IMPLEMENT, system=system, prompt=prompt)

    try:
        plan = _parse_json(raw)
    except (json.JSONDecodeError, KeyError):
        _update_placeholder(sit, placeholder_id,
                            f":x: Had trouble structuring changes:\n\n{raw[:3000]}")
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
    _update_placeholder(sit, placeholder_id,
                        ":mag: **Reviewing this merge request...**")

    if "diff" not in sit.gathered:
        fetch_source(sit, "diff")

    system, prompt = code_review(
        settings.llm_family,
        title=sit.target_title,
        description=sit.target_description,
        diff=sit.gathered.get("diff", ""),
    )

    response = await llm.complete(Task.CODE_REVIEW, system=system, prompt=prompt)
    _update_placeholder(sit, placeholder_id, response)
    log.info("Reviewed MR !%s", sit.target_iid)


def _do_comment(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    content = decision.get("content", decision.get("reasoning", ""))
    if not content:
        _remove_placeholder(sit, placeholder_id)
        return
    _update_placeholder(sit, placeholder_id, content)
    _clear_labels(sit)
    log.info("Commented on %s #%s", sit.target_type, sit.target_iid)


def _do_ask(sit: Situation, decision: dict, placeholder_id: int | None) -> None:
    question = decision.get("content", decision.get("question", "Could you provide more details?"))
    mention = decision.get("mention", f"@{sit.actor}")

    _update_placeholder(sit, placeholder_id, f"{mention} {question}")

    if sit.target_type == "Issue":
        glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::waiting"])

    work_id = state.create_work_item(
        sit.project_id, sit.target_type, sit.target_iid, "asked_question",
        context={"title": sit.target_title, "description": sit.target_description, "actor": sit.actor},
    )
    state.set_pending_response(work_id, question, mention.lstrip("@"), {
        "title": sit.target_title, "description": sit.target_description, "actor": sit.actor,
    })
    log.info("Asked question on %s #%s, waiting for %s",
             sit.target_type, sit.target_iid, mention)
