"""Tests for token redaction (security): bot credentials must never reach a
GitLab comment, even via a subprocess error that embeds the authenticated
clone URL. Regression for a live incident where a clone failure posted the
oauth2:<token>@host URL into an issue."""

from gitbot import gitlab_client as glc
from gitbot.config import settings


def test_redacts_glpat_token():
    s = "fatal: clone of https://oauth2:glpat-ABC123def.45.xyz@gitlab/x.git failed"
    out = glc.redact(s)
    assert "glpat-ABC123def" not in out
    assert "***" in out


def test_redacts_oauth2_userinfo_even_without_glpat_prefix():
    s = "https://oauth2:somesecrettokenvalue@gitlab.example.com/a/b.git"
    out = glc.redact(s)
    assert "somesecrettokenvalue" not in out


def test_redacts_the_configured_token(monkeypatch):
    monkeypatch.setattr(settings, "gitlab_token", "my-special-token", raising=False)
    assert "my-special-token" not in glc.redact("oops my-special-token leaked")


def test_leaves_clean_text_untouched():
    s = "Implemented in MR !5 — everything looks good."
    assert glc.redact(s) == s


def test_handles_empty():
    assert glc.redact("") == ""
    assert glc.redact(None) is None
