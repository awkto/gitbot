# GitBot

A Claude-powered agent that works as a GitLab team member. Assign issues, request reviews, @mention for help — GitBot responds like a real developer.

Built on the [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk): the SDK runs the agent loop, GitBot supplies the GitLab tools, workflows, and durability.

## Quick Start

```bash
docker run -d \
  --name gitbot \
  -p 8042:8042 \
  -v gitbot-data:/app/data \
  -e GITBOT_ADMIN_PASSWORD=change-me \
  -e GITBOT_WEBHOOK_SECRET=some-random-secret \
  awkto/gitbot:latest
```

Then open `http://your-host:8042/admin` (HTTP Basic auth — any username, your admin password) and either:

- **Onboard with an admin token** (easiest): paste a GitLab admin token in the **Onboarding** panel — GitBot creates its own service account, mints the account's API token, joins the groups you pick, and installs the group webhooks. One screen, done.
- **Configure manually**: enter your GitLab URL, a bot user's token, and an Anthropic API key in **Configure**, then enable groups in the **Groups** panel (GitBot creates and manages the webhooks) — or add a webhook yourself pointing to `http://your-host:8042/webhook` with **Issues**, **Merge Requests**, and **Notes** events.

Fully env-driven deployments can skip the UI entirely — see [Configuration](#configuration).

## What It Does

- **Assigned an issue** — triages it (code change vs. orchestration), researches, asks a clarifying question only when it matters, then implements: branch + commits + merge request
- **Assigned as MR reviewer** — reviews the diff in context of the full checkout, posts severity-marked inline findings (🔴🟠🟡🔵) and a verdict
- **@mentioned in a comment** — answers in a threaded reply; comments that steer or request work resume/start the real workflow
- **Complex requests** — multi-project orchestration: creates projects, CI/CD pipelines, epics/milestones, waits on pipelines, verifies each step
- **Survives restarts** — labels + a state DB let interrupted work resume exactly where it left off; every session keeps to a single comment thread

## Configuration

Layered, per key — highest wins:

1. **Environment variables** (`GITBOT_*`) — for automated/declarative deployments. Env-owned keys are immutable at runtime and show as locked `(env)` in the admin panel.
2. **The config store** (`data/config.json`, inside the data volume) — whatever you set in the admin panel. Persists across container recreation and applies live, no restart.
3. Built-in defaults.

So: pass everything as env vars and the app boots fully hooked up; pass nothing but an admin password and configure everything in the UI; or mix — env for connection secrets, UI for tunables.

| Variable | Default | Description |
|---|---|---|
| `GITBOT_GITLAB_URL` | `https://gitlab.com` | GitLab instance URL |
| `GITBOT_GITLAB_TOKEN` | | API token for the bot account |
| `GITBOT_BOT_USERNAME` | `gitbot` | GitLab username of the bot account |
| `GITBOT_ANTHROPIC_API_KEY` | | Anthropic API key |
| `GITBOT_WEBHOOK_SECRET` | | Shared secret for `/webhook` (set one!) |
| `GITBOT_ADMIN_PASSWORD` | | Protects `/admin` with HTTP Basic auth (set one!) |
| `GITBOT_ADMIN_ENABLED` | `true` | Enable admin panel at `/admin` |
| `GITBOT_GITLAB_SSL_VERIFY` | `true` | Set `false` for self-signed certs |
| `GITBOT_LOCAL_EXEC` | `light` | Shell policy in the agent workspace: `none`, `light`, `full` |
| `GITBOT_QUESTION_THRESHOLD` | `7` | How important (1-10) a question must be before asking the user |
| `GITBOT_RECONCILE_MINUTES` | `10` | Sweep interval for resuming orphaned/parked work (0 = off) |
| `GITBOT_MODEL_MENTION` / `_IMPLEMENT` / `_ORCHESTRATE` / `_REVIEW` | `auto` | Per-workflow model: `auto`, `haiku`, `sonnet`, `opus`, or a pinned id |

GitLab connection + Anthropic key are required for operation, whichever layer supplies them.

## Onboarding & Group Management

The admin panel automates GitLab setup end to end:

- **Onboarding** (admin token, used once, never stored): creates a dedicated **service account** for GitBot — or adopts an existing one; it refuses to hijack a human user — mints the account's own scoped API token (shown once), adds it to the groups you select at your chosen role, and installs the group webhooks. Optionally switches the running GitBot to the new identity on the spot.
- **Groups**: discover the groups your token owns and toggle GitBot per group. GitBot creates and owns the webhooks (correct events + secret), detects overlapping project hooks that would double-fire and offers to clean them, and blocks enabling a subgroup already covered by a parent.

Manual alternative: create a bot user + token with `api` scope, add it to your projects/group (Developer+), and point a webhook at `/webhook` yourself.

## Model Selection

`auto` (the default) lets GitBot decide: a cheap triage call scores each task's complexity 1-10 and the harness picks the tier — trivial mentions run on Haiku, typical work on Sonnet, reviews and complex orchestration on Opus. The tier aliases resolve to the current model of each tier, so there is nothing to update when new Claude models ship. Override per workflow in the admin panel (or pin an exact model id); reset back to `auto` anytime.

## Persistence

Mount `/app/data` — it holds the SQLite state DB (crash recovery, pending questions) **and** the config store.

```bash
# Named volume (recommended)
-v gitbot-data:/app/data

# Or bind mount to a host directory
-v /path/on/host/gitbot-data:/app/data
```

## Admin Panel

`http://your-host:8042/admin` (Basic auth when `GITBOT_ADMIN_PASSWORD` is set):

- Onboard the service account; enable/disable GitBot per group
- Edit configuration live (env-owned keys shown locked)
- Tune the ask-threshold and per-workflow models — persisted, applied live
- Monitor active workflows with live progress, activity feed, history

## Architecture

```
Webhook → triage (Haiku) → one Agent SDK session per task
            │                  ├─ mention      answer in-thread
            │                  ├─ implement    clone → branch → MR (verified)
            │                  ├─ orchestrate  multi-project / CI / admin
            │                  └─ review       inline findings + verdict
            └─ labels + state DB → reconcile sweep resumes interrupted work
```

- **One session = one comment thread** — the first comment is the anchor (edited in place for status and the final report); everything else is a threaded reply.
- **Finish gates** — implement only reports success after the API confirms an open MR with commits; finish states `BLOCKED` / `WAITING` / `NEEDS_INPUT` park work cleanly.
- **Not a devbox** — by default the container denies installs/docker/network tools in agent shells; builds and tests belong to your GitLab CI/CD pipelines.

## Deployment & Updates

Releases are published to Docker Hub as `awkto/gitbot:<version>` and `:latest` (built by CI on version tags, gated on the test suite). To stay current automatically, run [Watchtower](https://containrrr.dev/watchtower/) and label the container:

```bash
docker run -d --name gitbot \
  --label com.centurylinklabs.watchtower.enable=true \
  ... awkto/gitbot:latest
```

## Docker Compose

```yaml
services:
  gitbot:
    image: awkto/gitbot:latest
    ports:
      - "8042:8042"
    environment:
      # Option A: fully env-driven (all keys locked in the UI)
      - GITBOT_GITLAB_URL=https://gitlab.example.com
      - GITBOT_GITLAB_TOKEN=glpat-xxxxxxxxxxxx
      - GITBOT_BOT_USERNAME=gitbot
      - GITBOT_ANTHROPIC_API_KEY=sk-ant-...
      # Option B: set only these two and do the rest in the admin panel
      - GITBOT_ADMIN_PASSWORD=change-me
      - GITBOT_WEBHOOK_SECRET=some-random-secret
    volumes:
      - gitbot-data:/app/data
    restart: unless-stopped
    labels:
      - com.centurylinklabs.watchtower.enable=true

volumes:
  gitbot-data:
```

## Development

```bash
pip install -e ".[dev]"
pytest            # unit tests (also run by CI on every push/PR and before publish)
```
