"""The bot's brain — iteratively gathers context, then acts via tool calling.

Flow:
1. Start with minimal context (from webhook payload, no API calls)
2. Ask Haiku: "Do you have enough to act, or what do you need?"
3. If it needs more → fetch what it asked for → ask again (up to MAX_ROUNDS)
4. Once ready → hand off to the tool-calling loop with a stronger model
"""

import json
import logging
import re
from functools import partial

from gitbot import llm, gitlab_client as glc, state
from gitbot.config import settings
from gitbot.context import Situation, fetch_source, MAX_ROUNDS
from gitbot.models import Task
from gitbot.tools import TOOL_SCHEMAS, execute_tool

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

1. **"ready"** — You have enough context to act. Describe what you plan to do.
2. **"fetch"** — You need more context before deciding. Request specific sources.
3. **"skip"** — This event doesn't require the bot's attention at all.

If "ready", describe what you'll do and provide any guidance.
If "fetch", list exactly which sources you need and why.

Important: Check the conversation history if available. Don't duplicate work
that was already completed.

Respond with JSON:
{{
  "status": "ready" | "fetch" | "skip",
  "fetch_sources": ["source1", "source2"],
  "plan": "What you intend to do (when ready)",
  "reasoning": "Your thinking process"
}}

Only include fields relevant to your status.
Round {round} of {max_rounds}.{already_fetched}
</instructions>"""


AGENT_SYSTEM = """\
You are GitBot, an AI software developer embedded in a GitLab team.

You work entirely through GitLab using the tools provided. You can create
branches, write code, open merge requests, create issues, manage milestones,
search for issues, post comments, and more.

You are working in project "{project_name}" (ID: {project_id}).

Current context:
{situation}

Plan from triage: {plan}

## How to work

1. **For large tasks**: Start by posting a progress checklist comment using
   post_comment with markdown checkboxes (- [ ] item). Update it periodically
   by posting new comments as you complete sections. This helps the user track
   progress and helps you resume if interrupted.

2. **Parallel tool calls**: When multiple operations are independent (e.g.
   creating 10 issues that don't depend on each other), call multiple tools
   in a single response. Only serialize when there are dependencies (e.g.
   create milestone first, then assign it to issues).

3. **When done**: Send a final text message summarizing what you did.

4. **If something fails**: Note the failure, adapt, and continue with
   what you can do. Report failures in your summary."""


async def decide_and_act(sit: Situation) -> None:
    """Main entry: gather context iteratively, then act via tools."""
    if _should_skip(sit):
        return

    placeholder_id = _post_placeholder(sit)

    # Phase 1: Iterative context gathering (Haiku — cheap)
    plan = await _gather_context(sit)

    if plan is None:
        # skip
        _remove_placeholder(sit, placeholder_id)
        return

    # Phase 2: Execute with tools (Sonnet/Opus — capable)
    _update_placeholder(sit, placeholder_id,
                        ":hammer_and_wrench: **Working on it...**")
    if sit.target_type == "Issue":
        glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::working"])

    result = await _execute_with_tools(sit, plan, placeholder_id)

    # Phase 3: Update placeholder with result
    _update_placeholder(sit, placeholder_id, result)
    _clear_labels(sit)

    # Clean up pending question if we acted
    if sit.pending_question:
        state.complete_work_item(sit.pending_question["id"])


async def _gather_context(sit: Situation) -> str | None:
    """Iterative context loop. Returns the plan string, or None to skip."""
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

        raw = await llm.complete(Task.TRIAGE, system=GATHER_SYSTEM, prompt=prompt)

        try:
            result = _parse_json(raw)
        except (json.JSONDecodeError, KeyError):
            log.warning("Failed to parse gather response (round %d)", round_num)
            return "Respond to the event appropriately."

        status = result.get("status", "ready")
        log.info("Gather round %d/%d: status=%s", round_num, MAX_ROUNDS, status)

        if status == "skip":
            log.info("Brain says skip for %s #%s", sit.target_type, sit.target_iid)
            return None

        if status == "fetch":
            sources = result.get("fetch_sources", [])
            log.info("Fetching: %s", sources)
            for source in sources:
                fetch_source(sit, source)
            continue

        # ready
        plan = result.get("plan", result.get("reasoning", "Handle this event."))
        log.info("Plan for %s #%s: %s", sit.target_type, sit.target_iid, plan[:120])
        return plan

    log.warning("Exhausted %d gather rounds", MAX_ROUNDS)
    return "Do your best to handle this event."


async def _execute_with_tools(sit: Situation, plan: str, placeholder_id: int | None) -> str:
    """Run the tool-calling agentic loop."""
    system = AGENT_SYSTEM.format(
        project_name=sit.project_name,
        project_id=sit.project_id,
        situation=sit.to_prompt(),
        plan=plan,
    )

    # Build the initial prompt based on what triggered this
    if sit.comment_body:
        prompt = f"Respond to this request:\n\n{sit.comment_body}"
    elif sit.trigger == "review_requested":
        prompt = f"Review merge request !{sit.target_iid}: {sit.target_title}"
    elif sit.trigger == "assigned":
        prompt = f"You've been assigned to {sit.target_type} #{sit.target_iid}: {sit.target_title}\n\n{sit.target_description}"
    else:
        prompt = f"Handle this event on {sit.target_type} #{sit.target_iid}: {sit.target_title}"

    # Create the tool executor bound to this project
    executor = partial(execute_tool, project_id=sit.project_id)

    # Wrap executor to intercept post_comment and handle it specially
    comments_posted = []

    def wrapped_executor(tool_name, args):
        if tool_name == "post_comment":
            body = args.get("body", "")
            comments_posted.append(body)
            if sit.target_type == "Issue":
                note_id = glc.post_note_on_issue(sit.project_id, sit.target_iid, body)
            elif sit.target_type == "MergeRequest":
                note_id = glc.post_note_on_mr(sit.project_id, sit.target_iid, body)
            else:
                note_id = 0
            return f"Comment posted (note_id={note_id}). You can update it later with update_comment."
        if tool_name == "update_comment":
            # Fill in target info from context
            args.setdefault("target_type", "issue" if sit.target_type == "Issue" else "merge_request")
            args.setdefault("target_iid", sit.target_iid)
        return executor(tool_name, args)

    actions = await llm.tool_loop(
        Task.IMPLEMENT,
        system=system,
        prompt=prompt,
        tools=TOOL_SCHEMAS,
        execute_fn=wrapped_executor,
    )

    # Build summary from actions
    return _summarize_actions(actions, comments_posted)


def _summarize_actions(actions: list[dict], comments_posted: list[str]) -> str:
    """Build a summary of what the bot did."""
    if not actions:
        return "*(no actions taken)*"

    # If the last action is a text response, use that as the summary
    if actions[-1]["tool"] == "_text_response":
        summary = actions[-1]["result"]
        # If no other actions besides text and comments, just use the text
        real_actions = [a for a in actions if a["tool"] not in ("_text_response", "post_comment")]
        if not real_actions:
            return summary

    # Build a structured summary
    parts = []
    for a in actions:
        if a["tool"] == "_text_response":
            continue
        if a["tool"] == "post_comment":
            continue  # already posted as real comments
        result = a["result"]
        if len(result) > 200:
            result = result[:200] + "..."
        parts.append(f"- **{a['tool']}**: {result}")

    if not parts:
        # Only had text/comments
        if actions[-1]["tool"] == "_text_response":
            return actions[-1]["result"]
        return "*(completed)*"

    summary_text = "\n".join(parts)

    # Include the model's final text if present
    final = next((a["result"] for a in reversed(actions) if a["tool"] == "_text_response"), None)
    if final:
        return f"{final}\n\n**Actions taken:**\n{summary_text}"

    return f"**Actions taken:**\n{summary_text}"


# ---------------------------------------------------------------------------
# Pre-filters and helpers
# ---------------------------------------------------------------------------

def _should_skip(sit: Situation) -> bool:
    if sit.actor == sit.bot_username:
        log.info("Ignoring self-triggered event")
        return True

    if sit.event_type == "Note Hook":
        has_mention = f"@{sit.bot_username}" in sit.comment_body
        has_pending = sit.pending_question is not None
        is_asked_user = has_pending and sit.actor == sit.pending_question.get("asked_user")

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

    # Skip MR events the bot authored (unless review requested)
    if sit.event_type == "Merge Request Hook" and sit.bot_is_author:
        if not sit.bot_is_reviewer and sit.trigger != "review_requested":
            log.debug("Ignoring MR event on bot-authored MR !%s", sit.target_iid)
            return True

    return False


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
