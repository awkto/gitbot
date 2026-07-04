"""Tests for true SDK session resume (#25): id plumbing and context merging."""

import json

from gitbot import state
from gitbot.context import Situation
from gitbot.engine_sdk import _resume_id, _workspace_dir


def test_resume_id_only_on_replay():
    sit = Situation(is_replay=True, sdk_session_id="sess-1")
    assert _resume_id(sit) == "sess-1"


def test_no_resume_for_fresh_runs():
    # A fresh task must never resume an old session, even if an id leaked in.
    sit = Situation(is_replay=False, sdk_session_id="sess-1")
    assert _resume_id(sit) is None


def test_no_resume_without_session_id():
    sit = Situation(is_replay=True, sdk_session_id="")
    assert _resume_id(sit) is None


def test_workspace_dir_is_deterministic():
    # Same target → same path (SDK sessions are stored per-cwd; resume needs it).
    a = Situation(project_id=7, target_type="Issue", target_iid=3)
    b = Situation(project_id=7, target_type="Issue", target_iid=3)
    assert _workspace_dir("impl", a) == _workspace_dir("impl", b)
    assert _workspace_dir("impl", a) != _workspace_dir("orch", a)


def test_set_pending_response_merges_context(tmp_path, monkeypatch):
    # Regression: passing context= used to REPLACE the stored context JSON,
    # dropping the original event context and the sdk_session_id.
    monkeypatch.setattr(state, "_db", None)
    monkeypatch.setattr(
        state.settings, "state_db_path", str(tmp_path / "t.db"), raising=False)

    wid = state.create_work_item(1, "Issue", 2, "wf1",
                                 context={"event_type": "Note Hook",
                                          "comment_body": "hi"})
    state.update_context(wid, {"sdk_session_id": "sess-9"})
    state.set_pending_response(wid, "which color?", "alice",
                               context={"score": 8}, discussion_id="d1")

    row = state.get_pending_question(1, "Issue", 2)
    ctx = row["context"]
    assert ctx["event_type"] == "Note Hook"        # original context survives
    assert ctx["sdk_session_id"] == "sess-9"       # resume id survives
    assert ctx["score"] == 8 and ctx["discussion_id"] == "d1"  # new keys added
    state._db = None
