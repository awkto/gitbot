"""The bot's brain — the outer loop around the Claude Agent SDK workflows.

decide_and_act() owns everything the agent loop can't know: skip gates,
session bookkeeping (one thread per session, state DB rows, labels),
workflow selection via cheap triage, and finish-state handling
(BLOCKED / WAITING / NEEDS_INPUT). The agent loops themselves live in
engine_sdk.
"""

import asyncio
import logging
import uuid

from gitbot import gitlab_client as glc, state
from gitbot.activity import tracker
from gitbot.config import settings
from gitbot.context import Situation

log = logging.getLogger(__name__)


async def decide_and_act(sit: Situation) -> None:
    """Main entry: vet the event, pick a workflow, run it, finish cleanly."""
    if _should_skip(sit):
        tracker.webhook_skipped()
        return

    wf_id = str(uuid.uuid4())[:8]
    target_str = f"{sit.target_type} #{sit.target_iid}"
    return await _decide_and_act(sit, wf_id, target_str)


async def _run_task_workflow(kind: str, sit, wf_id: str,
                             placeholder_id: int | None) -> tuple[str, object]:
    """Run implement/orchestrate with failure-triggered escalation (#31).

    On failure, a cheap diagnosis classifies the attempt: capability →
    retry once, one model tier up; transient → retry once, same tier;
    environment/impossible → the failure stands. Max 2 attempts total; a
    pinned model is never overridden.
    """
    from gitbot import engine_sdk

    runner = (engine_sdk.run_orchestrate if kind == "orchestrate"
              else engine_sdk.run_implement)
    result, ok = await runner(sit, wf_id, placeholder_id)
    if ok is not False or not settings.escalation_enabled:
        return result, ok

    verdict, reason = await engine_sdk.diagnose_failure(sit, kind, result)
    tracker.log("info", f"Failure diagnosis: {verdict} — {reason}", wf_id)
    if verdict == "capability":
        wf = "orchestrate" if kind == "orchestrate" else "implement"
        used = engine_sdk._workflow_model(wf, sit.task_complexity, sit.min_tier)
        bumped = engine_sdk.next_tier(used)
        if not bumped:
            log.info("Escalation: no tier above %s — failure stands", used)
            return result, ok
        sit.min_tier = bumped
    elif verdict != "transient":
        return result, ok  # environment / impossible: a retry can't help

    sit.prior_failure = result[:1500]
    tracker.escalation(wf_id)
    tracker.log("info",
                f"Retrying ({verdict})"
                + (f" one tier up on {sit.min_tier}" if verdict == "capability"
                   else " on the same tier"), wf_id)
    log.info("Escalation retry (%s, %s) for %s #%s",
             verdict, sit.min_tier or "same tier", sit.target_type, sit.target_iid)
    return await runner(sit, wf_id, placeholder_id)


async def _decide_and_act(sit, wf_id: str, target_str: str) -> None:
    wf = tracker.start_workflow(wf_id, sit.trigger, target_str, sit.project_name,
                                target_url=sit.target_web_url)
    tracker.log("info", f"Started: {target_str} ({sit.trigger})", wf_id)

    # Track in state DB so we can resume on restart. The stored event context
    # lets an interrupted session be replayed faithfully — critical for
    # comment callouts, which leave no labels and whose GitLab todo is
    # auto-completed the moment our placeholder comment posts.
    work_id = state.create_work_item(
        sit.project_id, sit.target_type, sit.target_iid, wf_id,
        context={
            "event_type": sit.event_type,
            "trigger": sit.trigger,
            "actor": sit.actor,
            "comment_body": (sit.comment_body or "")[:2000],
            "discussion_id": sit.discussion_id,
        },
    )
    sit._wf_id = wf_id
    sit._work_id = work_id

    from gitbot import engine_sdk

    # For a plain comment the bot only sees via a followed role, decide whether
    # to engage BEFORE announcing itself — an "ignore" verdict stays fully
    # silent (no placeholder, no thread), so following a discussion feels
    # natural instead of the bot replying to everything. @mentions and answers
    # to the bot's own question always engage (never ignorable).
    answers_pending = (sit.event_type == "Note Hook"
                       and _answers_pending_question(sit))
    comment_intent: str | None = None
    if sit.event_type == "Note Hook" and not answers_pending:
        comment_intent = await engine_sdk.classify_comment(
            sit, allow_ignore=(sit.trigger != "mentioned"))
        if comment_intent == "ignore":
            tracker.log("info", f"Observed comment on {target_str} — no action needed", wf_id)
            tracker.finish_workflow(wf_id, "completed")
            state.complete_work_item(work_id)
            return

    placeholder_id = _post_placeholder(sit)

    # Pure answer sessions (comment callouts) must not touch labels: clearing
    # them at the end could erase gitbot::waiting/queued that a parked task
    # depends on. Sessions own labels only if they set them (assignment
    # placeholder, or a comment that escalates to task/steer work).
    owns_labels = sit.event_type != "Note Hook"

    try:
        sdk_result: str | None = None
        sdk_ok = True

        if (sit.trigger in ("mentioned", "comment")
                and sit.target_type == "Issue"
                and answers_pending):
            # Reply to a question the bot asked — continue the parked task
            # with the answer in context, not a conversational response.
            sit.is_replay = True
            kind = await engine_sdk.classify_assigned_issue(sit)
            tracker.add_phase(wf_id, "agent")
            tracker.log("info", f"Answer received — resuming task ({kind})...", wf_id)
            _set_working_label(sit)
            owns_labels = True
            sdk_result, sdk_ok = await _run_task_workflow(
                kind, sit, wf_id, placeholder_id)

        elif sit.trigger in ("mentioned", "comment"):
            # Comment intent triage (classified above, before the placeholder):
            # a plain answer must not churn labels or take over the issue;
            # "steer" adjusts/continues the target's existing work (resume-style,
            # adopting prior state); "task" is a fresh work request handled like
            # an assignment.
            intent = comment_intent or "answer"
            tracker.add_phase(wf_id, "agent")
            if intent in ("task", "steer"):
                if intent == "steer":
                    sit.is_replay = True  # adopt existing state, don't restart
                kind = await engine_sdk.classify_assigned_issue(sit)
                tracker.log("info", f"Comment is a work request ({intent}) — running {kind}...", wf_id)
                _set_working_label(sit)
                owns_labels = True
                sdk_result, sdk_ok = await _run_task_workflow(
                    kind, sit, wf_id, placeholder_id)
            else:
                tracker.log("info", "Running SDK agent loop (answer)...", wf_id)
                sdk_result = await engine_sdk.run_mention(sit, wf_id, placeholder_id)

        elif sit.target_type == "Issue" and sit.trigger in ("assigned", "resumed"):
            kind = await engine_sdk.classify_assigned_issue(sit)
            tracker.add_phase(wf_id, "agent")
            tracker.log("info", f"Running SDK agent loop ({kind})...", wf_id)
            _set_working_label(sit)
            sdk_result, sdk_ok = await _run_task_workflow(
                kind, sit, wf_id, placeholder_id)

        elif (sit.target_type == "MergeRequest"
                and sit.trigger in ("review_requested", "assigned", "resumed")):
            # MR review (github/gitbot#22): inline findings + verdict on Opus.
            tracker.add_phase(wf_id, "agent")
            tracker.log("info", "Running SDK agent loop (review)...", wf_id)
            _set_working_label(sit)
            sdk_result, sdk_ok = await engine_sdk.run_review(
                sit, wf_id, placeholder_id)

        if sdk_result is None:
            # Passed the gates but matched no workflow — nothing to do.
            log.warning("No workflow for event (%s, trigger=%s, target=%s #%s)",
                        sit.event_type, sit.trigger, sit.target_type, sit.target_iid)
            _update_placeholder(sit, placeholder_id, "*(no action needed)*")
            if owns_labels:
                _clear_labels(sit)
            tracker.finish_workflow(wf_id, "completed")
            state.complete_work_item(work_id)
            return

        _update_placeholder(sit, placeholder_id, sdk_result)
        if sit.event_type == "Note Hook" and sit.comment_body:
            # Tick off the GitLab TODO this mention created — the
            # todo ledger is how interrupted callouts are found, so
            # a finished session must positively ack it.
            from gitbot import todos as todos_mod
            await asyncio.to_thread(
                todos_mod.ack_comment_todos, sit.project_id,
                sit.target_type, sit.target_iid, sit.comment_body)
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
                work_id, question=sdk_result[:500], asked_user=sit.actor,
                discussion_id=sit.session_discussion_id,
                context={"score": sit.question_score}
                if sit.question_score else None)
            tracker.log("info",
                        f"Asked for input (importance "
                        f"{sit.question_score or '?'}/10): {target_str}", wf_id)
            tracker.finish_workflow(wf_id, "completed")
            return
        if owns_labels:
            _clear_labels(sit)
        status = "completed" if sdk_ok else "failed"
        tracker.log("info", f"Completed (sdk, {status}): {target_str}", wf_id)
        tracker.finish_workflow(wf_id, status)
        if sdk_ok:
            state.complete_work_item(work_id)
        else:
            state.fail_work_item(work_id)

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

        if owns_labels:
            _clear_labels(sit)
        state.fail_work_item(work_id)

    finally:
        # Close out the pending question only if this session consumed its
        # answer — an unrelated mention must not discard an open question.
        if sit.pending_question and _answers_pending_question(sit):
            state.complete_work_item(sit.pending_question["id"])


# ---------------------------------------------------------------------------
# Skip gates — which events are actionable at all
# ---------------------------------------------------------------------------

def _should_skip(sit: Situation) -> bool:
    """Drop events that must not start a workflow."""
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
        answers_pending = _answers_pending_question(sit)

        # A plain (non-mention) comment acts only when the bot holds a role the
        # operator has configured to follow (#40). An @mention or an answer to
        # the bot's own question always acts, regardless of role/config.
        follows = (has_mention or answers_pending
                   or _comment_role_follows(sit))
        if not follows:
            log.debug("Ignoring note — no mention, no followed role, no pending answer")
            return True
        if has_pending and not answers_pending and not has_mention:
            log.debug("Ignoring note — pending question, but comment is neither "
                      "in its thread nor from the asked user")
            return True

    if sit.event_type == "Merge Request Hook" and sit.bot_is_author:
        if not sit.bot_is_reviewer and sit.trigger != "review_requested":
            log.debug("Ignoring MR event on bot-authored MR !%s", sit.target_iid)
            return True

    return False


def _comment_role_follows(sit: Situation) -> bool:
    """Does the bot hold a role on this comment's target that the operator has
    configured to follow (#40)? Looks the role up live and records it on the
    situation (downstream branches reuse bot_is_* / mr_source_branch)."""
    if not sit.target_iid:
        return False
    if sit.target_type == "Issue":
        try:
            det = glc.get_issue_details(sit.project_id, sit.target_iid)
        except Exception:
            return False
        sit.bot_is_assignee = sit.bot_username in det.get("assignees", [])
        sit.bot_is_author = det.get("author") == sit.bot_username
        return sit.bot_is_assignee and settings.act_on_issue_assignee_comments
    if sit.target_type == "MergeRequest":
        try:
            det = glc.get_mr_details(sit.project_id, sit.target_iid)
        except Exception:
            return False
        sit.bot_is_author = det.get("author") == sit.bot_username
        sit.bot_is_assignee = sit.bot_username in det.get("assignees", [])
        sit.bot_is_reviewer = sit.bot_username in det.get("reviewers", [])
        sit.mr_source_branch = det.get("source_branch", sit.mr_source_branch)
        return (
            (sit.bot_is_author and settings.act_on_mr_author_comments)
            or (sit.bot_is_assignee and settings.act_on_mr_assignee_comments)
            or (sit.bot_is_reviewer and settings.act_on_mr_reviewer_comments)
        )
    return False


def _answers_pending_question(sit: Situation) -> bool:
    """Does this comment answer a question the bot asked (github/gitbot#27)?

    Two ways to answer, no @mention required:
    - a reply in the thread where the question was asked (any user — a
      teammate can answer on the requester's behalf), or
    - any comment on the target by the user the question was addressed to.
    """
    if not sit.pending_question:
        return False
    question_thread = (sit.pending_question.get("context") or {}).get("discussion_id")
    if question_thread and sit.discussion_id == question_thread:
        return True
    return sit.actor == sit.pending_question.get("asked_user")


# ---------------------------------------------------------------------------
# Session comments and labels
# ---------------------------------------------------------------------------

def _post_placeholder(sit: Situation) -> int | None:
    """Open the session's one comment thread.

    One session = one thread: the note created here is the anchor (edited in
    place for status and the final report); everything else the bot says
    during the session is a reply into this thread. No further top-level
    comments are ever posted.
    """
    # Comment callouts don't get a thinking label: a side question must not
    # mark the issue as bot-owned work (labels also drive crash recovery —
    # an interrupted answer should not resurrect the whole issue as a task).
    set_label = sit.event_type != "Note Hook"
    try:
        if sit.trigger == "resumed":
            body = ":arrows_counterclockwise: **GitBot is resuming this task...**"
        else:
            body = ":hourglass_flowing_sand: **GitBot is thinking...**"
        # Comment-triggered sessions live in the triggering comment's thread.
        if sit.event_type == "Note Hook" and sit.discussion_id:
            sit.session_discussion_id = sit.discussion_id
            note_id = glc.reply_to_discussion(
                sit.project_id, sit.target_type, sit.target_iid,
                sit.discussion_id, body)
            return note_id
        # Assignment-triggered sessions start their own thread.
        if sit.target_type in ("Issue", "MergeRequest"):
            discussion_id, note_id = glc.start_discussion(
                sit.project_id, sit.target_type, sit.target_iid, body)
            sit.session_discussion_id = discussion_id
            if set_label:
                if sit.target_type == "Issue":
                    glc.set_issue_labels(sit.project_id, sit.target_iid,
                                         ["gitbot::thinking"])
                else:
                    glc.set_mr_labels(sit.project_id, sit.target_iid,
                                      ["gitbot::thinking"])
            return note_id
    except Exception:
        log.warning("Could not post placeholder comment")
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
            # Keep the one-thread-per-session rule even on failure.
            if sit.session_discussion_id:
                glc.reply_to_discussion(sit.project_id, sit.target_type,
                                        sit.target_iid,
                                        sit.session_discussion_id, body)
            elif sit.target_type == "Issue":
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
        "--- Activity Log ---",
    ]
    events = tracker.get_events(limit=50)
    for event in reversed(events):
        if event.get("workflow_id") == wf_id:
            parts.append(f"  [{event['level']}] {event['message']}")

    parts.append("")
    parts.append("--- Exception ---")
    parts.append(traceback.format_exc())

    return "\n".join(parts)
