"""Process GitLab todos for crash recovery and tracking.

On startup, we check for pending todos — these are tasks that were assigned
to the bot but may not have been processed (e.g. due to a crash/restart,
or if webhooks were missed).
"""

import logging

from gitbot import gitlab_client as glc

log = logging.getLogger(__name__)

# Map GitLab todo actions to our event types
_ACTION_MAP = {
    "assigned": "assigned",
    "review_requested": "review_requested",
    "directly_addressed": "mentioned",
    "mentioned": "mentioned",
}


async def process_pending_todos() -> None:
    """Check pending todos and process any that need action."""
    try:
        todos = glc.get_pending_todos()
    except Exception:
        log.exception("Failed to fetch pending todos")
        return

    if not todos:
        log.info("No pending todos found")
        return

    log.info("Found %d pending todos", len(todos))

    for todo in todos:
        action = _ACTION_MAP.get(todo["action"], todo["action"])
        target_type = todo["target_type"]
        target_iid = todo["target_iid"]
        project_id = todo["project_id"]

        log.info(
            "Pending todo: %s on %s #%s in project %s — %s",
            action, target_type, target_iid, project_id,
            todo["target_title"],
        )

        # For now, just log pending todos so we can see what was missed.
        # We mark them as done to avoid reprocessing on next restart.
        # In the future, we could replay these as synthetic webhook events.
        #
        # We DON'T auto-replay because:
        # 1. The todo might have already been processed (webhook arrived before crash)
        # 2. Re-processing could create duplicate MRs or comments
        # 3. Better to let the user re-assign or re-mention if needed
        #
        # What we DO is: check if the bot has already commented on the target.
        # If not, it's genuinely missed and we should process it.

        already_handled = _check_if_handled(project_id, target_type, target_iid)
        if already_handled:
            log.info("  -> Already handled, marking todo as done")
            _safe_mark_done(todo["id"])
            continue

        log.warning(
            "  -> MISSED: %s #%s was never handled. Re-processing...",
            target_type, target_iid,
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


async def reconcile() -> None:
    """Periodic sweep: resume orphaned (gitbot::working with no live workflow),
    parked (gitbot::waiting) and queued (gitbot::queued) items. Safe to run
    while workflows are active — targets whose lock is currently held are skipped.
    """
    from gitbot import locks

    found: dict[tuple[int, str, int], dict] = {}
    for label in _WORKING_LABELS + _PARKED_LABELS + _QUEUED_LABELS:
        try:
            for item in glc.find_items_by_label(label):
                key = (item["project_id"], item["target_type"], item["target_iid"])
                found.setdefault(key, {**item, "label": label})
        except Exception:
            log.exception("Reconcile: label search failed for %s", label)

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
            if key not in found:
                found[key] = {
                    "project_id": item["project_id"],
                    "target_type": item["target_type"],
                    "target_iid": item["target_iid"],
                    "title": "",
                    "source": "state_db",
                    "work_id": item["id"],
                }
                log.info(
                    "Found interrupted work via state DB: %s #%s (work_id=%s)",
                    item["target_type"], item["target_iid"], item["id"],
                )
            # Mark the stale DB item as failed regardless — the resume will create a new one
            state.fail_work_item(item["id"])
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
