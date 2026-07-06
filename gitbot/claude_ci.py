"""Claude Code in CI pipelines (github/gitbot#41).

GitBot delegates heavy/agentic work to a dedicated GitLab "runner project"
that runs Claude Code non-interactively. GitBot owns the pipeline definition
in that project and triggers it with the target context in variables; the
pipeline clones/acts on the *target* repo and opens an MR back.

Design notes:
- The runner project is created or adopted (tiered, like the #36 onboarding).
- Only two secrets are set by GitBot as masked CI/CD variables:
  ANTHROPIC_API_KEY and GITBOT_PUSH_TOKEN. Any customer tool tokens
  (bao/gh/doctl/cloudflare/...) are the customer's own CI/CD variables — the
  job exposes the whole CI env, so their tools authenticate from it.
- The container image is customer-owned/extensible: point claude_ci_image at
  a Claude Code base extended with whatever CLIs the customer needs.
- Compute (runners) is the org's: GitBot verifies availability, it can't
  provide it.
"""

import logging

from gitbot import gitlab_client as glc
from gitbot.config import settings

log = logging.getLogger(__name__)

CI_YAML_PATH = ".gitlab-ci.yml"

# The pipeline GitBot seeds into the runner project. It runs Claude Code
# headless against the target repo (carried in trigger variables) and opens an
# MR. `image` comes from the trigger so the customer can swap it per run.
CI_YAML = r"""# Managed by GitBot — runs Claude Code non-interactively for a target
# issue/MR. Do not edit by hand; GitBot re-seeds this file on setup.
# Required masked CI/CD variables (set by GitBot): ANTHROPIC_API_KEY,
# GITBOT_PUSH_TOKEN. Add any tool tokens (bao/gh/doctl/...) as your own
# masked variables — they are exposed to the job env for your image's CLIs.
claude:
  image: "$CLAUDE_IMAGE"
  rules:
    - if: '$PROMPT'          # only run when GitBot triggers with a prompt
  variables:
    GIT_STRATEGY: none        # we clone the TARGET repo, not this one
  script:
    - set -eu
    - export ANTHROPIC_API_KEY GITLAB_TOKEN="$GITBOT_PUSH_TOKEN"
    - git config --global user.email "${GITBOT_USER:-gitbot}@${CI_SERVER_HOST}"
    - git config --global user.name "GitBot"
    - git clone --depth 30 "https://oauth2:${GITBOT_PUSH_TOKEN}@${CI_SERVER_HOST}/${TARGET_PROJECT}.git" work
    - cd work
    - DEFAULT_BRANCH="$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')"
    - BRANCH="gitbot/${TARGET_TYPE}-${TARGET_IID}"
    - git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH"
    - |
      if [ -n "${ALLOWED_TOOLS:-}" ]; then
        claude -p "$PROMPT" --permission-mode acceptEdits --allowedTools "$ALLOWED_TOOLS" --output-format stream-json | tee ../claude.log
      else
        claude -p "$PROMPT" --permission-mode acceptEdits --output-format stream-json | tee ../claude.log
      fi
    - |
      if [ -z "$(git status --porcelain)" ]; then
        echo "Claude produced no file changes."; exit 0
      fi
      git add -A
      git commit -m "GitBot: ${PROMPT}"
      git push -u origin "$BRANCH"
      export GITLAB_HOST="$CI_SERVER_HOST" GITLAB_TOKEN="$GITBOT_PUSH_TOKEN"
      glab mr create -R "$TARGET_PROJECT" --source-branch "$BRANCH" \
        --target-branch "$DEFAULT_BRANCH" --title "GitBot: ${PROMPT}" \
        --description "Automated by GitBot pipeline ${CI_PIPELINE_URL}" --yes || \
        echo "Branch pushed; open the MR manually if glab failed."
"""


def _resolve_project(spec: str, create_if_missing: bool) -> dict:
    """Resolve the runner project by id/path, optionally creating it by name."""
    existing = glc.get_project(spec)
    if existing:
        return existing
    if not create_if_missing:
        raise ValueError(f"project '{spec}' not found")
    # A namespaced path (group/name) creates the project under that GROUP;
    # a bare name goes to the token owner's personal space (which service
    # accounts can't do — give a group path like 'automation/gitbot-runner').
    if "/" in spec:
        namespace, name = spec.rsplit("/", 1)
        return glc.create_project(name, namespace_path=namespace)
    return glc.create_project(spec)


def setup(project_spec: str, image: str, create_if_missing: bool) -> dict:
    """Provision/adopt the runner project: ensure it exists, set the two
    required secrets, seed the pipeline, and verify runners.

    Returns a status dict (project, runners, warnings)."""
    if not settings.anthropic_api_key:
        raise ValueError("No Anthropic API key configured — set it before enabling Claude CI.")

    project = _resolve_project(project_spec, create_if_missing)
    pid = project["id"]

    # Secrets → masked CI/CD variables. The push token is GitBot's own token
    # (its service account); scope it down in production.
    glc.set_project_variable(pid, "ANTHROPIC_API_KEY", settings.anthropic_api_key)
    glc.set_project_variable(pid, "GITBOT_PUSH_TOKEN", settings.gitlab_token)

    # Seed the pipeline file on the default branch (create or update).
    branch = project["default_branch"]
    existing_file = None
    try:
        existing_file = glc.get_file_content(pid, CI_YAML_PATH, ref=branch)
    except Exception:
        pass
    if existing_file != CI_YAML:
        action = "update" if existing_file is not None else "create"
        glc.commit_files(pid, branch, "GitBot: seed Claude Code CI pipeline",
                         [{"action": action, "file_path": CI_YAML_PATH, "content": CI_YAML}])

    runners = glc.list_project_runners(pid)
    usable = [r for r in runners if r.get("online") or r.get("active")]
    warnings = []
    if not runners:
        warnings.append("No CI runners available to this project. Enable shared "
                        "runners (group/instance) or register a runner on it, or "
                        "pipelines will stay pending.")
    elif not usable:
        warnings.append(f"{len(runners)} runner(s) attached but none appear online.")

    return {"project": project, "image": image,
            "runners": runners, "runner_count": len(runners),
            "warnings": warnings}


def dispatch(target_project: str, target_type: str, target_iid: int,
             prompt: str, allowed_tools: str = "") -> dict:
    """Trigger a Claude Code pipeline for a target issue/MR. Returns the
    pipeline id/status/url so GitBot can link + track it."""
    if not settings.claude_ci_enabled or not settings.claude_ci_project:
        raise ValueError("Claude CI is not enabled/configured.")
    project = glc.get_project(settings.claude_ci_project)
    if not project:
        raise ValueError(f"runner project '{settings.claude_ci_project}' not found")
    variables = {
        "PROMPT": prompt,
        "TARGET_PROJECT": target_project,
        "TARGET_TYPE": target_type,
        "TARGET_IID": str(target_iid),
        "CLAUDE_IMAGE": settings.claude_ci_image,
        "GITBOT_USER": settings.bot_username,
    }
    if allowed_tools:
        variables["ALLOWED_TOOLS"] = allowed_tools
    pipe = glc.trigger_pipeline(project["id"], settings.claude_ci_ref, variables)
    log.info("Claude CI dispatched: pipeline %s (%s) for %s %s#%s",
             pipe["id"], pipe["status"], target_project, target_type, target_iid)
    return pipe
