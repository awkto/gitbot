"""Claude Code in CI pipelines (github/gitbot#41).

GitBot delegates heavy/agentic work to a dedicated GitLab "runner project"
that runs Claude Code non-interactively. GitBot owns the pipeline definition
in that project and triggers it with the target context in variables; the
pipeline clones/acts on the *target* repo and opens an MR back.

The runner project is self-sufficient: it carries a Dockerfile that extends a
Claude Code base image with the CLIs the customer needs (gh, glab, doctl, bao,
cloudflared/wrangler, ...), a build-image job that publishes that image to the
project's OWN container registry (via Kaniko — no privileged runner), and the
claude job that runs on it. Pulling from the project registry uses the job
token, so there's no Docker Hub pull limit to hit.

Design notes:
- The runner project is created or adopted (tiered, like the #36 onboarding).
- Only two secrets are set by GitBot as masked CI/CD variables:
  ANTHROPIC_API_KEY and GITBOT_PUSH_TOKEN. Any customer tool tokens
  (bao/gh/doctl/cloudflare/...) are the customer's own CI/CD variables — the
  job exposes the whole CI env, so their tools authenticate from it.
- The model is chosen by GitBot and passed per-run as CLAUDE_MODEL.
- Compute (runners) is the org's: GitBot verifies availability, it can't
  provide it. Image builds need a runner that can run Kaniko (any normal
  runner; no docker-in-docker / privileged required).
"""

import logging

from gitbot import gitlab_client as glc
from gitbot.config import settings

log = logging.getLogger(__name__)

CI_YAML_PATH = ".gitlab-ci.yml"
DOCKERFILE_PATH = "Dockerfile"

# Starting-scaffold Dockerfile seeded into the runner project. Extends the
# Claude Code base with common infra CLIs; the customer edits this freely.
# {base} is the base image GitBot templates in at seed time.
_DOCKERFILE_TMPL = r"""# Managed by GitBot — STARTING SCAFFOLD, edit freely, then re-run the
# build-image pipeline. Extends the Claude Code base with the CLIs your CI
# tasks use; the build-image job publishes it to THIS project's container
# registry, which the claude job then runs on.
FROM {base}

USER root
ARG ARCH=amd64
ARG GLAB_VERSION=1.68.0
ARG DOCTL_VERSION=1.124.0
ARG BAO_VERSION=2.5.5
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg unzip; \
    # GitHub CLI (gh)
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; apt-get install -y gh; \
    # glab (GitLab CLI)
    curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${ARCH}.tar.gz" | tar -xz -C /usr/local bin/glab; \
    # doctl (DigitalOcean)
    curl -fsSL "https://github.com/digitalocean/doctl/releases/download/v${DOCTL_VERSION}/doctl-${DOCTL_VERSION}-linux-${ARCH}.tar.gz" | tar -xz -C /usr/local/bin doctl; \
    # bao (OpenBao) — note the asset naming: Linux_x86_64.tar.gz
    curl -fsSL "https://github.com/openbao/openbao/releases/download/v${BAO_VERSION}/bao_${BAO_VERSION}_Linux_x86_64.tar.gz" | tar -xz -C /usr/local/bin bao; \
    # cloudflared + wrangler (Cloudflare)
    curl -fsSL -o /usr/local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"; \
    chmod +x /usr/local/bin/cloudflared; \
    npm install -g wrangler; \
    apt-get clean; rm -rf /var/lib/apt/lists/* /tmp/*
# Match your base image's runtime user (the Claude Code base runs as `node`).
USER node
"""

# The pipeline GitBot seeds into the runner project: a Kaniko build-image job
# (publishes the extended image to the project registry) + the claude job.
CI_YAML = r"""# Managed by GitBot — do not edit by hand; GitBot re-seeds this on setup.
# Required masked CI/CD variables (set by GitBot): ANTHROPIC_API_KEY,
# GITBOT_PUSH_TOKEN. Model is passed per-run as CLAUDE_MODEL. Add any tool
# tokens (bao/gh/doctl/...) as your own masked variables — the claude job
# exposes the whole CI env to your image's CLIs. The MR is opened via native
# GitLab push options, so the image needs only git + claude.
stages:
  - build
  - run

# Build the extended image (Dockerfile = base + your CLIs) and publish it to
# THIS project's registry. Triggered with BUILD_IMAGE=true (GitBot's "Build
# image" action, or on a Dockerfile change). Kaniko needs no privileged /
# docker-in-docker runner.
build-image:
  stage: build
  image:
    name: gcr.io/kaniko-project/executor:debug
    entrypoint: [""]
  rules:
    - if: '$BUILD_IMAGE'
  script:
    - mkdir -p /kaniko/.docker
    - AUTH=$(printf "%s:%s" "$CI_REGISTRY_USER" "$CI_REGISTRY_PASSWORD" | base64 | tr -d '\n')
    - printf '{"auths":{"%s":{"auth":"%s"}}}' "$CI_REGISTRY" "$AUTH" > /kaniko/.docker/config.json
    - /kaniko/executor --context "$CI_PROJECT_DIR" --dockerfile "$CI_PROJECT_DIR/Dockerfile" --destination "$CI_REGISTRY_IMAGE:latest"

# Run Claude Code non-interactively against the target repo (carried in trigger
# variables) and open an MR back.
claude:
  stage: run
  image: "$CLAUDE_IMAGE"
  rules:
    - if: '$PROMPT'
  variables:
    GIT_STRATEGY: none        # we clone the TARGET repo, not this one
  script:
    - set -eu
    - export ANTHROPIC_API_KEY GITLAB_TOKEN="$GITBOT_PUSH_TOKEN"
    - CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}"
    - git config --global user.email "${GITBOT_USER:-gitbot}@${CI_SERVER_HOST}"
    - git config --global user.name "GitBot"
    - git clone --depth 30 "https://oauth2:${GITBOT_PUSH_TOKEN}@${CI_SERVER_HOST}/${TARGET_PROJECT}.git" work
    - cd work
    - DEFAULT_BRANCH="$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')"
    - BRANCH="gitbot/${TARGET_TYPE}-${TARGET_IID}"
    - git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH"
    - |
      if [ -n "${ALLOWED_TOOLS:-}" ]; then
        claude -p "$PROMPT" --model "$CLAUDE_MODEL" --permission-mode acceptEdits --allowedTools "$ALLOWED_TOOLS" | tee ../claude.log
      else
        claude -p "$PROMPT" --model "$CLAUDE_MODEL" --permission-mode acceptEdits | tee ../claude.log
      fi
    - |
      if [ -z "$(git status --porcelain)" ]; then
        echo "Claude produced no file changes."; exit 0
      fi
      git add -A
      git commit -m "GitBot: ${PROMPT}"
      git push -u origin "$BRANCH" \
        -o merge_request.create \
        -o merge_request.target="$DEFAULT_BRANCH" \
        -o merge_request.title="GitBot: ${PROMPT}" \
        -o merge_request.description="Automated by GitBot pipeline ${CI_PIPELINE_URL}" \
        || git push -u origin "$BRANCH"
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


def _seed_file(pid: int, branch: str, path: str, content: str, label: str) -> None:
    existing = None
    try:
        existing = glc.get_file_content(pid, path, ref=branch)
    except Exception:
        pass
    if existing != content:
        action = "update" if existing is not None else "create"
        glc.commit_files(pid, branch, f"GitBot: seed {label}",
                         [{"action": action, "file_path": path, "content": content}])


def setup(project_spec: str, base_image: str, create_if_missing: bool) -> dict:
    """Provision/adopt the runner project: ensure it exists, set the two
    required secrets, seed the Dockerfile + pipeline, verify runners, and
    trigger a first image build. `base_image` is the FROM base the Dockerfile
    extends; the claude job runs on the resulting project-registry image.

    Returns a status dict (project, registry_image, runners, build, warnings)."""
    if not settings.anthropic_api_key:
        raise ValueError("No Anthropic API key configured — set it before enabling Claude CI.")

    project = _resolve_project(project_spec, create_if_missing)
    pid = project["id"]
    branch = project["default_branch"]

    glc.set_project_variable(pid, "ANTHROPIC_API_KEY", settings.anthropic_api_key)
    glc.set_project_variable(pid, "GITBOT_PUSH_TOKEN", settings.gitlab_token)

    _seed_file(pid, branch, DOCKERFILE_PATH, _DOCKERFILE_TMPL.replace("{base}", base_image), "Dockerfile")
    _seed_file(pid, branch, CI_YAML_PATH, CI_YAML, "Claude Code CI pipeline")

    # If the instance has a container registry, the claude job runs on the
    # EXTENDED image we build there (base + the Dockerfile's CLIs). Without a
    # registry (CI_REGISTRY_IMAGE empty), there's nowhere to push — fall back to
    # running the base image directly and skip the build.
    registry_prefix = project.get("registry_prefix") or ""
    has_registry = bool(registry_prefix)
    registry_image = f"{registry_prefix}:latest" if has_registry else base_image

    runners = glc.list_project_runners(pid)
    usable = [r for r in runners if r.get("online") or r.get("active")]
    warnings = []
    if not runners:
        warnings.append("No CI runners available to this project. Enable shared "
                        "runners (group/instance) or register a runner, or "
                        "pipelines will stay pending.")
    elif not usable:
        warnings.append(f"{len(runners)} runner(s) attached but none appear online.")
    if not has_registry:
        warnings.append("This GitLab instance has no container registry configured, "
                        "so the extended-image build is skipped — the claude job runs "
                        "the base image directly. To bake in custom CLIs, enable the "
                        "GitLab container registry, or point the base image at one that "
                        "already includes your tools.")

    # Kick off the first image build (only meaningful with a registry).
    build = None
    if has_registry and usable:
        try:
            build = glc.trigger_pipeline(pid, branch, {"BUILD_IMAGE": "true"})
        except Exception as e:  # non-fatal — the admin can build from the panel
            warnings.append(f"Could not start the image build automatically: {e}")

    return {"project": project, "base_image": base_image,
            "registry_image": registry_image, "has_registry": has_registry,
            "runners": runners, "runner_count": len(runners),
            "build": build, "warnings": warnings}


def build_image() -> dict:
    """Trigger the build-image pipeline (rebuild the extended image)."""
    if not settings.claude_ci_project:
        raise ValueError("Claude CI is not configured.")
    project = glc.get_project(settings.claude_ci_project)
    if not project:
        raise ValueError(f"runner project '{settings.claude_ci_project}' not found")
    return glc.trigger_pipeline(project["id"], settings.claude_ci_ref, {"BUILD_IMAGE": "true"})


def dispatch(target_project: str, target_type: str, target_iid: int,
             prompt: str, allowed_tools: str = "", model: str = "") -> dict:
    """Trigger a Claude Code pipeline for a target issue/MR. Returns the
    pipeline id/status/url so GitBot can link + track it. `model` overrides
    the configured claude_ci_model (passed to the CLI as CLAUDE_MODEL)."""
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
        "CLAUDE_MODEL": model or settings.claude_ci_model,
        "GITBOT_USER": settings.bot_username,
    }
    if allowed_tools:
        variables["ALLOWED_TOOLS"] = allowed_tools
    pipe = glc.trigger_pipeline(project["id"], settings.claude_ci_ref, variables)
    log.info("Claude CI dispatched: pipeline %s (%s) for %s %s#%s",
             pipe["id"], pipe["status"], target_project, target_type, target_iid)
    return pipe
