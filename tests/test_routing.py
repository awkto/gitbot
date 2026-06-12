"""Routing tests: pending-question matching (#27) and comment-callout gates (#26)."""

import pytest

from gitbot.brain import _answers_pending_question, _should_skip
from gitbot.context import Situation


def _sit(**kw) -> Situation:
    sit = Situation()
    sit.bot_username = "gitbot"
    sit.project_id = 1
    sit.target_type = "Issue"
    sit.target_iid = 7
    sit.event_type = "Note Hook"
    sit.trigger = "comment"
    for k, v in kw.items():
        setattr(sit, k, v)
    return sit


def _pending(asked_user="alice", discussion_id="d1"):
    return {
        "id": 1,
        "asked_user": asked_user,
        "question": "Which group?",
        "context": {"discussion_id": discussion_id} if discussion_id else {},
    }


# --- _answers_pending_question -------------------------------------------

def test_no_pending_question():
    assert not _answers_pending_question(_sit(actor="alice"))


def test_untagged_reply_in_question_thread_from_anyone():
    sit = _sit(actor="bob", discussion_id="d1",
               pending_question=_pending(asked_user="alice"))
    assert _answers_pending_question(sit)


def test_asked_user_comment_outside_thread():
    sit = _sit(actor="alice", discussion_id="other",
               pending_question=_pending(asked_user="alice"))
    assert _answers_pending_question(sit)


def test_other_user_outside_thread_is_not_an_answer():
    sit = _sit(actor="bob", discussion_id="other",
               pending_question=_pending(asked_user="alice"))
    assert not _answers_pending_question(sit)


def test_legacy_pending_without_discussion_id():
    sit = _sit(actor="alice", discussion_id="d9",
               pending_question=_pending(asked_user="alice", discussion_id=None))
    assert _answers_pending_question(sit)


# --- _should_skip note gates ----------------------------------------------

def test_skip_self_triggered():
    assert _should_skip(_sit(actor="gitbot", comment_body="hi"))


def test_skip_system_note():
    assert _should_skip(_sit(actor="alice", note_is_system=True,
                             comment_body="assigned to @gitbot"))


def test_untagged_thread_reply_passes_when_pending():
    sit = _sit(actor="bob", discussion_id="d1", comment_body="use gbtest group",
               pending_question=_pending(asked_user="alice"))
    assert not _should_skip(sit)


def test_untagged_unrelated_comment_skipped_when_pending():
    sit = _sit(actor="bob", discussion_id="other", comment_body="nice weather",
               pending_question=_pending(asked_user="alice"))
    assert _should_skip(sit)


def test_mention_passes_without_pending():
    sit = _sit(actor="bob", discussion_id="d2", comment_body="@gitbot status?")
    assert not _should_skip(sit)


def test_untagged_comment_without_role_or_pending_skipped():
    sit = _sit(actor="bob", discussion_id="d2", comment_body="hello world")
    assert _should_skip(sit)


def test_untagged_comment_with_assignee_role_passes():
    sit = _sit(actor="bob", discussion_id="d2", comment_body="hello world",
               bot_is_assignee=True)
    assert not _should_skip(sit)


# --- NEEDS_INPUT score parsing (question-importance scoring) ---------------

from gitbot.engine_sdk import _parse_needs_input, _asking_rules


def test_parse_needs_input_with_score():
    score, body = _parse_needs_input(
        "NEEDS_INPUT\nSCORE: 9\nWaiting for the target group name.")
    assert score == 9
    assert body == "Waiting for the target group name."


def test_parse_needs_input_without_score():
    score, body = _parse_needs_input("NEEDS_INPUT\nWhich group did you mean?")
    assert score is None
    assert body == "Which group did you mean?"


def test_parse_needs_input_clamps_score():
    score, _ = _parse_needs_input("NEEDS_INPUT\nSCORE: 15\nstuff")
    assert score == 10


def test_asking_rules_contains_scale_and_threshold():
    rules = _asking_rules("@alice")
    assert "9-10 BLOCKING" in rules
    assert "@alice" in rules
    assert "/10" in rules


def test_prompts_format_cleanly():
    from gitbot.engine_sdk import IMPLEMENT_SYSTEM, ORCHESTRATE_SYSTEM
    impl = IMPLEMENT_SYSTEM.format(
        target_iid=1, target_title="t", project_name="p", project_id=1,
        branch_name="b", requester="@u", asking_rules=_asking_rules("@u"))
    orch = ORCHESTRATE_SYSTEM.format(
        gitlab_url="https://x", target_iid=1, target_title="t",
        project_name="p", project_id=1, requester="@u", bot_username="gitbot",
        asking_rules=_asking_rules("@u"))
    assert "SCORE" in impl and "SCORE" in orch
    assert "gitbot::queued" in orch
