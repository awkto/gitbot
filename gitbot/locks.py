"""Per-issue/MR async locking to prevent concurrent processing of the same target."""

import asyncio
import logging

log = logging.getLogger(__name__)

# Lock registry: key is (project_id, target_type, target_iid)
_locks: dict[tuple, asyncio.Lock] = {}


def _key(project_id: int, target_type: str, target_iid: int) -> tuple:
    return (project_id, target_type, target_iid)


def get_lock(project_id: int, target_type: str, target_iid: int) -> asyncio.Lock:
    """Get or create a lock for a specific issue/MR."""
    k = _key(project_id, target_type, target_iid)
    if k not in _locks:
        _locks[k] = asyncio.Lock()
    return _locks[k]


async def acquire(project_id: int, target_type: str, target_iid: int) -> asyncio.Lock:
    """Acquire the lock for a target, waiting if another handler is active."""
    lock = get_lock(project_id, target_type, target_iid)
    if lock.locked():
        log.info(
            "Waiting for lock on %s #%s in project %s",
            target_type, target_iid, project_id,
        )
    await lock.acquire()
    return lock


def cleanup():
    """Remove locks that are no longer held (memory hygiene)."""
    to_remove = [k for k, v in _locks.items() if not v.locked()]
    for k in to_remove:
        del _locks[k]
