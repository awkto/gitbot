# GitBot

A Claude-powered agent that works as a GitLab team member. Assign issues, request reviews, @mention for help — GitBot responds like a real developer.

Built on the [Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk): the SDK runs the agent loop, GitBot supplies the GitLab tools, workflows, and durability.

## Quick Start

```bash
docker run -d \
  --name gitbot \
  -p 8042:8042 \
  -v gitbot-data:/app/data \
  -e GITBOT_GITLAB_URL=https://gitlab.example.com \
  -e GITBOT_GITLAB_TOKEN=glpat-xxxxxxxxxxxx \
  -e GITBOT_BOT_USERNAME=gitbot \
  -e GITBOT_ANTHROPIC_API_KEY=sk-ant-... \
  awkto/gitbot:latest
```

Then add a webhook in your GitLab project/group pointing to `http://your-host:8042/webhook` with **Issues**, **Merge Requests**, and **Notes** events enabled.

## What It Does

- **Assigned an issue** — triages it (code change vs. orchestration), researches, asks a clarifying question only when it matters, then implements: branch + commits + merge request
- **Assigned as MR reviewer** — reviews the diff in context of the full checkout, posts severity-marked inline findings (🔴🟠🟡🔵) and a verdict
- **@mentioned in a comment** — answers in a threaded reply; comments that steer or request work resume/start the real workflow
- **Complex requests** — multi-project orchestration: creates projects, CI/CD pipelines, epics/milestones, waits on pipelines, verifies each step
- **Survives restarts** — labels + a state DB let interrupted work resume exactly where it left off; every session keeps to a single comment thread

## Configuration

All config via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITBOT_GITLAB_URL` | Yes | `https://gitlab.com` | GitLab instance URL |
| `GITBOT_GITLAB_TOKEN` | Yes | | Personal access token for the bot user |
| `GITBOT_BOT_USERNAME` | Yes | `gitbot` | GitLab username of the bot account |
| `GITBOT_ANTHROPIC_API_KEY` | Yes | | Anthropic API key |
| `GITBOT_WEBHOOK_SECRET` | No | | Webhook secret token for verification |
| `GITBOT_GITLAB_SSL_VERIFY` | No | `true` | Set `false` for self-signed certs |
| `GITBOT_ADMIN_ENABLED` | No | `true` | Enable admin panel at `/admin` |
| `GITBOT_LOCAL_EXEC` | No | `light` | Shell policy in the agent workspace: `none`, `light`, `full` |
| `GITBOT_QUESTION_THRESHOLD` | No | `7` | How important (1-10) a question must be before asking the user |
| `GITBOT_RECONCILE_MINUTES` | No | `10` | Sweep interval for resuming orphaned/parked work (0 = off) |
| `GITBOT_MODEL_MENTION` / `_IMPLEMENT` / `_ORCHESTRATE` / `_REVIEW` | No | `auto` | Per-workflow model: `auto`, `haiku`, `sonnet`, `opus`, or a pinned id |

## Model Selection

`auto` (the default) lets GitBot decide: a cheap triage call scores each task's complexity 1-10 and the harness picks the tier — trivial mentions run on Haiku, typical work on Sonnet, reviews and complex orchestration on Opus. The tier aliases resolve to the current model of each tier, so there is nothing to update when new Claude models ship. Override per workflow in the admin panel (or pin an exact model id); reset back to `auto` anytime.

## Persistence

Mount `/app/data` to persist the SQLite state database across restarts. This stores pending questions and work-in-progress tracking for crash recovery.

```bash
# Named volume (recommended)
-v gitbot-data:/app/data

# Or bind mount to a host directory
-v /path/on/host/gitbot-data:/app/data
```

## Admin Panel

Access the admin panel at `http://your-host:8042/admin` to:
- View connection status for GitLab and the Anthropic API
- Tune the ask-threshold and per-workflow models live
- Monitor active workflows with live progress
- View activity feed and workflow history

Disable with `GITBOT_ADMIN_ENABLED=false`.

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

## GitLab Setup

1. Create a GitLab user or service account for the bot
2. Generate a personal access token with `api` scope
3. Add the bot as a member (Developer+) to your projects or group
4. Add a webhook (project or group level) pointing to GitBot

## Docker Compose

```yaml
services:
  gitbot:
    image: awkto/gitbot:latest
    ports:
      - "8042:8042"
    environment:
      - GITBOT_GITLAB_URL=https://gitlab.example.com
      - GITBOT_GITLAB_TOKEN=glpat-xxxxxxxxxxxx
      - GITBOT_BOT_USERNAME=gitbot
      - GITBOT_ANTHROPIC_API_KEY=sk-ant-...
    volumes:
      - gitbot-data:/app/data
    restart: unless-stopped

volumes:
  gitbot-data:
```
