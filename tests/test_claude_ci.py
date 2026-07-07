"""Tests for Claude Code in CI (#41): the seeded pipeline + setup/dispatch."""

import pytest

from gitbot import claude_ci
from gitbot.config import settings


def test_ci_yaml_is_gated_and_uses_variables():
    y = claude_ci.CI_YAML
    assert "$PROMPT" in y                 # claude job only runs on a prompt
    assert 'if: \'$PROMPT\'' in y
    assert "$CLAUDE_IMAGE" in y           # image comes from the trigger
    assert all(v in y for v in ("TARGET_PROJECT", "TARGET_TYPE", "TARGET_IID"))
    assert "claude -p" in y               # runs Claude Code headless
    assert '--model "$CLAUDE_MODEL"' in y  # model chosen by GitBot, passed as a var
    assert "merge_request.create" in y    # opens the MR via native push options
    assert "glab" not in y                 # no dependency on glab in the image
    assert "$GITBOT_PUSH_TOKEN" in y      # push auth from a masked var


def test_ci_yaml_has_kaniko_build_job():
    y = claude_ci.CI_YAML
    assert "build-image:" in y                     # the build stage
    assert "kaniko-project/executor" in y          # rootless build, no privileged
    assert "$CI_REGISTRY_IMAGE:latest" in y        # publishes to project registry
    assert "if: '$BUILD_IMAGE'" in y               # gated on the build trigger var


def test_dockerfile_extends_base_with_clis():
    df = claude_ci._DOCKERFILE_TMPL.replace("{base}", "awkto/claude-code:latest")
    assert df.startswith("# Managed by GitBot") and "FROM awkto/claude-code:latest" in df
    # the full ops/dev toolbox the user asked for
    for tool in ("gh", "glab", "doctl", "bao", "cloudflared", "wrangler",
                 "terraform", "ansible", "openssh-client", "gnupg", "wget",
                 "go", "python3", "tmux", "android", "dnsutils", "nginx", "awkto-cli"):
        assert tool in df, tool
    # awkto-cli is gated on an optional build-arg so the image builds without it
    assert 'ARG GH_TOKEN' in df and 'if [ -n "$GH_TOKEN" ]' in df


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
                "default_branch": "main", "web_url": "http://gl/auto/runner",
                "registry_prefix": "reg.example/auto/runner"}
    monkeypatch.setattr(claude_ci.glc, "get_project", get_project)
    monkeypatch.setattr(claude_ci.glc, "create_project",
                        lambda name, **k: {"id": 7, "path_with_namespace": f"me/{name}",
                                           "default_branch": "main", "web_url": "http://gl/me",
                                           "registry_prefix": f"reg.example/me/{name}"})
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


def test_setup_seeds_files_sets_secrets_and_triggers_build(stub_glc):
    res = claude_ci.setup("auto/runner", "awkto/claude-code:latest", create_if_missing=False)
    keys = [k for k, _ in stub_glc["vars"]]
    assert "ANTHROPIC_API_KEY" in keys and "GITBOT_PUSH_TOKEN" in keys
    seeded = {a[0]["file_path"] for a in stub_glc["commits"]}
    assert seeded == {claude_ci.DOCKERFILE_PATH, claude_ci.CI_YAML_PATH}
    # claude job runs on the project-registry image, not the base
    assert res["registry_image"] == "reg.example/auto/runner:latest"
    assert res["runner_count"] == 1 and res["warnings"] == []
    # first image build kicked off
    assert stub_glc["pipelines"] and stub_glc["pipelines"][0][1] == {"BUILD_IMAGE": "true"}
    assert res["build"]["id"] == 42


def test_setup_falls_back_to_base_image_without_registry(stub_glc, monkeypatch):
    monkeypatch.setattr(claude_ci.glc, "get_project",
                        lambda spec: {"id": 7, "path_with_namespace": "auto/runner",
                                      "default_branch": "main", "web_url": "http://gl",
                                      "registry_prefix": ""})   # no instance registry
    res = claude_ci.setup("auto/runner", "awkto/claude-code:latest", create_if_missing=False)
    assert res["has_registry"] is False
    assert res["registry_image"] == "awkto/claude-code:latest"   # base image directly
    assert res["build"] is None                                   # no build without a registry
    assert any("registry" in w.lower() for w in res["warnings"])


def test_setup_warns_and_skips_build_when_no_runners(stub_glc, monkeypatch):
    monkeypatch.setattr(claude_ci.glc, "list_project_runners", lambda pid: [])
    res = claude_ci.setup("auto/runner", "img", create_if_missing=False)
    assert res["runner_count"] == 0
    assert any("runner" in w.lower() for w in res["warnings"])
    assert res["build"] is None and not stub_glc["pipelines"]  # no build without runners


def test_setup_requires_anthropic_key(stub_glc, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
    with pytest.raises(ValueError):
        claude_ci.setup("auto/runner", "img", create_if_missing=False)


def test_dispatch_passes_target_context_as_variables(stub_glc, monkeypatch):
    monkeypatch.setattr(settings, "claude_ci_enabled", True, raising=False)
    monkeypatch.setattr(settings, "claude_ci_project", "auto/runner", raising=False)
    monkeypatch.setattr(settings, "claude_ci_image", "awkto/claude-code:latest", raising=False)
    monkeypatch.setattr(settings, "claude_ci_ref", "main", raising=False)
    monkeypatch.setattr(settings, "claude_ci_model", "sonnet", raising=False)
    pipe = claude_ci.dispatch("group/app", "issue", 12, "fix the bug", "Read,Edit,Bash")
    assert pipe["id"] == 42
    ref, variables = stub_glc["pipelines"][0]
    assert ref == "main"
    assert variables["TARGET_PROJECT"] == "group/app"
    assert variables["TARGET_IID"] == "12"
    assert variables["PROMPT"] == "fix the bug"
    assert variables["ALLOWED_TOOLS"] == "Read,Edit,Bash"
    assert variables["CLAUDE_IMAGE"] == "awkto/claude-code:latest"
    assert variables["CLAUDE_MODEL"] == "sonnet"       # default tier


def test_dispatch_model_override(stub_glc, monkeypatch):
    monkeypatch.setattr(settings, "claude_ci_enabled", True, raising=False)
    monkeypatch.setattr(settings, "claude_ci_project", "auto/runner", raising=False)
    monkeypatch.setattr(settings, "claude_ci_model", "sonnet", raising=False)
    claude_ci.dispatch("group/app", "issue", 1, "x", model="opus")
    _, variables = stub_glc["pipelines"][0]
    assert variables["CLAUDE_MODEL"] == "opus"          # per-dispatch override wins


def test_dispatch_refuses_when_disabled(stub_glc, monkeypatch):
    monkeypatch.setattr(settings, "claude_ci_enabled", False, raising=False)
    with pytest.raises(ValueError):
        claude_ci.dispatch("group/app", "issue", 1, "x")
