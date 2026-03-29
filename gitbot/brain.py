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
import uuid

from gitbot.activity import tracker
from gitbot.config import settings
from gitbot.context import Situation, fetch_source, MAX_ROUNDS
from gitbot.models import Task, Tier, resolve_model
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
Break this task into execution steps. Each step is executed by a separate
model instance that can only see the ID registry (not previous conversations).

CRITICAL PLANNING RULES:

1. **Make steps atomic and concrete.** Don't say "assign milestones to all 30 issues."
   Instead, break it down: "Assign milestone X to issues #1-#10 in project A",
   "Assign milestone Y to issues #1-#10 in project B", etc. Each step should be
   a small, clear batch that a simple model can follow without complex reasoning.

2. **Include explicit values in step descriptions.** Don't say "distribute epics
   across issues." Say "Assign epic 'Supply Chain' to issues #1,#2,#3 in project
   customer-portal, epic 'Factory Ops' to issues #4,#5,#6..." The executing model
   will have the ID registry with real IDs, but the descriptions should spell out
   the mapping.

3. **One concern per step.** Don't combine "create issues AND assign milestones."
   Split: step A creates issues, step B assigns milestones to those issues.

4. **Creation before assignment.** Always create resources (projects, milestones,
   epics) in earlier steps before steps that assign them to other resources.

MODEL TIERS:

- **"cheap"** — ONLY for self-contained operations with no cross-referencing:
  post a comment, create a single branch, acknowledge the task.

- **"mid"** — Default for most work: creating resources, writing code,
  assignments, anything that references the ID registry.

- **"strong"** — Deep reasoning: architecture, security review, debugging
  failures. Rare.

Respond with JSON:
{{
  "steps": [
    {{
      "description": "Concrete, explicit description of what to do",
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

Originating project: "{project_name}" (ID: {project_id})

{id_registry}

Your current step: {step_description}

CRITICAL: When a tool needs a project_id, milestone_id, epic_iid, or
iteration_id, you MUST use the exact IDs from the registry above.
NEVER guess or invent IDs. If an ID you need is not in the registry,
say so — do not make one up.

When done, list any NEW IDs you created in this format:
CREATED: type name = id

Tips:
- Call multiple tools in parallel when operations are independent
- If something fails, note it and move on"""


async def decide_and_act(sit: Situation) -> None:
    """Main entry: gather → plan → execute."""
    if _should_skip(sit):
        tracker.webhook_skipped()
        return

    wf_id = str(uuid.uuid4())[:8]
    target_str = f"{sit.target_type} #{sit.target_iid}"
    wf = tracker.start_workflow(wf_id, sit.trigger, target_str, sit.project_name)
    tracker.log("info", f"Started: {target_str} ({sit.trigger})", wf_id)

    placeholder_id = _post_placeholder(sit)

    try:
        # Phase 1: Gather context (Haiku)
        tracker.log("info", "Gathering context...", wf_id)
        summary = await _gather_context(sit)
        if summary is None:
            _remove_placeholder(sit, placeholder_id)
            tracker.log("info", "Skipped — not relevant", wf_id)
            tracker.finish_workflow(wf_id, "completed")
            return

        # Phase 2: Plan (Sonnet) — break into steps with model tiers
        tracker.log("info", "Planning...", wf_id)
        plan = await _make_plan(sit, summary)

        if not plan or not plan.get("steps"):
            _update_placeholder(sit, placeholder_id,
                                ":hammer_and_wrench: **Working on it...**")
            if sit.target_type == "Issue":
                glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::working"])

            tracker.set_plan(wf_id, 1)
            result = await _execute_step(sit, summary, Tier.MID, TOOL_SCHEMAS)
            tracker.step_completed(wf_id, settings.get_llm_family())
            _update_placeholder(sit, placeholder_id, result)
            _clear_labels(sit)
        else:
            num_steps = len(plan["steps"])
            tracker.set_plan(wf_id, num_steps)
            tracker.log("info", f"Plan: {num_steps} steps", wf_id)

            checklist = plan.get("checklist_markdown", "")
            if checklist:
                _update_placeholder(sit, placeholder_id, checklist)
            else:
                _update_placeholder(sit, placeholder_id,
                                    ":hammer_and_wrench: **Working through the plan...**")

            if sit.target_type == "Issue":
                glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::working"])

            step_results = []
            id_registry = {}
            for i, step in enumerate(plan["steps"]):
                tier = Tier(step.get("tier", "cheap"))
                desc = step.get("description", f"Step {i+1}")
                log.info("Executing step %d/%d [%s]: %s",
                         i + 1, num_steps, tier, desc[:80])
                tracker.log("info", f"Step {i+1}/{num_steps} [{tier}]: {desc[:60]}", wf_id)

                result = await _execute_step_with_escalation(
                    sit, desc, tier, TOOL_SCHEMAS, id_registry, i + 1, num_steps,
                )
                tracker.step_completed(wf_id, tier)
                step_results.append(f"**Step {i+1}**: {desc}\n{result}")

                _extract_ids_from_result(result, id_registry)

                if checklist:
                    checklist = checklist.replace("- [ ]", "- [x]", 1)
                    _update_placeholder(sit, placeholder_id, checklist)

            # Final summary
            final = "\n\n".join(step_results)
            if len(final) > 4000:
                final = final[:4000] + "\n\n*(truncated)*"
            _update_placeholder(sit, placeholder_id,
                                f":white_check_mark: **Done!**\n\n{final}")
            _clear_labels(sit)

        tracker.log("info", f"Completed: {target_str}", wf_id)
        tracker.finish_workflow(wf_id, "completed")

    except Exception as e:
        log.exception("Workflow failed for %s", target_str)
        tracker.log("error", f"Failed: {e}", wf_id)
        tracker.finish_workflow(wf_id, "failed", str(e))
        raise

    finally:
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

    raw = await llm.complete(Task.PLAN, system=PLAN_SYSTEM, prompt=prompt)

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


_TIER_ESCALATION = {Tier.CHEAP: Tier.MID, Tier.MID: Tier.STRONG}


async def _execute_step_with_escalation(
    sit: Situation, step_description: str, tier: Tier, tools: list[dict],
    id_registry: dict | None, step_num: int, total_steps: int,
) -> str:
    """Execute a step, escalating to a stronger model if it fails."""
    result = await _execute_step(sit, step_description, tier, tools, id_registry)

    if _step_looks_failed(result):
        next_tier = _TIER_ESCALATION.get(tier)
        if next_tier:
            log.warning(
                "Step %d/%d failed on %s — escalating to %s",
                step_num, total_steps, tier, next_tier,
            )
            tracker.escalation(sit._wf_id if hasattr(sit, '_wf_id') else "")
            tracker.log("warn", f"Step {step_num} failed on {tier}, escalating to {next_tier}")
            result = await _execute_step(
                sit,
                f"{step_description}\n\nPREVIOUS ATTEMPT FAILED. The weaker model returned:\n{result[:500]}\n\nPlease complete this step properly.",
                next_tier, tools, id_registry,
            )

            # If still failed on strong, give up gracefully
            if _step_looks_failed(result) and next_tier == Tier.STRONG:
                log.error("Step %d/%d failed even on strong tier", step_num, total_steps)

    return result


def _step_looks_failed(result: str) -> bool:
    """Check tool results (ground truth) for failure, not the model's summary."""
    if not result or result == "*(no actions taken)*" or result == "*(completed)*":
        return True

    # Count actual tool successes vs failures in the result
    success_markers = ["Created ", "Committed ", "Updated ", "Assigned ", "Linked ", "Pushed ", "Triggered "]
    fail_markers = ["Error:", "404 Not Found", "403 Forbidden", "could not", "not found"]
    model_gave_up = [
        "does not include", "not available", "tool availability",
        "manual creation", "cannot be done", "limited by tool",
        "I encountered issues", "unable to complete",
    ]

    lower = result.lower()
    successes = sum(1 for m in success_markers if m.lower() in lower)
    failures = sum(1 for m in fail_markers if m.lower() in lower)
    gave_up = any(m in lower for m in model_gave_up)

    # Failed if: model gave up, or zero successes, or mostly errors
    if gave_up:
        return True
    if successes == 0:
        return True
    if failures > successes:
        return True

    return False


async def _execute_step(sit: Situation, step_description: str, tier: Tier, tools: list[dict], id_registry: dict | None = None) -> str:
    """Phase 3: Execute a single step with the appropriate model."""
    family = settings.get_llm_family()
    overrides = settings.tier_overrides()
    model_str = overrides.get(tier) if overrides else None
    if not model_str:
        from gitbot.models import FAMILY_DEFAULTS
        model_str = FAMILY_DEFAULTS[family][tier]

    log.info("Executing step [%s] with model %s: %s", tier, model_str, step_description[:80])

    # Format ID registry as a clean reference table
    if id_registry:
        registry_lines = ["## Resource IDs (use these EXACT values in tool calls):"]
        for name, rid in id_registry.items():
            registry_lines.append(f"  {name} = {rid}")
        registry_str = "\n".join(registry_lines)
    else:
        registry_str = "## Resource IDs: (none yet — this is the first step)"

    system = STEP_SYSTEM.format(
        project_name=sit.project_name,
        project_id=sit.project_id,
        id_registry=registry_str,
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
    """Summarize actions. ALWAYS includes tool results with IDs so
    subsequent steps can reference them."""
    if not actions:
        return "*(no actions taken)*"

    # Always build the raw results list — this is what gets passed to next steps
    parts = []
    for a in actions:
        if a["tool"] in ("_text_response", "post_comment"):
            continue
        # Keep full results for ID-bearing operations, truncate others
        result = a["result"]
        if a["tool"] in ("create_project", "create_group", "create_issue",
                          "create_milestone", "create_epic", "create_merge_request",
                          "create_iteration_cadence", "create_iteration",
                          "create_branch"):
            # Keep full result — IDs are critical for subsequent steps
            pass
        elif len(result) > 200:
            result = result[:200] + "..."
        parts.append(f"- **{a['tool']}**: {result}")

    # Include model's final text if present
    final = next((a["result"] for a in reversed(actions) if a["tool"] == "_text_response"), None)

    if not parts:
        return final or "*(completed)*"

    summary_text = "\n".join(parts)
    if final:
        return f"{final}\n\n**Actions and IDs:**\n{summary_text}"
    return f"**Actions and IDs:**\n{summary_text}"


def _extract_ids_from_result(result: str, registry: dict) -> None:
    """Parse tool result text and extract IDs into the registry."""

    # Pattern: Created <type>: <path/name> (id=<num>)
    for m in re.finditer(r"Created (\w[\w\s]*?):\s*(.+?)\s*\(id=(\d+)\)", result):
        rtype = m.group(1).strip().lower()
        name = m.group(2).strip().split("/")[-1]  # last path component
        registry[f"{rtype} {name}"] = int(m.group(3))

    # Pattern: Created group milestone: <name> (id=<num>)
    for m in re.finditer(r"Created (\w+ milestone):\s*(.+?)\s*\(id=(\d+)\)", result):
        registry[f"{m.group(1).lower()} {m.group(2).strip()}"] = int(m.group(3))

    # Pattern: Created issue #<iid> (global_id=<num>, project_id=<num>)
    for m in re.finditer(r"Created issue #(\d+) \(global_id=(\d+), project_id=(\d+)\)", result):
        registry[f"issue #{m.group(1)} in project {m.group(3)} global_id"] = int(m.group(2))

    # Pattern: Created epic &<iid>: <title>
    for m in re.finditer(r"Created epic &(\d+):\s*(.+?)(?:\n|$)", result):
        registry[f"epic {m.group(2).strip()}"] = int(m.group(1))

    # Pattern: Created iteration cadence/iteration: <name> (id=<gid>)
    for m in re.finditer(r"Created iteration(?:\s+cadence)?:\s*(.+?)\s*\(id=([^,)]+)", result):
        registry[f"iteration {m.group(1).strip()}"] = m.group(2)

    # Pattern: CREATED: <type> <name> = <id> (model's own output format)
    for m in re.finditer(r"CREATED:\s*(\w+)\s+(.+?)\s*=\s*(\d+)", result):
        registry[f"{m.group(1).lower()} {m.group(2).strip()}"] = int(m.group(3))

    if registry:
        log.debug("ID registry now has %d entries", len(registry))


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
