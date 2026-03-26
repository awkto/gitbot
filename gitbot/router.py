"""Route incoming GitLab webhook events to the right handler."""

import logging

from gitbot.config import settings
from gitbot import handlers

log = logging.getLogger(__name__)


async def route_event(event_type: str, payload: dict) -> None:
    """Determine what happened and dispatch to the appropriate handler."""
    bot = settings.bot_username

    if event_type == "Issue Hook":
        action = payload.get("object_attributes", {}).get("action")
        assignees = payload.get("assignees", [])
        bot_assigned = any(a.get("username") == bot for a in assignees)
        if action == "update" and bot_assigned:
            await handlers.handle_issue_assigned(payload)
            return

    elif event_type == "Merge Request Hook":
        attrs = payload.get("object_attributes", {})
        action = attrs.get("action")
        assignees = payload.get("assignees", [])
        reviewers = payload.get("reviewers", [])
        bot_is_assignee = any(a.get("username") == bot for a in assignees)
        bot_is_reviewer = any(r.get("username") == bot for r in reviewers)

        if bot_is_reviewer:
            await handlers.handle_mr_review_requested(payload)
            return
        if bot_is_assignee and action in ("open", "update"):
            await handlers.handle_mr_assigned(payload)
            return

    elif event_type == "Note Hook":
        note_body = payload.get("object_attributes", {}).get("note", "")
        if f"@{bot}" in note_body:
            await handlers.handle_mention(payload)
            return

    log.debug("Ignoring event: %s (action=%s)", event_type, payload.get("object_attributes", {}).get("action"))
