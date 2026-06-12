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
from gitbot.tools import TOOL_SCHEMAS, execute_tool, get_tools_for_step

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

{gathered_context}

Your current step: {step_description}

CRITICAL: When a tool needs a project_id, milestone_id, epic_iid, or
iteration_id, you MUST use the exact IDs from the registry above.
NEVER guess or invent IDs. If an ID you need is not in the registry,
say so — do not make one up.

If a tool returns TOOL_ERROR, do NOT retry the same call. Try a different
approach or note the failure and move on.

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
    wf = tracker.start_workflow(wf_id, sit.trigger, target_str, sit.project_name,
                                target_url=sit.target_web_url)
    tracker.log("info", f"Started: {target_str} ({sit.trigger})", wf_id)

    # Track in state DB so we can resume on restart
    work_id = state.create_work_item(
        sit.project_id, sit.target_type, sit.target_iid, wf_id,
    )
    sit._wf_id = wf_id
    sit._work_id = work_id

    placeholder_id = _post_placeholder(sit)

    try:
        # SDK engine (#19/#20): mention and implement workflows through a
        # single Claude Agent SDK loop. Other workflows still use the legacy brain.
        if settings.engine == "sdk":
            sdk_result: str | None = None
            sdk_ok = True

            if (sit.trigger in ("mentioned", "comment") and sit.pending_question
                    and sit.target_type == "Issue"):
                # Reply to a question the bot asked — continue the parked task
                # with the answer in context, not a conversational response.
                from gitbot import engine_sdk
                sit.is_replay = True
                kind = await engine_sdk.classify_assigned_issue(sit)
                tracker.add_phase(wf_id, "agent")
                tracker.log("info", f"Answer received — resuming task ({kind})...", wf_id)
                _set_working_label(sit)
                if kind == "orchestrate":
                    sdk_result, sdk_ok = await engine_sdk.run_orchestrate(
                        sit, wf_id, placeholder_id)
                else:
                    sdk_result, sdk_ok = await engine_sdk.run_implement(
                        sit, wf_id, placeholder_id)

            elif sit.trigger in ("mentioned", "comment"):
                from gitbot import engine_sdk
                # Side question vs work request: a plain answer must not churn
                # labels or take over the issue; a work request is handled
                # like an assignment (labels, task workflow, finish states).
                intent = await engine_sdk.classify_comment(sit)
                tracker.add_phase(wf_id, "agent")
                if intent == "task":
                    kind = await engine_sdk.classify_assigned_issue(sit)
                    tracker.log("info", f"Comment is a work request — running {kind}...", wf_id)
                    _set_working_label(sit)
                    if kind == "orchestrate":
                        sdk_result, sdk_ok = await engine_sdk.run_orchestrate(
                            sit, wf_id, placeholder_id)
                    else:
                        sdk_result, sdk_ok = await engine_sdk.run_implement(
                            sit, wf_id, placeholder_id)
                else:
                    tracker.log("info", "Running SDK agent loop (answer)...", wf_id)
                    sdk_result = await engine_sdk.run_mention(sit, wf_id, placeholder_id)

            elif sit.target_type == "Issue" and sit.trigger in ("assigned", "resumed"):
                from gitbot import engine_sdk
                kind = await engine_sdk.classify_assigned_issue(sit)
                tracker.add_phase(wf_id, "agent")
                tracker.log("info", f"Running SDK agent loop ({kind})...", wf_id)
                _set_working_label(sit)
                if kind == "orchestrate":
                    sdk_result, sdk_ok = await engine_sdk.run_orchestrate(
                        sit, wf_id, placeholder_id)
                else:
                    sdk_result, sdk_ok = await engine_sdk.run_implement(
                        sit, wf_id, placeholder_id)

            if sdk_result is not None:
                _update_placeholder(sit, placeholder_id, sdk_result)
                if sdk_ok == "waiting":
                    # Parked: swap working labels for gitbot::waiting so the
                    # reconciliation sweep picks it back up later.
                    _clear_labels(sit)
                    _set_label(sit, "gitbot::waiting")
                    tracker.log("info", f"Parked (waiting): {target_str}", wf_id)
                    tracker.finish_workflow(wf_id, "completed")
                    state.complete_work_item(work_id)
                    return
                if sdk_ok == "needs_input":
                    # Asked the user a question: park until they reply (the
                    # reconciler ignores this label; a Note Hook resumes it).
                    _clear_labels(sit)
                    _set_label(sit, "gitbot::needs-input")
                    state.set_pending_response(
                        work_id, question=sdk_result[:500], asked_user=sit.actor)
                    tracker.log("info", f"Asked for input: {target_str}", wf_id)
                    tracker.finish_workflow(wf_id, "completed")
                    return
                _clear_labels(sit)
                status = "completed" if sdk_ok else "failed"
                tracker.log("info", f"Completed (sdk, {status}): {target_str}", wf_id)
                tracker.finish_workflow(wf_id, status)
                if sdk_ok:
                    state.complete_work_item(work_id)
                else:
                    state.fail_work_item(work_id)
                return

        # Phase 1: Gather context (Haiku)
        tracker.add_phase(wf_id, "gather")
        tracker.log("info", "Gathering context...", wf_id)
        summary = await _gather_context(sit)
        if summary is None:
            _remove_placeholder(sit, placeholder_id)
            tracker.log("info", "Skipped — not relevant", wf_id)
            tracker.finish_workflow(wf_id, "completed")
            return

        tracker.set_gather_summary(wf_id, summary)

        # Phase 2: Plan (Sonnet) — break into steps with model tiers
        tracker.add_phase(wf_id, "plan")
        tracker.log("info", "Planning...", wf_id)
        plan = await _make_plan(sit, summary)

        # Phase 3: Execute
        tracker.add_phase(wf_id, "execute")

        if not plan or not plan.get("steps"):
            _update_placeholder(sit, placeholder_id,
                                ":hammer_and_wrench: **Working on it...**")
            _set_working_label(sit)

            tracker.set_plan(wf_id, 1)
            model_str = _resolve_model_for_tier(Tier.MID)
            tracker.start_step(wf_id, 1, summary[:100], "mid", model_str, len(TOOL_SCHEMAS))
            result, actions = await _execute_step(sit, summary, Tier.MID, TOOL_SCHEMAS)
            tracker.finish_step(wf_id, 1, actions=actions)
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

            _set_working_label(sit)

            step_results = []
            id_registry = {}
            for i, step in enumerate(plan["steps"]):
                tier = Tier(step.get("tier", "cheap"))
                desc = step.get("description", f"Step {i+1}")
                step_tools = step.get("tools_needed")
                model_str = _resolve_model_for_tier(tier)
                tools_for_step = get_tools_for_step(step_tools) if step_tools else TOOL_SCHEMAS
                log.info("Executing step %d/%d [%s]: %s",
                         i + 1, num_steps, tier, desc[:80])
                tracker.log("info", f"Step {i+1}/{num_steps} [{tier}]: {desc[:60]}", wf_id)
                tracker.start_step(wf_id, i + 1, desc, tier, model_str, len(tools_for_step))

                result = await _execute_step_with_escalation(
                    sit, desc, tier, TOOL_SCHEMAS, id_registry, i + 1, num_steps,
                    tools_needed=step_tools,
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
        state.complete_work_item(work_id)

    except Exception as e:
        log.exception("Workflow failed for %s", target_str)
        error_str = str(e)
        is_permission = _is_permission_error(e)

        if is_permission:
            tracker.log("error", f"Permission denied: {error_str}", wf_id)
            tracker.finish_workflow(wf_id, "failed", error_str)
            _post_failure_comment(sit, placeholder_id, "permission", error_str)
        else:
            tracker.log("error", f"Error: {error_str}", wf_id)
            tracker.finish_workflow(wf_id, "failed", error_str)
            debug_text = _build_debug_log(sit, wf_id, e)
            if settings.debug_output:
                tracker.store_debug_log(wf_id, debug_text)
            _post_failure_comment(sit, placeholder_id, "error", error_str, wf_id=wf_id)

        _clear_labels(sit)
        state.fail_work_item(work_id)

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


def _resolve_model_for_tier(tier: Tier) -> str:
    """Resolve the concrete model string for a tier."""
    family = settings.get_llm_family()
    overrides = settings.tier_overrides()
    if overrides and tier in overrides:
        return overrides[tier]
    from gitbot.models import FAMILY_DEFAULTS
    return FAMILY_DEFAULTS[family][tier]


async def _execute_step_with_escalation(
    sit: Situation, step_description: str, tier: Tier, tools: list[dict],
    id_registry: dict | None, step_num: int, total_steps: int,
    tools_needed: list[str] | None = None,
) -> str:
    """Execute a step, escalating to a stronger model if it fails."""
    summary, actions = await _execute_step(
        sit, step_description, tier, tools, id_registry, tools_needed=tools_needed,
    )

    wf_id = sit._wf_id if hasattr(sit, '_wf_id') else ""

    failure_reason = _step_failure_reason(actions)
    if failure_reason:
        next_tier = _TIER_ESCALATION.get(tier)

        # Skip escalation if next tier uses the same model (no point retrying)
        if next_tier and _resolve_model_for_tier(next_tier) == _resolve_model_for_tier(tier):
            log.warning(
                "Step %d/%d failed on %s but %s uses the same model — skipping escalation",
                step_num, total_steps, tier, next_tier,
            )
            next_tier = None

        if next_tier:
            log.warning(
                "Step %d/%d failed on %s — escalating to %s (reason: %s)",
                step_num, total_steps, tier, next_tier, failure_reason,
            )
            tracker.escalation(wf_id)
            tracker.mark_step_escalated(wf_id, step_num)
            tracker.log("warn", f"Step {step_num} failed on {tier}, escalating to {next_tier}: {failure_reason}")

            # Give the stronger model structured failure context
            error_details = _format_failure_context(actions, failure_reason)
            escalation_desc = (
                f"{step_description}\n\n"
                f"PREVIOUS ATTEMPT FAILED on a weaker model.\n{error_details}\n\n"
                f"Please complete this step. If a tool returned an error, try a different approach."
            )
            summary, actions = await _execute_step(
                sit, escalation_desc, next_tier, tools, id_registry,
                tools_needed=tools_needed,
            )

            retry_failure = _step_failure_reason(actions)
            if retry_failure and next_tier == Tier.STRONG:
                log.error("Step %d/%d failed even on strong tier: %s", step_num, total_steps, retry_failure)

    tracker.finish_step(wf_id, step_num, actions=actions)
    return summary


def _step_failure_reason(actions: list[dict]) -> str | None:
    """Analyze raw tool actions to detect failure. Returns reason string or None if OK."""
    if not actions:
        return "no actions taken"

    tool_calls = [a for a in actions if a["tool"] not in ("_text_response", "_empty_response")]
    text_responses = [a for a in actions if a["tool"] == "_text_response"]
    empty_responses = [a for a in actions if a["tool"] == "_empty_response"]

    # Empty responses from the model = model failure
    if empty_responses and not tool_calls and not text_responses:
        return "model returned empty responses"

    # If the model produced a text response or posted a comment, that's a valid completion
    # (e.g. "discuss" or "ask clarification" steps)
    comment_actions = [a for a in tool_calls if a["tool"] == "post_comment"]
    if text_responses or comment_actions:
        # Check if the text response indicates the model gave up
        for resp in text_responses:
            reason = _text_indicates_gave_up(resp["result"])
            if reason:
                return reason
        # Model produced output — this is success for discussion/comment steps
        if not tool_calls or comment_actions:
            return None

    # No tool calls and no text = nothing happened
    if not tool_calls and not text_responses:
        return "no actions taken"

    # Check tool results for errors — use structured 'error' flag if available,
    # fall back to string matching for backwards compatibility
    failed_tools = []
    succeeded_tools = []
    for a in tool_calls:
        if a["tool"] == "post_comment":
            succeeded_tools.append(a["tool"])
            continue
        if a.get("error"):
            failed_tools.append(f"{a['tool']}: {a['result'][:100]}")
        elif isinstance(a["result"], str) and a["result"].startswith("TOOL_ERROR:"):
            failed_tools.append(f"{a['tool']}: {a['result'][:100]}")
        else:
            succeeded_tools.append(a["tool"])

    # All tool calls failed
    if failed_tools and not succeeded_tools:
        return f"all tool calls failed — {failed_tools[0]}"

    # Majority of tool calls failed
    if len(failed_tools) > len(succeeded_tools):
        return f"{len(failed_tools)}/{len(failed_tools) + len(succeeded_tools)} tool calls failed"

    return None


def _text_indicates_gave_up(text: str) -> str | None:
    """Check if a model's text response indicates it gave up on the task.

    Only matches phrases that are clearly the model talking about its own limitations,
    not phrases that could appear in legitimate issue/MR content.
    """
    if not text:
        return None
    lower = text.lower()
    # These are phrases models use when they can't complete a task
    gave_up_patterns = [
        ("limited by tool", "model reported tool limitation"),
        ("manual creation", "model deferred to manual action"),
        ("unable to complete", "model unable to complete"),
        ("i encountered issues", "model reported issues"),
        ("i cannot", "model reported inability"),
        ("i'm unable", "model reported inability"),
        ("tool availability", "model reported tool availability issue"),
    ]
    for pattern, reason in gave_up_patterns:
        if pattern in lower:
            return reason
    return None


def _format_gathered_context(sit: Situation, max_length: int = 6000) -> str:
    """Format the context gathered in Phase 1 for inclusion in step prompts.

    Condenses the Situation's gathered dict into a readable reference section.
    Caps total length to avoid blowing up the prompt.
    """
    if not sit.gathered:
        return ""

    parts = ["## Context gathered during analysis:"]
    total = 0
    for source, content in sit.gathered.items():
        if not content or content.startswith("("):
            continue  # skip empty/error entries
        # Cap individual sources
        capped = content[:2000] + "..." if len(content) > 2000 else content
        entry = f"\n### {source}\n{capped}"
        if total + len(entry) > max_length:
            parts.append("\n*(additional context truncated for brevity)*")
            break
        parts.append(entry)
        total += len(entry)

    return "\n".join(parts) if len(parts) > 1 else ""


def _format_failure_context(actions: list[dict], reason: str) -> str:
    """Build structured failure context for the escalation prompt."""
    lines = [f"Failure reason: {reason}", ""]
    for a in actions:
        if a["tool"] in ("_text_response", "_empty_response"):
            continue
        result_preview = (a["result"] or "")[:150]
        lines.append(f"- {a['tool']}({', '.join(f'{k}={v!r}' for k, v in list(a['args'].items())[:3])})")
        lines.append(f"  Result: {result_preview}")
    return "\n".join(lines)


async def _execute_step(
    sit: Situation, step_description: str, tier: Tier, tools: list[dict],
    id_registry: dict | None = None, tools_needed: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """Phase 3: Execute a single step with the appropriate model.

    Returns (summary_text, raw_actions) so callers can inspect actions for failure detection.
    """
    model_str = _resolve_model_for_tier(tier)

    # Filter tools if the plan specified which ones this step needs
    step_tools = get_tools_for_step(tools_needed) if tools_needed else tools

    log.info("Executing step [%s] with model %s (%d tools): %s",
             tier, model_str, len(step_tools), step_description[:80])

    # Format ID registry as a clean reference table
    if id_registry:
        registry_lines = ["## Resource IDs (use these EXACT values in tool calls):"]
        for name, rid in id_registry.items():
            registry_lines.append(f"  {name} = {rid}")
        registry_str = "\n".join(registry_lines)
    else:
        registry_str = "## Resource IDs: (none yet — this is the first step)"

    # Include gathered context so the executor sees what the gather phase learned
    gathered_context = _format_gathered_context(sit)

    system = STEP_SYSTEM.format(
        project_name=sit.project_name,
        project_id=sit.project_id,
        id_registry=registry_str,
        gathered_context=gathered_context,
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
        tools=step_tools,
        execute_fn=wrapped_executor,
    )

    return _summarize_actions(actions, comments_posted), actions


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

    # System notes ("set status to X", "assigned to Y", "mentioned in !N")
    # are bookkeeping, not requests — processing them causes duplicate workflows.
    if sit.event_type == "Note Hook" and sit.note_is_system:
        log.info("Ignoring system note on %s #%s", sit.target_type, sit.target_iid)
        return True

    # Issue events: only a NEW assignment of the bot is actionable. Any other
    # update on a bot-assigned issue (labels, title, status) would otherwise
    # re-trigger a full workflow on every edit. Replayed todos / resumed work
    # carry no webhook action metadata and are pre-vetted by their callers.
    if sit.event_type == "Issue Hook" and not sit.is_replay:
        is_new_assignment = sit.bot_is_assignee and (
            sit.newly_assigned or (sit.action == "open")
        )
        if not is_new_assignment:
            log.info("Ignoring issue event (action=%s, newly_assigned=%s)",
                     sit.action, sit.newly_assigned)
            return True

    # MR events: actionable only when the bot newly becomes reviewer/assignee
    # (or the MR is opened with the bot already in a role).
    if sit.event_type == "Merge Request Hook" and not sit.is_replay:
        is_new_role = (
            sit.newly_review_requested
            or sit.newly_assigned
            or (sit.action == "open" and (sit.bot_is_reviewer or sit.bot_is_assignee))
        )
        if not is_new_role:
            log.info("Ignoring MR event (action=%s)", sit.action)
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
    # Comment callouts don't get a thinking label: a side question must not
    # mark the issue as bot-owned work (labels also drive crash recovery —
    # an interrupted answer should not resurrect the whole issue as a task).
    set_label = sit.event_type != "Note Hook"
    try:
        body = ":hourglass_flowing_sand: **GitBot is thinking...**"
        # Reply in the triggering comment's thread so the whole exchange stays
        # threaded, rather than dropping a separate top-level comment.
        if sit.event_type == "Note Hook" and sit.discussion_id:
            note_id = glc.reply_to_discussion(
                sit.project_id, sit.target_type, sit.target_iid,
                sit.discussion_id, body)
            return note_id
        if sit.target_type == "Issue":
            note_id = glc.post_note_on_issue(sit.project_id, sit.target_iid, body)
            if set_label:
                glc.set_issue_labels(sit.project_id, sit.target_iid, ["gitbot::thinking"])
            return note_id
        elif sit.target_type == "MergeRequest":
            note_id = glc.post_note_on_mr(sit.project_id, sit.target_iid, body)
            if set_label:
                glc.set_mr_labels(sit.project_id, sit.target_iid, ["gitbot::thinking"])
            return note_id
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


def _set_label(sit: Situation, label: str) -> None:
    """Set a gitbot label on the target (Issue or MR)."""
    try:
        if sit.target_type == "Issue":
            glc.set_issue_labels(sit.project_id, sit.target_iid, [label])
        elif sit.target_type == "MergeRequest":
            glc.set_mr_labels(sit.project_id, sit.target_iid, [label])
    except Exception:
        pass


def _set_working_label(sit: Situation) -> None:
    _set_label(sit, "gitbot::working")


_GITBOT_LABELS = ["gitbot::thinking", "gitbot::working", "gitbot::waiting",
                  "gitbot::needs-input"]


def _clear_labels(sit: Situation) -> None:
    try:
        if sit.target_type == "Issue":
            glc.remove_issue_labels(sit.project_id, sit.target_iid, _GITBOT_LABELS)
        elif sit.target_type == "MergeRequest":
            glc.remove_mr_labels(sit.project_id, sit.target_iid, _GITBOT_LABELS)
    except Exception:
        pass


def _parse_json(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

_PERMISSION_PATTERNS = [
    "403 forbidden", "403 Forbidden",
    "access denied", "Access Denied",
    "insufficient permissions", "Insufficient permissions",
    "not authorized", "Not authorized",
    "401 unauthorized", "401 Unauthorized",
]


def _is_permission_error(exc: Exception) -> bool:
    """Check if an exception is a permission/auth problem."""
    msg = str(exc).lower()
    return any(p.lower() in msg for p in _PERMISSION_PATTERNS)


def _post_failure_comment(
    sit: Situation,
    placeholder_id: int | None,
    failure_type: str,
    error_str: str,
    wf_id: str = "",
) -> None:
    """Post an appropriate failure comment on the issue/MR."""
    if failure_type == "permission":
        body = (
            ":no_entry: **GitBot doesn't have permission to complete this task.**\n\n"
            f"Error: `{error_str[:200]}`\n\n"
            "Please check that the bot's GitLab account has the required access level "
            "for this project and try again."
        )
    else:
        body = ":x: **GitBot encountered an error and couldn't complete this task.**\n\n"
        if settings.debug_output and settings.admin_enabled and wf_id:
            body += (
                f"A debug log is available in the "
                f"[admin panel](/admin) (workflow `{wf_id}`).\n"
            )
        else:
            body += "The team has been notified. You can re-assign to retry.\n"

    if placeholder_id:
        _update_placeholder(sit, placeholder_id, body)
    else:
        try:
            if sit.target_type == "Issue":
                glc.post_note_on_issue(sit.project_id, sit.target_iid, body)
            elif sit.target_type == "MergeRequest":
                glc.post_note_on_mr(sit.project_id, sit.target_iid, body)
        except Exception:
            log.warning("Could not post failure comment")


def _build_debug_log(sit: Situation, wf_id: str, exc: Exception) -> str:
    """Build a debug log string for a failed workflow."""
    import traceback

    parts = [
        f"=== GitBot Debug Log ===",
        f"Workflow: {wf_id}",
        f"Target: {sit.target_type} #{sit.target_iid} — {sit.target_title}",
        f"Project: {sit.project_name} (ID: {sit.project_id})",
        f"Trigger: {sit.trigger} by {sit.actor}",
        f"Event: {sit.event_type}",
        "",
        "--- Gathered Context ---",
    ]
    for source, content in sit.gathered.items():
        preview = content[:500] if content else "(empty)"
        parts.append(f"[{source}]: {preview}")

    parts.append("")
    parts.append("--- Activity Log ---")
    events = tracker.get_events(limit=50)
    for event in reversed(events):
        if event.get("workflow_id") == wf_id:
            parts.append(f"  [{event['level']}] {event['message']}")

    parts.append("")
    parts.append("--- Exception ---")
    parts.append(traceback.format_exc())

    return "\n".join(parts)
