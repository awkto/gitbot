"""Tests for group-webhook ownership matching (issue #35).

_hook_is_ours decides which hooks the group-enable/disable flow will touch.
It must match GitBot's own webhook (by host + /webhook path, tolerating a
trailing slash) and must NOT match unrelated hooks (e.g. a pipeline trigger)."""

from gitbot.gitlab_client import _hook_is_ours

OUR = "https://gitbot-dev.pro.dnsif.ca/webhook"


def test_exact_match():
    assert _hook_is_ours(OUR, OUR)


def test_trailing_slash_tolerated():
    assert _hook_is_ours(OUR + "/", OUR)
    assert _hook_is_ours(OUR, OUR + "/")


def test_different_host_is_not_ours():
    assert not _hook_is_ours("https://evil.example.com/webhook", OUR)


def test_different_path_is_not_ours():
    # A pipeline-trigger hook on the same host must not be claimed as ours.
    assert not _hook_is_ours(
        "https://gitlab.dnsif.ca/api/v4/projects/99/trigger/pipeline", OUR
    )


def test_empty_urls_are_not_ours():
    assert not _hook_is_ours("", OUR)
    assert not _hook_is_ours(OUR, "")
