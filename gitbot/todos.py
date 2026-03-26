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
    sit.target_type = target_type
    sit.target_iid = target_iid
    sit.actor = "system"  # replayed, not a real user event

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
