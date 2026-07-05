"""Tests for failure-triggered escalation (#31): tier math, diagnosis parsing,
and the retry decision logic in brain._run_task_workflow."""

import pytest

from gitbot import brain, engine_sdk
from gitbot.config import settings
from gitbot.context import Situation


# --- tier math -------------------------------------------------------------

def test_next_tier_steps_up():
    assert engine_sdk.next_tier("haiku") == "sonnet"
    assert engine_sdk.next_tier("sonnet") == "opus"


def test_next_tier_tops_out():
    assert engine_sdk.next_tier("opus") is None


def test_next_tier_never_bumps_pinned_ids():
    assert engine_sdk.next_tier("claude-opus-4-8") is None


def test_min_tier_floors_auto_selection(monkeypatch):
    monkeypatch.setattr(settings, "model_implement", "auto", raising=False)
    assert engine_sdk._workflow_model("implement", 4) == "sonnet"
    assert engine_sdk._workflow_model("implement", 4, min_tier="opus") == "opus"
    # floor below the natural pick changes nothing
    assert engine_sdk._workflow_model("implement", 9, min_tier="sonnet") == "opus"


def test_min_tier_respects_pinned_override(monkeypatch):
    monkeypatch.setattr(settings, "model_implement", "claude-sonnet-4-6", raising=False)
    # A pinned id is an operator decision — escalation must not override it.
    assert engine_sdk._workflow_model("implement", 5, min_tier="opus") == "claude-sonnet-4-6"


def test_min_tier_raises_alias_override(monkeypatch):
    monkeypatch.setattr(settings, "model_implement", "haiku", raising=False)
    assert engine_sdk._workflow_model("implement", 5, min_tier="sonnet") == "sonnet"


# --- diagnosis parsing -------------------------------------------------------

async def test_diagnose_failure_parses_verdict(monkeypatch):
    async def fake(system, prompt, max_tokens=32):
        return "capability - the model repeatedly misread the diff"
    monkeypatch.setattr(engine_sdk, "_classify_complete", fake)
    verdict, reason = await engine_sdk.diagnose_failure(
        Situation(target_title="t"), "implement", "gate failed")
    assert verdict == "capability" and "misread" in reason


async def test_diagnose_failure_defaults_to_impossible(monkeypatch):
    async def fake(system, prompt, max_tokens=32):
        return "banana ??"
    monkeypatch.setattr(engine_sdk, "_classify_complete", fake)
    verdict, _ = await engine_sdk.diagnose_failure(
        Situation(), "orchestrate", "x")
    assert verdict == "impossible"


async def test_diagnose_failure_survives_classifier_error(monkeypatch):
    async def boom(system, prompt, max_tokens=32):
        raise RuntimeError("api down")
    monkeypatch.setattr(engine_sdk, "_classify_complete", boom)
    verdict, _ = await engine_sdk.diagnose_failure(Situation(), "implement", "x")
    assert verdict == "impossible"  # unknown → no autonomous extra spend


# --- retry decision loop -----------------------------------------------------

class _Runner:
    """Scripted run_implement stand-in recording each attempt's context."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, sit, wf_id, placeholder_id):
        self.calls.append({"min_tier": sit.min_tier,
                           "prior_failure": sit.prior_failure})
        return self.results.pop(0)


@pytest.fixture
def sit():
    return Situation(target_type="Issue", target_iid=1, task_complexity=4,
                     target_title="t")


async def _run(monkeypatch, sit, results, verdict="capability", enabled=True):
    runner = _Runner(results)
    monkeypatch.setattr(engine_sdk, "run_implement", runner)

    async def fake_diag(s, k, r):
        return verdict, "because"
    monkeypatch.setattr(engine_sdk, "diagnose_failure", fake_diag)
    monkeypatch.setattr(settings, "escalation_enabled", enabled, raising=False)
    monkeypatch.setattr(settings, "model_implement", "auto", raising=False)
    out = await brain._run_task_workflow("implement", sit, "wf", None)
    return out, runner


async def test_success_never_diagnoses(monkeypatch, sit):
    (result, ok), runner = await _run(monkeypatch, sit, [("done", True)])
    assert ok is True and len(runner.calls) == 1


async def test_parked_states_never_retry(monkeypatch, sit):
    (result, ok), runner = await _run(
        monkeypatch, sit, [("q?", "needs_input")])
    assert ok == "needs_input" and len(runner.calls) == 1


async def test_capability_retries_one_tier_up(monkeypatch, sit):
    (result, ok), runner = await _run(
        monkeypatch, sit, [("fail", False), ("done", True)])
    assert ok is True and len(runner.calls) == 2
    # complexity 4 → sonnet on attempt 1; retry floored to opus with the report
    assert runner.calls[1]["min_tier"] == "opus"
    assert "fail" in runner.calls[1]["prior_failure"]


async def test_transient_retries_same_tier(monkeypatch, sit):
    (result, ok), runner = await _run(
        monkeypatch, sit, [("fail", False), ("done", True)], verdict="transient")
    assert ok is True and len(runner.calls) == 2
    assert runner.calls[1]["min_tier"] == ""  # no tier bump


async def test_environment_failure_stands(monkeypatch, sit):
    (result, ok), runner = await _run(
        monkeypatch, sit, [("fail", False)], verdict="environment")
    assert ok is False and len(runner.calls) == 1


async def test_impossible_failure_stands(monkeypatch, sit):
    (result, ok), runner = await _run(
        monkeypatch, sit, [("fail", False)], verdict="impossible")
    assert ok is False and len(runner.calls) == 1


async def test_max_two_attempts(monkeypatch, sit):
    # Retry also fails → no third attempt.
    (result, ok), runner = await _run(
        monkeypatch, sit, [("fail", False), ("fail again", False)])
    assert ok is False and len(runner.calls) == 2


async def test_capability_at_top_tier_stands(monkeypatch, sit):
    sit.task_complexity = 9  # auto-picks opus already
    (result, ok), runner = await _run(monkeypatch, sit, [("fail", False)])
    assert ok is False and len(runner.calls) == 1


async def test_disabled_never_retries(monkeypatch, sit):
    (result, ok), runner = await _run(
        monkeypatch, sit, [("fail", False)], enabled=False)
    assert ok is False and len(runner.calls) == 1
