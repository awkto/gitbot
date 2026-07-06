"""Tests for comment-follow triggers (#40): which roles make a plain comment
act, and that @mentions/pending-answers always act regardless of config."""

import pytest

from gitbot import brain
from gitbot.config import settings
from gitbot.context import Situation


@pytest.fixture
def cfg(monkeypatch):
    # Defaults: issue-assignee, mr-assignee, mr-author on; mr-reviewer off.
    for k, v in {
        "act_on_issue_assignee_comments": True,
        "act_on_mr_assignee_comments": True,
        "act_on_mr_author_comments": True,
        "act_on_mr_reviewer_comments": False,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    monkeypatch.setattr(settings, "bot_username", "gitbot", raising=False)


def _note(**kw):
    base = dict(event_type="Note Hook", bot_username="gitbot", project_id=1,
                target_iid=5, comment_body="just a thought", trigger="comment")
    return Situation(**{**base, **kw})


def _mock_issue(monkeypatch, assignees):
    monkeypatch.setattr(brain.glc, "get_issue_details",
                        lambda p, i: {"assignees": assignees, "author": "alice"})


def _mock_mr(monkeypatch, assignees=(), author="alice", reviewers=()):
    monkeypatch.setattr(brain.glc, "get_mr_details",
                        lambda p, i: {"assignees": list(assignees), "author": author,
                                      "reviewers": list(reviewers), "source_branch": "b"})


# --- issue comments --------------------------------------------------------

def test_plain_comment_on_assigned_issue_acts(cfg, monkeypatch):
    _mock_issue(monkeypatch, ["gitbot"])
    assert brain._should_skip(_note(target_type="Issue")) is False


def test_plain_comment_on_unassigned_issue_dropped(cfg, monkeypatch):
    _mock_issue(monkeypatch, ["alice"])
    assert brain._should_skip(_note(target_type="Issue")) is True


def test_issue_assignee_follow_can_be_disabled(cfg, monkeypatch):
    monkeypatch.setattr(settings, "act_on_issue_assignee_comments", False, raising=False)
    _mock_issue(monkeypatch, ["gitbot"])
    assert brain._should_skip(_note(target_type="Issue")) is True


def test_mention_on_unassigned_issue_always_acts(cfg, monkeypatch):
    _mock_issue(monkeypatch, ["alice"])
    s = _note(target_type="Issue", comment_body="hey @gitbot look at this",
              trigger="mentioned")
    assert brain._should_skip(s) is False


# --- MR comments -----------------------------------------------------------

def test_plain_comment_on_assigned_mr_acts(cfg, monkeypatch):
    _mock_mr(monkeypatch, assignees=["gitbot"])
    assert brain._should_skip(_note(target_type="MergeRequest")) is False


def test_plain_comment_on_authored_mr_acts(cfg, monkeypatch):
    _mock_mr(monkeypatch, author="gitbot")
    assert brain._should_skip(_note(target_type="MergeRequest")) is False


def test_plain_comment_on_reviewer_only_mr_dropped_by_default(cfg, monkeypatch):
    # The key ask: reviewer-only MR does NOT follow plain comments.
    _mock_mr(monkeypatch, reviewers=["gitbot"], author="alice")
    assert brain._should_skip(_note(target_type="MergeRequest")) is True


def test_reviewer_follow_can_be_enabled(cfg, monkeypatch):
    monkeypatch.setattr(settings, "act_on_mr_reviewer_comments", True, raising=False)
    _mock_mr(monkeypatch, reviewers=["gitbot"], author="alice")
    assert brain._should_skip(_note(target_type="MergeRequest")) is False


def test_mention_on_reviewer_mr_always_acts(cfg, monkeypatch):
    _mock_mr(monkeypatch, reviewers=["gitbot"], author="alice")
    s = _note(target_type="MergeRequest", comment_body="@gitbot ?", trigger="mentioned")
    assert brain._should_skip(s) is False


def test_role_lookup_failure_drops_plain_comment(cfg, monkeypatch):
    def boom(*a):
        raise RuntimeError("api down")
    monkeypatch.setattr(brain.glc, "get_mr_details", boom)
    assert brain._should_skip(_note(target_type="MergeRequest")) is True


# --- system notes / self events still dropped ------------------------------

def test_system_note_dropped(cfg):
    assert brain._should_skip(_note(target_type="Issue", note_is_system=True)) is True


def test_self_comment_dropped(cfg):
    assert brain._should_skip(_note(target_type="Issue", actor="gitbot")) is True
