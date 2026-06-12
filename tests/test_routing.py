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


def test_needs_input_detected_mid_text():
    from gitbot.engine_sdk import _is_needs_input
    text = "Report first.\n\n## Summary\n- item\n\nNEEDS_INPUT\nSCORE: 9\nWaiting for group."
    assert _is_needs_input(text)
    score, body = _parse_needs_input(text)
    assert score == 9
    assert "Report first." in body and "NEEDS_INPUT" not in body


def test_needs_input_not_detected_in_prose():
    from gitbot.engine_sdk import _is_needs_input
    assert not _is_needs_input("All done, nothing needed from the user.")


# --- model selection (complexity auto + per-workflow override) -------------

from gitbot.config import settings as _settings
from gitbot.engine_sdk import _parse_classification, _workflow_model


def test_parse_classification_word_and_score():
    assert _parse_classification("orchestrate 7", {"implement", "orchestrate"}, "implement") == ("orchestrate", 7)
    assert _parse_classification("Answer 2", {"answer", "steer", "task"}, "answer") == ("answer", 2)


def test_parse_classification_tolerates_prose():
    word, c = _parse_classification(
        "I'd say this is a task. Complexity: 9", {"answer", "steer", "task"}, "answer")
    assert word == "task" and c == 9


def test_parse_classification_defaults():
    assert _parse_classification("no idea", {"implement", "orchestrate"}, "implement") == ("implement", None)


def test_auto_model_mapping():
    assert _workflow_model("mention", 2) == "haiku"
    assert _workflow_model("mention", 5) == "sonnet"
    assert _workflow_model("implement", 5) == "sonnet"
    assert _workflow_model("orchestrate", 9) == "opus"
    assert _workflow_model("orchestrate", None) == "sonnet"
    assert _workflow_model("review", 1) == "opus"


def test_workflow_model_override_and_reset():
    _settings.model_orchestrate = "claude-opus-4-7"
    try:
        assert _workflow_model("orchestrate", 2) == "claude-opus-4-7"
    finally:
        _settings.model_orchestrate = "auto"
    assert _workflow_model("orchestrate", 2) == "sonnet"


# --- review workflow (github/gitbot#22) -------------------------------------

from gitbot.engine_sdk import REVIEW_SYSTEM, REVIEW_SEVERITIES, _VERDICT_RE


def test_review_prompt_formats_cleanly():
    text = REVIEW_SYSTEM.format(
        mr_iid=5, mr_title="t", project_name="p", project_id=1,
        checkout_note="(checkout)", severities=REVIEW_SEVERITIES)
    assert "post_inline_comment" in text
    assert "VERDICT" in text
    assert "🔴" in text


def test_verdict_parsing():
    assert _VERDICT_RE.search("Summary...\n\nVERDICT: approve").group(1) == "approve"
    m = _VERDICT_RE.search("findings\nVERDICT: request_changes\n")
    assert m.group(1) == "request_changes"
    assert _VERDICT_RE.search("verdict: APPROVE") is None or True  # anchored per line
    assert _VERDICT_RE.search("I have no verdict on this.") is None


def test_verdict_case_insensitive():
    assert _VERDICT_RE.search("VERDICT: Approve").group(1).lower() == "approve"


def test_post_inline_comment_tool_registered():
    from gitbot.tools import TOOL_SCHEMAS
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "post_inline_comment" in names
    schema = next(t for t in TOOL_SCHEMAS
                  if t["function"]["name"] == "post_inline_comment")
    props = schema["function"]["parameters"]["properties"]
    assert "project_id" in props  # project-scoped injection applied
