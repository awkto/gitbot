"""Tests for the nightly deep audit (#30): a DONE todo with no work item and
no bot reply is an invisibly lost callout and must be replayed."""

import time

import pytest

from gitbot import state, todos
from gitbot.config import settings


def _todo(**kw):
    base = {
        "id": 1, "action": "mentioned", "target_type": "Issue",
        "target_iid": 5, "target_title": "t", "project_id": 9,
        "body": "@gitbot help", "author": "alice",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                    time.gmtime(time.time() - 3600)),
    }
    return base | kw


@pytest.fixture
def audit_env(monkeypatch):
    replays = []

    async def fake_replay(project_id, target_type, target_iid, action, todo):
        replays.append(todo["id"])

    monkeypatch.setattr(todos, "_replay_todo", fake_replay)
    monkeypatch.setattr(todos, "_mention_handled", lambda *a: False)
    monkeypatch.setattr(todos, "_check_if_handled", lambda *a: False)
    monkeypatch.setattr(state, "has_work_since", lambda *a: False)
    monkeypatch.setattr(settings, "bot_username", "gitbot", raising=False)
    return replays


async def test_lost_mention_is_replayed(monkeypatch, audit_env):
    monkeypatch.setattr(todos.glc, "get_done_todos", lambda: [_todo()])
    assert await todos.deep_audit() == 1
    assert audit_env == [1]


async def test_work_item_means_not_lost(monkeypatch, audit_env):
    monkeypatch.setattr(todos.glc, "get_done_todos", lambda: [_todo()])
    monkeypatch.setattr(state, "has_work_since", lambda *a: True)
    assert await todos.deep_audit() == 0


async def test_bot_reply_means_not_lost(monkeypatch, audit_env):
    monkeypatch.setattr(todos.glc, "get_done_todos", lambda: [_todo()])
    monkeypatch.setattr(todos, "_mention_handled", lambda *a: True)
    assert await todos.deep_audit() == 0


async def test_self_authored_is_skipped(monkeypatch, audit_env):
    monkeypatch.setattr(todos.glc, "get_done_todos",
                        lambda: [_todo(author="gitbot")])
    assert await todos.deep_audit() == 0


async def test_outside_window_is_skipped(monkeypatch, audit_env):
    old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                        time.gmtime(time.time() - 3 * 86400))
    monkeypatch.setattr(todos.glc, "get_done_todos",
                        lambda: [_todo(created_at=old)])
    assert await todos.deep_audit(window_hours=26) == 0


async def test_irrelevant_actions_are_skipped(monkeypatch, audit_env):
    monkeypatch.setattr(todos.glc, "get_done_todos",
                        lambda: [_todo(action="marked")])
    assert await todos.deep_audit() == 0


async def test_assigned_uses_coarse_check(monkeypatch, audit_env):
    monkeypatch.setattr(todos.glc, "get_done_todos",
                        lambda: [_todo(action="assigned")])
    monkeypatch.setattr(todos, "_check_if_handled", lambda *a: True)
    assert await todos.deep_audit() == 0


async def test_replay_failure_does_not_abort_sweep(monkeypatch, audit_env):
    async def boom(*a):
        raise RuntimeError("nope")
    monkeypatch.setattr(todos, "_replay_todo", boom)
    monkeypatch.setattr(todos.glc, "get_done_todos",
                        lambda: [_todo(id=1), _todo(id=2)])
    # both fail to replay, neither counted, no exception escapes
    assert await todos.deep_audit() == 0
