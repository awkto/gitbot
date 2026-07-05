"""Process GitLab todos for crash recovery and tracking.

On startup, we check for pending todos — these are tasks that were assigned
to the bot but may not have been processed (e.g. due to a crash/restart,
or if webhooks were missed).
"""

import logging
import time

from gitbot import gitlab_client as glc

log = logging.getLogger(__name__)

# Map GitLab todo actions to our event types
_ACTION_MAP = {
    "assigned": "assigned",
    "review_requested": "review_requested",
    "directly_addressed": "mentioned",
    "mentioned": "mentioned",
}


# Don't replay todos younger than this: the webhook for them is probably in
# flight (or its workflow still running). Todos are the catch-up net for
# missed/interrupted events, not the primary trigger path.
_TODO_MIN_AGE_SECONDS = 600


async def process_pending_todos(min_age_seconds: int = 0) -> None:
    """GitLab TODOs as the bot's completion ledger (like a human's TODO page).

    Mentions/assignments create pending todos for the bot automatically;
    a completed session marks its todo done (ack_comment_todos / the
    already-handled checks here). Whatever is still pending after a crash or
    missed webhook is, by definition, unfinished — and gets replayed.
    """
    import time
    from datetime import datetime, timezone

    try:
        todos = glc.get_pending_todos()
    except Exception:
        log.exception("Failed to fetch pending todos")
        return

    if not todos:
        return

    log.info("Found %d pending todos", len(todos))

    from gitbot.config import settings

    for todo in todos:
        action = _ACTION_MAP.get(todo["action"], todo["action"])
        target_type = todo["target_type"]
        target_iid = todo["target_iid"]
        project_id = todo["project_id"]

        # Todos the bot caused itself (e.g. self-assigning a follow-up issue
        # mid-session) must never replay: self-created work enters ONLY via
        # the deliberate gitbot::queued handoff, same as dropped self-webhooks
        # — otherwise the queued issue runs twice (github/gitbot#28).
        if todo.get("author") == settings.bot_username:
            log.info("  -> Self-authored todo on %s #%s, marking done (queued "
                     "label is the only self-handoff)", target_type, target_iid)
            _safe_mark_done(todo["id"])
            continue

        if min_age_seconds:
            try:
                created = datetime.fromisoformat(
                    todo["created_at"].replace("Z", "+00:00"))
                age = time.time() - created.timestamp()
                if age < min_age_seconds:
                    continue  # webhook path is likely still on it
            except Exception:
                pass

        log.info(
            "Pending todo: %s on %s #%s in project %s — %s",
            action, target_type, target_iid, project_id,
            todo["target_title"],
        )

        if action == "mentioned":
            handled = _mention_handled(project_id, target_type, target_iid,
                                       todo.get("body") or "")
        else:
            handled = _check_if_handled(project_id, target_type, target_iid)
        if handled:
            log.info("  -> Already handled, marking todo as done")
            _safe_mark_done(todo["id"])
            continue

        log.warning(
            "  -> MISSED: %s todo on %s #%s was never handled. Re-processing...",
            action, target_type, target_iid,
        )

        try:
            await _replay_todo(project_id, target_type, target_iid, action, todo)
            _safe_mark_done(todo["id"])
        except Exception:
            log.exception("  -> Failed to replay todo %s", todo["id"])


def _check_if_handled(project_id: int, target_type: str, target_iid: int) -> bool:
    """Check if the bot has already posted a comment on this target."""
    from gitbot.config import settings
    bot = settings.bot_username

    gl = glc.get_client()
    project = gl.projects.get(project_id)

    try:
        if target_type == "Issue":
            target = project.issues.get(target_iid)
        elif target_type == "MergeRequest":
            target = project.mergerequests.get(target_iid)
        else:
            return False

        notes = target.notes.list(per_page=50)
        return any(
            n.author.get("username") == bot
            for n in notes
            if not n.system
        )
    except Exception:
        return False


def _find_note_discussion(target, body: str) -> tuple[str, int] | None:
    """Locate the note whose body matches; return (discussion_id, note_id)."""
    if not body:
        return None
    try:
        for d in target.discussions.list(get_all=True):
            for n in d.attributes.get("notes", []):
                if not n.get("system") and (n.get("body") or "").strip() == body.strip():
                    return d.id, n["id"]
    except Exception:
        pass
    return None


def _mention_handled(project_id: int, target_type: str, target_iid: int,
                     body: str) -> bool:
    """A mention todo is handled iff the bot replied in the mentioning
    comment's own thread AFTER it (one thread per session makes this exact —
    any bot comment elsewhere on a busy multi-session issue proves nothing).
    """
    from gitbot.config import settings
    bot = settings.bot_username

    try:
        gl = glc.get_client()
        project = gl.projects.get(project_id)
        if target_type == "Issue":
            target = project.issues.get(target_iid)
        elif target_type == "MergeRequest":
            target = project.mergerequests.get(target_iid)
        else:
            return False

        loc = _find_note_discussion(target, body)
        if loc is None:
            # Mentioning note edited or deleted — can't match; fall back to
            # the coarse check rather than replaying into the wrong context.
            return _check_if_handled(project_id, target_type, target_iid)
        discussion_id, note_id = loc
        disc = target.discussions.get(discussion_id)
        seen_mention = False
        for n in disc.attributes.get("notes", []):
            if n["id"] == note_id:
                seen_mention = True
                continue
            if seen_mention and not n.get("system") \
                    and n.get("author", {}).get("username") == bot:
                return True
        return False
    except Exception:
        return False


def ack_comment_todos(project_id: int, target_type: str, target_iid: int,
                      comment_body: str) -> None:
    """Positive ack: a comment-callout session finished — mark the matching
    pending mention todo(s) done, like a user ticking off their TODO page."""
    try:
        for todo in glc.get_pending_todos():
            if (todo["project_id"] == project_id
                    and todo["target_type"] == target_type
                    and todo["target_iid"] == target_iid
                    and _ACTION_MAP.get(todo["action"]) == "mentioned"
                    and (todo.get("body") or "").strip() == comment_body.strip()):
                _safe_mark_done(todo["id"])
                log.info("Acked mention todo %s on %s #%s",
                         todo["id"], target_type, target_iid)
    except Exception:
        log.warning("Could not ack mention todos", exc_info=True)


async def _replay_todo(
    project_id: int, target_type: str, target_iid: int, action: str, todo: dict
) -> None:
    """Re-process a missed todo by building a Situation and calling the brain."""
    from gitbot.context import Situation
    from gitbot.brain import decide_and_act
    from gitbot.config import settings

    gl = glc.get_client()
    project = gl.projects.get(project_id)

    sit = Situation()
    sit.bot_username = settings.bot_username
    sit.project_id = project_id
    sit.project_name = project.name
    sit.project_web_url = getattr(project, "web_url", "")
    sit.target_type = target_type
    sit.target_iid = target_iid
    sit.actor = "system"  # replayed, not a real user event
    sit.is_replay = True

    if action == "mentioned":
        # A missed comment callout: replay as a Note Hook (answer/steer/task
        # triage), NOT as an assignment — replaying a mention as task work
        # was how interrupted side questions resurrected whole issues.
        if target_type == "Issue":
            t = project.issues.get(target_iid)
        else:
            t = project.mergerequests.get(target_iid)
        sit.target_title = t.title
        sit.target_description = t.description or ""
        sit.target_state = t.state
        sit.event_type = "Note Hook"
        sit.trigger = "mentioned"
        sit.comment_body = todo.get("body") or f"@{settings.bot_username}"
        sit.actor = todo.get("author") or "system"
        loc = _find_note_discussion(t, todo.get("body") or "")
        if loc:
            sit.discussion_id = loc[0]
        from gitbot import state
        sit.pending_question = state.get_pending_question(
            project_id, target_type, target_iid)
        await decide_and_act(sit)
        return

    if target_type == "Issue":
        issue = project.issues.get(target_iid)
        sit.target_title = issue.title
        sit.target_description = issue.description or ""
        sit.target_state = issue.state
        sit.bot_is_assignee = any(a.get("username") == sit.bot_username for a in (issue.assignees or []))
        sit.event_type = "Issue Hook"
        sit.trigger = "assigned"
    elif target_type == "MergeRequest":
        mr = project.mergerequests.get(target_iid)
        sit.target_title = mr.title
        sit.target_description = mr.description or ""
        sit.target_state = mr.state
        sit.mr_source_branch = mr.source_branch
        sit.bot_is_assignee = any(a.get("username") == sit.bot_username for a in (mr.assignees or []))
        sit.bot_is_reviewer = any(r.get("username") == sit.bot_username for r in (mr.reviewers or []))
        sit.bot_is_author = (mr.author.get("username") == sit.bot_username if isinstance(mr.author, dict) else False)
        sit.event_type = "Merge Request Hook"
        sit.trigger = "review_requested" if action == "review_requested" else "assigned"
    else:
        log.info("  -> Don't know how to replay target_type=%s", target_type)
        return

    await decide_and_act(sit)


def _safe_mark_done(todo_id: int) -> None:
    try:
        glc.mark_todo_done(todo_id)
    except Exception:
        log.warning("Failed to mark todo %s as done", todo_id)


# ---------------------------------------------------------------------------
# Resume incomplete work on restart
# ---------------------------------------------------------------------------

_WORKING_LABELS = ["gitbot::working", "gitbot::thinking"]
_PARKED_LABELS = ["gitbot::waiting"]
# Deliberate handoff: the agent labels an issue gitbot::queued to say "pick
# this up as a separate task later" (self-created webhooks are dropped as
# self-triggered, so without this label such issues would never be worked).
_QUEUED_LABELS = ["gitbot::queued"]


async def deep_audit(window_hours: float = 26) -> int:
    """Nightly deep audit (github/gitbot#30): find INVISIBLY lost callouts.

    The known race: GitLab auto-completes the bot's mention todo the moment
    the bot posts ANY comment on the target — including a concurrent
    session's placeholder. If that mention's webhook was also lost, nothing
    pending remains: no todo, no label, no work row. The only trace is a
    DONE todo that nobody actually handled.

    Scan the window's done todos; for each mention/assignment not authored by
    the bot: if no work item was created for the target after the todo AND
    the thread shows no bot reply, replay it. Pure API work — an LLM session
    runs only when something lost is actually found. Returns replay count.
    """
    from datetime import datetime

    from gitbot import state
    from gitbot.config import settings

    try:
        todos = glc.get_done_todos()
    except Exception:
        log.exception("Deep audit: could not list done todos")
        return 0

    cutoff = time.time() - window_hours * 3600
    replayed = 0
    for todo in todos:
        action = _ACTION_MAP.get(todo["action"], todo["action"])
        if action not in ("mentioned", "assigned", "review_requested"):
            continue
        if todo.get("author") == settings.bot_username:
            continue  # self-handoffs enter only via gitbot::queued (#28)
        try:
            created_ts = datetime.fromisoformat(
                todo["created_at"].replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if created_ts < cutoff:
            continue
        project_id, target_type, target_iid = (
            todo["project_id"], todo["target_type"], todo["target_iid"])
        if not (project_id and target_iid):
            continue
        # A work item created around/after the todo = some session took the event.
        if state.has_work_since(project_id, target_type, target_iid,
                                created_ts - 60):
            continue
        if action == "mentioned":
            handled = _mention_handled(project_id, target_type, target_iid,
                                       todo.get("body") or "")
        else:
            handled = _check_if_handled(project_id, target_type, target_iid)
        if handled:
            continue
        log.warning("Deep audit: LOST %s on %s #%s (todo %s done, but no work "
                    "item and no bot reply) — replaying",
                    action, target_type, target_iid, todo["id"])
        try:
            await _replay_todo(project_id, target_type, target_iid, action, todo)
            replayed += 1
        except Exception:
            log.exception("Deep audit: replay failed for todo %s", todo["id"])
    return replayed


async def reconcile() -> None:
    """Periodic sweep: resume orphaned (gitbot::working with no live workflow),
    parked (gitbot::waiting) and queued (gitbot::queued) items. Safe to run
    while workflows are active — targets whose lock is currently held are skipped.
    """
    from gitbot import locks

    # TODO ledger first: pending mention/assignment todos older than the
    # grace window are work the bot never finished (interrupted callouts have
    # no label/state to find them by — the todo is the only trace).
    try:
        await process_pending_todos(min_age_seconds=_TODO_MIN_AGE_SECONDS)
    except Exception:
        log.exception("Reconcile: todo sweep failed")

    # Nightly deep audit (#30) rides the reconcile cadence: once per
    # deep_audit_hours, scan recently-DONE todos for invisibly lost callouts.
    from gitbot.config import settings as _settings
    if _settings.deep_audit_hours > 0:
        from gitbot import state as _state
        try:
            last = float(_state.get_meta("last_deep_audit") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if time.time() - last >= _settings.deep_audit_hours * 3600:
            _state.set_meta("last_deep_audit", str(time.time()))
            try:
                n = await deep_audit(window_hours=_settings.deep_audit_hours + 2)
                log.info("Deep audit complete: %d lost callout(s) replayed", n)
            except Exception:
                log.exception("Deep audit failed")

    found: dict[tuple[int, str, int], dict] = {}
    for label in _WORKING_LABELS + _PARKED_LABELS + _QUEUED_LABELS:
        try:
            for item in glc.find_items_by_label(label):
                key = (item["project_id"], item["target_type"], item["target_iid"])
                found.setdefault(key, {**item, "label": label})
        except Exception:
            log.exception("Reconcile: label search failed for %s", label)

    # State-DB orphans: unlocked IN_PROGRESS rows past the grace period are
    # sessions that died with the process. Comment callouts leave no labels
    # and their GitLab todo auto-completes on the bot's first comment — this
    # row is the ONLY trace of a mid-session interruption.
    try:
        from gitbot import state
        for item in state.get_stale_in_progress(max_age_hours=0.25):
            key = (item["project_id"], item["target_type"], item["target_iid"])
            if key in found:
                # The label resume covers this target — close the dead row
                # so it doesn't replay again after that session finishes.
                state.fail_work_item(item["id"])
            else:
                found[key] = {
                    "project_id": item["project_id"],
                    "target_type": item["target_type"],
                    "target_iid": item["target_iid"],
                    "title": "",
                    "label": None,
                    "work_item": item,
                }
    except Exception:
        log.exception("Reconcile: state DB scan failed")

    if not found:
        return

    for key, info in found.items():
        project_id, target_type, target_iid = key
        if locks.get_lock(project_id, target_type, target_iid).locked():
            log.debug("Reconcile: %s #%s is actively being worked — skipping",
                      target_type, target_iid)
            continue
        log.info("Reconcile: picking up %s #%s (%s)",
                 target_type, target_iid, info.get("label"))
        lock = await locks.acquire(project_id, target_type, target_iid)
        try:
            await _resume_item(project_id, target_type, target_iid, info)
        except Exception:
            log.exception("Reconcile: failed to resume %s #%s", target_type, target_iid)
        finally:
            lock.release()


async def resume_incomplete_work() -> None:
    """Find and resume work that was interrupted by a crash/restart.

    Uses two signals:
    1. GitLab labels (gitbot::working / gitbot::thinking) — primary, zero false positives
    2. State DB IN_PROGRESS items — catches anything labels missed
    """
    from gitbot import state

    found: dict[tuple[int, str, int], dict] = {}  # (project_id, type, iid) -> info

    # --- Signal 1: Label scan across all accessible projects ---
    for label in _WORKING_LABELS:
        try:
            items = glc.find_items_by_label(label)
            for item in items:
                key = (item["project_id"], item["target_type"], item["target_iid"])
                if key not in found:
                    found[key] = {**item, "source": "label"}
                    log.info(
                        "Found interrupted work via label: %s #%s (%s) — %s",
                        item["target_type"], item["target_iid"], label, item["title"],
                    )
        except Exception:
            log.exception("Failed to search for label %s", label)

    # --- Signal 2: State DB — items stuck in IN_PROGRESS ---
    try:
        stale_items = state.get_all_in_progress()
        for item in stale_items:
            key = (item["project_id"], item["target_type"], item["target_iid"])
            if key in found:
                # The label resume covers this target — close the dead row.
                state.fail_work_item(item["id"])
            else:
                found[key] = {
                    "project_id": item["project_id"],
                    "target_type": item["target_type"],
                    "target_iid": item["target_iid"],
                    "title": "",
                    "source": "state_db",
                    "work_item": item,
                }
                log.info(
                    "Found interrupted work via state DB: %s #%s (work_id=%s)",
                    item["target_type"], item["target_iid"], item["id"],
                )
    except Exception:
        log.exception("Failed to check state DB for stale work items")

    if not found:
        log.info("No interrupted work to resume")
        return

    log.info("Resuming %d interrupted work item(s)", len(found))

    for key, info in found.items():
        project_id, target_type, target_iid = key
        try:
            await _resume_item(project_id, target_type, target_iid, info)
        except Exception:
            log.exception(
                "Failed to resume %s #%s in project %s",
                target_type, target_iid, project_id,
            )


async def _resume_item(
    project_id: int, target_type: str, target_iid: int, info: dict
) -> None:
    """Resume a single interrupted work item."""
    from gitbot.context import Situation
    from gitbot.brain import decide_and_act
    from gitbot.config import settings

    gl = glc.get_client()
    project = gl.projects.get(project_id)

    sit = Situation()
    sit.bot_username = settings.bot_username
    sit.project_id = project_id
    sit.project_name = project.name
    sit.project_web_url = getattr(project, "web_url", "")
    sit.target_type = target_type
    sit.target_iid = target_iid
    sit.actor = "system"  # resumed, not a real user event

    # State-DB orphans: close out the dead row first (the replay session
    # creates its own), or every sweep would re-find it.
    if info.get("work_item"):
        from gitbot import state
        state.fail_work_item(info["work_item"]["id"])

    # An orphan with stored Note Hook context is an interrupted comment
    # callout — replay it as the comment it was, NOT as task work on the
    # whole issue.
    ctx = (info.get("work_item") or {}).get("context") or {}
    # The dead session's SDK id (persisted by _drive at session start, #25):
    # the replay resumes that session instead of rebuilding from a snapshot.
    sit.sdk_session_id = ctx.get("sdk_session_id", "")
    if ctx.get("event_type") == "Note Hook" and ctx.get("comment_body"):
        if target_type == "Issue":
            t = project.issues.get(target_iid)
        else:
            t = project.mergerequests.get(target_iid)
        if t.state != "opened":
            return
        sit.target_title = t.title
        sit.target_description = t.description or ""
        sit.target_state = t.state
        sit.event_type = "Note Hook"
        sit.trigger = ctx.get("trigger", "mentioned")
        sit.actor = ctx.get("actor", "system")
        sit.comment_body = ctx["comment_body"]
        sit.discussion_id = ctx.get("discussion_id", "")
        sit.bot_is_assignee = any(
            a.get("username") == sit.bot_username for a in (t.assignees or []))
        sit.pending_question = state.get_pending_question(
            project_id, target_type, target_iid)
        log.info("  -> Replaying interrupted callout on %s #%s",
                 target_type, target_iid)
        await decide_and_act(sit)
        return

    queued = info.get("label") in _QUEUED_LABELS
    if queued:
        # Deliberately queued work, not interrupted work: run it as a fresh
        # assignment (no resume framing/snapshot) and consume the label.
        sit.trigger = "assigned"
        sit.newly_assigned = True
        try:
            if target_type == "Issue":
                glc.remove_issue_labels(project_id, target_iid, _QUEUED_LABELS)
            else:
                glc.remove_mr_labels(project_id, target_iid, _QUEUED_LABELS)
        except Exception:
            log.warning("  -> Could not remove queued label")
    else:
        sit.trigger = "resumed"
        sit.is_replay = True

    if target_type == "Issue":
        issue = project.issues.get(target_iid)
        if issue.state != "opened":
            log.info("  -> Issue #%s is %s, skipping resume", target_iid, issue.state)
            _clear_stale_labels(project_id, target_type, target_iid)
            return
        sit.target_title = issue.title
        sit.target_description = issue.description or ""
        sit.target_state = issue.state
        sit.bot_is_assignee = any(
            a.get("username") == sit.bot_username for a in (issue.assignees or [])
        )
        sit.event_type = "Issue Hook"
    elif target_type == "MergeRequest":
        mr = project.mergerequests.get(target_iid)
        if mr.state not in ("opened",):
            log.info("  -> MR !%s is %s, skipping resume", target_iid, mr.state)
            _clear_stale_labels(project_id, target_type, target_iid)
            return
        sit.target_title = mr.title
        sit.target_description = mr.description or ""
        sit.target_state = mr.state
        sit.mr_source_branch = mr.source_branch
        sit.bot_is_assignee = any(
            a.get("username") == sit.bot_username for a in (mr.assignees or [])
        )
        sit.bot_is_reviewer = any(
            r.get("username") == sit.bot_username for r in (mr.reviewers or [])
        )
        sit.bot_is_author = (
            mr.author.get("username") == sit.bot_username
            if isinstance(mr.author, dict) else False
        )
        sit.event_type = "Merge Request Hook"
    else:
        log.info("  -> Don't know how to resume target_type=%s", target_type)
        return

    if queued:
        # The gitbot::queued label is the explicit handoff signal — honor it
        # even if the creating session forgot to self-assign the issue.
        sit.bot_is_assignee = True

    log.info("  -> %s %s #%s: %s", "Starting queued" if queued else "Resuming",
             target_type, target_iid, sit.target_title)

    # No separate "resuming" comment: each session opens exactly one thread
    # (the placeholder), whose anchor note announces what's happening.
    await decide_and_act(sit)


def _clear_stale_labels(project_id: int, target_type: str, target_iid: int) -> None:
    """Remove gitbot:: labels from a closed/merged item."""
    labels = ["gitbot::thinking", "gitbot::working", "gitbot::waiting",
              "gitbot::queued"]
    try:
        if target_type == "Issue":
            glc.remove_issue_labels(project_id, target_iid, labels)
        elif target_type == "MergeRequest":
            glc.remove_mr_labels(project_id, target_iid, labels)
    except Exception:
        pass
