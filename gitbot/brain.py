"""The bot's brain — three-phase architecture:

1. GATHER (Haiku) — iteratively fetch context as needed
2. PLAN (Sonnet) — break task into steps, assign model tier per step
3. EXECUTE (model per step) — run tool calls, cheapest model that can do the job
"""

import json
import logging
import re
from functools import partial

from gitbot import llm, gitlab_client as glc, state
from gitbot.config import settings
from gitbot.context import Situation, fetch_source, MAX_ROUNDS
from gitbot.models import Task, Tier, Family, resolve_model
from gitbot.tools import TOOL_SCHEMAS, execute_tool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1: GATHER — what context do we need? (Haiku)
# ---------------------------------------------------------------------------

GATHER_SYSTEM = """\
You are GitBot's context-gathering module. Look at an incoming GitLab event
and decide: do you have enough context to plan, or do you need more info?

You MUST respond with valid JSON only."""

GATHER_PROMPT = """\
<situation>
{situation}
</situation>

<instructions>
Decide ONE of:
1. **"ready"** — You have enough context. Describe what needs to be done.
2. **"fetch"** — You need more context. Request specific sources.
3. **"skip"** — This event doesn't need the bot's attention.

Respond with JSON:
{{
  "status": "ready" | "fetch" | "skip",
  "fetch_sources": ["source1", "source2"],
  "summary": "What needs to be done (when ready)",
  "reasoning": "Your thinking"
}}

Round {round} of {max_rounds}.{already_fetched}
</instructions>"""


# ---------------------------------------------------------------------------
# Phase 2: PLAN — break into steps with model selection (Sonnet)
# ---------------------------------------------------------------------------

PLAN_SYSTEM = """\
You are GitBot's planning module. Given a task summary and context, break it
into concrete execution steps. For each step, decide which model tier should
execute it based on complexity.

You MUST respond with valid JSON only."""

PLAN_PROMPT = """\
<situation>
{situation}
</situation>

<task_summary>{summary}</task_summary>

<instructions>
Break this task into execution steps. For each step, assign a model tier:

- **"cheap"** (Haiku) — Simple operations: creating issues, adding labels,
  linking items, searching, listing, posting comments, assigning milestones.
  Use for anything that doesn't require creative thinking or code generation.

- **"mid"** (Sonnet) — Code generation, writing descriptions/documentation,
  complex analysis, reviewing code, writing CI/CD configs. Use when the step
  requires understanding code or producing quality text.

- **"strong"** (Opus) — Only for tasks requiring deep reasoning: complex
  architecture decisions, subtle security analysis, large-scale refactoring
  plans. Most tasks do NOT need this.

Default to "cheap" unless the step genuinely needs more capability.
A task that creates 33 issues should be "cheap" — the plan already says what
to create, execution just follows through.

Respond with JSON:
{{
  "steps": [
    {{
      "description": "What to do in this step",
      "tier": "cheap" | "mid" | "strong",
      "tools_needed": ["tool1", "tool2"]
    }}
  ],
  "checklist_markdown": "Progress checklist to post as a comment (use - [ ] format)"
}}
</instructions>"""


# ---------------------------------------------------------------------------
# Phase 3: EXECUTE — run each step with the right model
# ---------------------------------------------------------------------------

STEP_SYSTEM = """\
You are GitBot, executing a specific step of a larger plan.

Project: "{project_name}" (ID: {project_id})

Full context:
{situation}

Overall plan:
{plan_summary}

Your current step: {step_description}

Use the tools to complete THIS STEP ONLY. When done with this step,
send a short text summary of what you accomplished.

Tips:
- Call multiple tools in parallel when operations are independent
- If something fails, note it and move on
- Be efficient — don't overthink simple operations"""


async def decide_and_act(sit: Situation) -> None:
    """Main entry: gather → plan → execute."""
    if _should_skip(sit):
        return

    placeholder_id = _post_placeholder(sit)

    # Phase 1: Gather context (Haiku)
    summary = await _gather_context(sit)
    if summary is None:
        _remove_placeholder(sit, placeholder_id)
        return

    # Phase 2: Plan (Sonnet) — break into steps with model tiers
    plan = await _make_plan(sit, summary)

    if not plan or not plan.get("steps"):
        # Simple task — just execute directly with Sonnet
        _update_placeholder(sit, placeholder_id,
                            ":hammer_and_wrench: **Working on it...**")
        if sit.target_type == "Issue":
            glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::working"])

        result = await _execute_step(sit, summary, Tier.MID, TOOL_SCHEMAS)
        _update_placeholder(sit, placeholder_id, result)
        _clear_labels(sit)
    else:
        # Multi-step plan — post checklist and execute step by step
        checklist = plan.get("checklist_markdown", "")
        if checklist:
            _update_placeholder(sit, placeholder_id, checklist)
        else:
            _update_placeholder(sit, placeholder_id,
                                ":hammer_and_wrench: **Working through the plan...**")

        if sit.target_type == "Issue":
            glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::working"])

        step_results = []
        for i, step in enumerate(plan["steps"]):
            tier = Tier(step.get("tier", "cheap"))
            desc = step.get("description", f"Step {i+1}")
            log.info("Executing step %d/%d [%s]: %s",
                     i + 1, len(plan["steps"]), tier, desc[:80])

            result = await _execute_step(sit, desc, tier, TOOL_SCHEMAS)
            step_results.append(f"**Step {i+1}**: {desc}\n{result}")

            # Update checklist — mark step as done
            if checklist:
                # Replace the first unchecked box with a checked one
                checklist = checklist.replace("- [ ]", "- [x]", 1)
                _update_placeholder(sit, placeholder_id, checklist)

        # Final summary
        final = "\n\n".join(step_results)
        if len(final) > 4000:
            final = final[:4000] + "\n\n*(truncated)*"
        _update_placeholder(sit, placeholder_id,
                            f":white_check_mark: **Done!**\n\n{final}")
        _clear_labels(sit)

    # Clean up pending question if we acted
    if sit.pending_question:
        state.complete_work_item(sit.pending_question["id"])


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

async def _gather_context(sit: Situation) -> str | None:
    """Phase 1: Iterative context gathering with Haiku."""
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

        summary = result.get("summary", result.get("plan", result.get("reasoning", "Handle this event.")))
        log.info("Gather complete for %s #%s: %s", sit.target_type, sit.target_iid, summary[:120])
        return summary

    log.warning("Exhausted %d gather rounds", MAX_ROUNDS)
    return "Do your best to handle this event."


async def _make_plan(sit: Situation, summary: str) -> dict | None:
    """Phase 2: Break task into steps with model tiers (Sonnet)."""
    prompt = PLAN_PROMPT.format(
        situation=sit.to_prompt(),
        summary=summary,
    )

    raw = await llm.complete(Task.TRIAGE, system=PLAN_SYSTEM, prompt=prompt)

    try:
        plan = _parse_json(raw)
    except (json.JSONDecodeError, KeyError):
        log.warning("Failed to parse plan, will execute as single step")
        return None

    steps = plan.get("steps", [])
    if not steps:
        return None

    # Log the plan
    for i, step in enumerate(steps):
        log.info("  Plan step %d [%s]: %s", i + 1, step.get("tier", "?"), step.get("description", "?")[:80])

    return plan


async def _execute_step(sit: Situation, step_description: str, tier: Tier, tools: list[dict]) -> str:
    """Phase 3: Execute a single step with the appropriate model."""
    family = settings.llm_family
    model = resolve_model(family, Task.IMPLEMENT, {tier: resolve_model(family, Task.IMPLEMENT, {Tier.CHEAP: resolve_model(family, Task.CLASSIFY), Tier.MID: resolve_model(family, Task.MENTION_RESPONSE), Tier.STRONG: resolve_model(family, Task.CODE_REVIEW)})})

    # Simpler: just resolve the model for the requested tier directly
    model = settings.tier_overrides() or {}
    model_str = model.get(tier) if model else None
    if not model_str:
        from gitbot.models import FAMILY_DEFAULTS
        model_str = FAMILY_DEFAULTS[family][tier]

    log.info("Executing step [%s] with model %s: %s", tier, model_str, step_description[:80])

    system = STEP_SYSTEM.format(
        project_name=sit.project_name,
        project_id=sit.project_id,
        situation=sit.to_prompt(),
        plan_summary=step_description,
        step_description=step_description,
    )

    if sit.comment_body:
        prompt = f"Original request: {sit.comment_body}\n\nYour step: {step_description}"
    elif sit.trigger == "assigned":
        prompt = f"Assigned to {sit.target_type} #{sit.target_iid}: {sit.target_title}\n\n{sit.target_description}\n\nYour step: {step_description}"
    else:
        prompt = f"Handle: {step_description}"

    executor = partial(execute_tool, project_id=sit.project_id)
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
            args.setdefault("target_type", "issue" if sit.target_type == "Issue" else "merge_request")
            args.setdefault("target_iid", sit.target_iid)
        return executor(tool_name, args)

    actions = await llm.tool_loop_with_model(
        model=model_str,
        system=system,
        prompt=prompt,
        tools=tools,
        execute_fn=wrapped_executor,
    )

    return _summarize_actions(actions, comments_posted)


def _summarize_actions(actions: list[dict], comments_posted: list[str]) -> str:
    if not actions:
        return "*(no actions taken)*"

    if actions[-1]["tool"] == "_text_response":
        text = actions[-1]["result"]
        real_actions = [a for a in actions if a["tool"] not in ("_text_response", "post_comment")]
        if not real_actions:
            return text

    parts = []
    for a in actions:
        if a["tool"] in ("_text_response", "post_comment"):
            continue
        result = a["result"]
        if len(result) > 200:
            result = result[:200] + "..."
        parts.append(f"- **{a['tool']}**: {result}")

    if not parts:
        if actions[-1]["tool"] == "_text_response":
            return actions[-1]["result"]
        return "*(completed)*"

    summary_text = "\n".join(parts)
    final = next((a["result"] for a in reversed(actions) if a["tool"] == "_text_response"), None)
    if final:
        return f"{final}\n\n**Actions:**\n{summary_text}"
    return f"**Actions:**\n{summary_text}"


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
