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

    # Acquire per-target lock
    lock_key = _extract_lock_key(event_type, payload)
    lock = None
    if lock_key:
        lock = await locks.acquire(*lock_key)

    try:
        # Import here to avoid circular imports
        from gitbot.brain import decide_and_act
        await decide_and_act(sit)
    finally:
        if lock:
            lock.release()
            locks.cleanup()
