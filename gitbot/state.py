"""Lightweight state tracking for the bot's pending work.

Stores pending questions, in-progress work, and conversation context
so the bot can resume after asking for clarification or after a restart.
"""

import json
import logging
import sqlite3
import time
from enum import StrEnum
from pathlib import Path

from gitbot.config import settings

log = logging.getLogger(__name__)

_db: sqlite3.Connection | None = None


class Status(StrEnum):
    PENDING_RESPONSE = "pending_response"  # bot asked a question, waiting for reply
    IN_PROGRESS = "in_progress"            # bot is actively working
    COMPLETED = "completed"
    FAILED = "failed"


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        db_path = Path(settings.state_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(db_path))
        _db.row_factory = sqlite3.Row
        _db.execute("""
            CREATE TABLE IF NOT EXISTS work_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_iid INTEGER NOT NULL,
                status TEXT NOT NULL,
                workflow TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '{}',
                question TEXT,
                asked_user TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        _db.execute("""
            CREATE INDEX IF NOT EXISTS idx_work_target
            ON work_items (project_id, target_type, target_iid, status)
        """)
        # Group webhooks GitBot created and manages (issue #35). Our record of
        # what we own, so the admin panel has a source of truth independent of
        # live GitLab reads and can detect drift.
        _db.execute("""
            CREATE TABLE IF NOT EXISTS managed_hooks (
                group_id INTEGER PRIMARY KEY,
                group_path TEXT,
                hook_id INTEGER,
                url TEXT,
                created_at REAL NOT NULL
            )
        """)
        _db.commit()
    return _db


def record_managed_hook(group_id: int, group_path: str, hook_id: int, url: str) -> None:
    """Record (or update) a group webhook GitBot owns."""
    db = _get_db()
    db.execute(
        """INSERT OR REPLACE INTO managed_hooks
           (group_id, group_path, hook_id, url, created_at) VALUES (?, ?, ?, ?, ?)""",
        (group_id, group_path, hook_id, url, time.time()),
    )
    db.commit()


def delete_managed_hook(group_id: int) -> None:
    db = _get_db()
    db.execute("DELETE FROM managed_hooks WHERE group_id=?", (group_id,))
    db.commit()


def list_managed_hooks() -> list[dict]:
    db = _get_db()
    rows = db.execute("SELECT * FROM managed_hooks ORDER BY group_path").fetchall()
    return [dict(r) for r in rows]


def create_work_item(
    project_id: int,
    target_type: str,
    target_iid: int,
    workflow: str,
    context: dict | None = None,
    status: Status = Status.IN_PROGRESS,
) -> int:
    """Create a new work item and return its ID."""
    db = _get_db()
    now = time.time()
    cur = db.execute(
        """INSERT INTO work_items
           (project_id, target_type, target_iid, status, workflow, context, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_id, target_type, target_iid, status, workflow,
         json.dumps(context or {}), now, now),
    )
    db.commit()
    return cur.lastrowid


def set_pending_response(
    work_id: int,
    question: str,
    asked_user: str,
    context: dict | None = None,
    discussion_id: str = "",
) -> None:
    """Mark a work item as waiting for a user response.

    discussion_id is the thread the question lives in — an untagged reply
    in that thread counts as the answer (stored in the context JSON).
    """
    db = _get_db()
    if discussion_id:
        context = (context or {}) | {"discussion_id": discussion_id}
    updates = {"status": Status.PENDING_RESPONSE, "question": question,
               "asked_user": asked_user, "updated_at": time.time()}
    if context is not None:
        updates["context"] = json.dumps(context)
    db.execute(
        """UPDATE work_items
           SET status=?, question=?, asked_user=?, context=COALESCE(?, context), updated_at=?
           WHERE id=?""",
        (updates["status"], question, asked_user,
         updates.get("context"), updates["updated_at"], work_id),
    )
    db.commit()


def complete_work_item(work_id: int) -> None:
    db = _get_db()
    db.execute(
        "UPDATE work_items SET status=?, updated_at=? WHERE id=?",
        (Status.COMPLETED, time.time(), work_id),
    )
    db.commit()


def fail_work_item(work_id: int) -> None:
    db = _get_db()
    db.execute(
        "UPDATE work_items SET status=?, updated_at=? WHERE id=?",
        (Status.FAILED, time.time(), work_id),
    )
    db.commit()


def get_pending_question(
    project_id: int, target_type: str, target_iid: int
) -> dict | None:
    """Get the most recent pending question for a target, if any."""
    db = _get_db()
    row = db.execute(
        """SELECT * FROM work_items
           WHERE project_id=? AND target_type=? AND target_iid=? AND status=?
           ORDER BY updated_at DESC LIMIT 1""",
        (project_id, target_type, target_iid, Status.PENDING_RESPONSE),
    ).fetchone()
    if row:
        return dict(row) | {"context": json.loads(row["context"])}
    return None


def get_active_work(
    project_id: int, target_type: str, target_iid: int
) -> dict | None:
    """Get any active (in_progress or pending) work for a target."""
    db = _get_db()
    row = db.execute(
        """SELECT * FROM work_items
           WHERE project_id=? AND target_type=? AND target_iid=?
             AND status IN (?, ?)
           ORDER BY updated_at DESC LIMIT 1""",
        (project_id, target_type, target_iid,
         Status.IN_PROGRESS, Status.PENDING_RESPONSE),
    ).fetchone()
    if row:
        return dict(row) | {"context": json.loads(row["context"])}
    return None


def get_stale_in_progress(max_age_hours: float = 24) -> list[dict]:
    """Get work items stuck in IN_PROGRESS (likely from a crash).

    Returns items that have been in_progress for longer than expected,
    indicating the bot was interrupted before completing them.
    """
    db = _get_db()
    cutoff = time.time() - (max_age_hours * 3600)
    rows = db.execute(
        """SELECT * FROM work_items
           WHERE status = ? AND updated_at < ?
           ORDER BY updated_at DESC""",
        (Status.IN_PROGRESS, cutoff),
    ).fetchall()
    return [dict(row) | {"context": json.loads(row["context"])} for row in rows]


def get_all_in_progress() -> list[dict]:
    """Get all work items currently in IN_PROGRESS status."""
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM work_items WHERE status = ? ORDER BY updated_at DESC",
        (Status.IN_PROGRESS,),
    ).fetchall()
    return [dict(row) | {"context": json.loads(row["context"])} for row in rows]


def update_context(work_id: int, context: dict) -> None:
    """Merge new keys into a work item's context."""
    db = _get_db()
    row = db.execute("SELECT context FROM work_items WHERE id=?", (work_id,)).fetchone()
    if row:
        existing = json.loads(row["context"])
        existing.update(context)
        db.execute(
            "UPDATE work_items SET context=?, updated_at=? WHERE id=?",
            (json.dumps(existing), time.time(), work_id),
        )
        db.commit()
