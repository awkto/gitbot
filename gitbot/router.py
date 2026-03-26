"""Route incoming GitLab webhook events through the brain.

The router is now thin: build context → call brain → done.
All decision-making lives in brain.py.
"""

import logging

from gitbot import locks
from gitbot.context import build_minimal

log = logging.getLogger(__name__)


def _extract_lock_key(event_type: str, payload: dict) -> tuple[int, str, int] | None:
    """Extract (project_id, target_type, target_iid) for locking."""
    project_id = payload.get("project", {}).get("id")
    if not project_id:
        return None

    if event_type == "Issue Hook":
        iid = payload.get("object_attributes", {}).get("iid")
        return (project_id, "Issue", iid) if iid else None

    if event_type == "Merge Request Hook":
        iid = payload.get("object_attributes", {}).get("iid")
        return (project_id, "MergeRequest", iid) if iid else None

    if event_type == "Note Hook":
        noteable_type = payload.get("object_attributes", {}).get("noteable_type")
        if noteable_type == "MergeRequest" and "merge_request" in payload:
            return (project_id, "MergeRequest", payload["merge_request"]["iid"])
        if noteable_type == "Issue" and "issue" in payload:
            return (project_id, "Issue", payload["issue"]["iid"])

    return None


async def route_event(event_type: str, payload: dict) -> None:
    """Build situation, acquire lock, let the brain decide."""
    # Build minimal context (no API calls)
    sit = build_minimal(event_type, payload)

    # Acquire per-target lock — track if we had to wait
    lock_key = _extract_lock_key(event_type, payload)
    lock = None
    waited_for_lock = False
    if lock_key:
        lock_obj = locks.get_lock(*lock_key)
        waited_for_lock = lock_obj.locked()
        lock = await locks.acquire(*lock_key)

    # If we waited, another handler just finished on this target.
    # Pre-fetch conversation history so the brain sees what just happened.
    if waited_for_lock:
        log.info("Lock was contended for %s — pre-fetching conversation history", lock_key)
        from gitbot.context import fetch_source
        fetch_source(sit, "conversation_history")

    try:
        from gitbot.brain import decide_and_act
        await decide_and_act(sit)
    finally:
        if lock:
            lock.release()
            locks.cleanup()
