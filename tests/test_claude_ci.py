"""Tests for Claude Code in CI (#41): the seeded pipeline + setup/dispatch."""

import pytest

from gitbot import claude_ci
from gitbot.config import settings


def test_ci_yaml_is_gated_and_uses_variables():
    y = claude_ci.CI_YAML
    assert "$PROMPT" in y                 # only runs when triggered with a prompt
    assert 'if: \'$PROMPT\'' in y
    assert "$CLAUDE_IMAGE" in y           # image comes from the trigger
    assert all(v in y for v in ("TARGET_PROJECT", "TARGET_TYPE", "TARGET_IID"))
    assert "claude -p" in y               # runs Claude Code headless
    assert "glab mr create" in y          # opens the MR back
    assert "$GITBOT_PUSH_TOKEN" in y      # push auth from a masked var


def test_ci_yaml_never_hardcodes_secrets():
    # The template must reference secrets as variables, not literals.
    y = claude_ci.CI_YAML.lower()
    assert "glpat-" not in y and "sk-ant-" not in y


@pytest.fixture
def stub_glc(monkeypatch):
    calls = {"vars": [], "commits": [], "pipelines": []}

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-x", raising=False)
    monkeypatch.setattr(settings, "gitlab_token", "glpat-x", raising=False)
    monkeypatch.setattr(settings, "bot_username", "gitbot", raising=False)

    def get_project(spec):
        return {"id": 7, "path_with_namespace": "auto/runner",
                "default_branch": "main", "web_url": "http://gl/auto/runner"}
    monkeypatch.setattr(claude_ci.glc, "get_project", get_project)
    monkeypatch.setattr(claude_ci.glc, "create_project",
                        lambda name, **k: {"id": 7, "path_with_namespace": f"me/{name}",
                                           "default_branch": "main", "web_url": "http://gl/me"})
    monkeypatch.setattr(claude_ci.glc, "set_project_variable",
                        lambda pid, k, v, **kw: calls["vars"].append((k, v)))
    monkeypatch.setattr(claude_ci.glc, "get_file_content",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("absent")))
    monkeypatch.setattr(claude_ci.glc, "commit_files",
                        lambda pid, br, msg, actions: calls["commits"].append(actions))
    monkeypatch.setattr(claude_ci.glc, "list_project_runners",
                        lambda pid: [{"id": 1, "online": True, "active": True}])
    monkeypatch.setattr(claude_ci.glc, "trigger_pipeline",
                        lambda pid, ref, variables: calls["pipelines"].append((ref, variables))
                        or {"id": 42, "status": "created", "web_url": "http://gl/pipe/42"})
    return calls


def test_setup_sets_secrets_seeds_pipeline_verifies_runners(stub_glc):
    res = claude_ci.setup("auto/runner", "awkto/claude-code:latest", create_if_missing=False)
    keys = [k for k, _ in stub_glc["vars"]]
    assert "ANTHROPIC_API_KEY" in keys and "GITBOT_PUSH_TOKEN" in keys
    assert stub_glc["commits"], "pipeline file should be seeded"
    assert stub_glc["commits"][0][0]["file_path"] == claude_ci.CI_YAML_PATH
    assert res["runner_count"] == 1 and res["warnings"] == []


def test_setup_warns_when_no_runners(stub_glc, monkeypatch):
    monkeypatch.setattr(claude_ci.glc, "list_project_runners", lambda pid: [])
    res = claude_ci.setup("auto/runner", "img", create_if_missing=False)
    assert res["runner_count"] == 0
    assert any("runner" in w.lower() for w in res["warnings"])


def test_setup_requires_anthropic_key(stub_glc, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    with pytest.raises(ValueError):
        claude_ci.setup("auto/runner", "img", create_if_missing=False)


def test_dispatch_passes_target_context_as_variables(stub_glc, monkeypatch):
    monkeypatch.setattr(settings, "claude_ci_enabled", True, raising=False)
    monkeypatch.setattr(settings, "claude_ci_project", "auto/runner", raising=False)
    monkeypatch.setattr(settings, "claude_ci_image", "awkto/claude-code:latest", raising=False)
    monkeypatch.setattr(settings, "claude_ci_ref", "main", raising=False)
    pipe = claude_ci.dispatch("group/app", "issue", 12, "fix the bug", "Read,Edit,Bash")
    assert pipe["id"] == 42
    ref, variables = stub_glc["pipelines"][0]
    assert ref == "main"
    assert variables["TARGET_PROJECT"] == "group/app"
    assert variables["TARGET_IID"] == "12"
    assert variables["PROMPT"] == "fix the bug"
    assert variables["ALLOWED_TOOLS"] == "Read,Edit,Bash"
    assert variables["CLAUDE_IMAGE"] == "awkto/claude-code:latest"


def test_dispatch_refuses_when_disabled(stub_glc, monkeypatch):
    monkeypatch.setattr(settings, "claude_ci_enabled", False, raising=False)
    with pytest.raises(ValueError):
        claude_ci.dispatch("group/app", "issue", 1, "x")
