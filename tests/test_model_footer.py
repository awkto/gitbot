"""Tests for the optional model footer on comments (debug toggle, #42)."""

from gitbot import brain
from gitbot.config import settings
from gitbot.context import Situation


def test_footer_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "show_model_footer", False, raising=False)
    sit = Situation(model_used="sonnet")
    assert brain._model_footer(sit, "Done") == "Done"


def test_footer_appends_model_when_on(monkeypatch):
    monkeypatch.setattr(settings, "show_model_footer", True, raising=False)
    sit = Situation(model_used="claude-opus-4-8")
    out = brain._model_footer(sit, "Implemented in MR !5")
    assert out.startswith("Implemented in MR !5")
    assert "claude-opus-4-8" in out and "model:" in out


def test_footer_skipped_without_model(monkeypatch):
    monkeypatch.setattr(settings, "show_model_footer", True, raising=False)
    sit = Situation(model_used="")
    assert brain._model_footer(sit, "Done") == "Done"


def test_footer_skipped_on_empty_text(monkeypatch):
    monkeypatch.setattr(settings, "show_model_footer", True, raising=False)
    sit = Situation(model_used="sonnet")
    assert brain._model_footer(sit, "") == ""
