# GitBot

AI-powered GitLab team member. Assign issues, request reviews, @mention for help — GitBot responds like a real developer.

## Quick Start

```bash
docker run -d \
  --name gitbot \
  -p 8042:8042 \
  -v gitbot-data:/app/data \
  -e GITBOT_GITLAB_URL=https://gitlab.example.com \
  -e GITBOT_GITLAB_TOKEN=glpat-xxxxxxxxxxxx \
  -e GITBOT_BOT_USERNAME=gitbot \
  -e GITBOT_LLM_FAMILY=gemini \
  -e GITBOT_LLM_API_KEY=AIza... \
  awkto/gitbot:latest
```

Then add a webhook in your GitLab project/group pointing to `http://your-host:8042/webhook` with **Issues**, **Merge Requests**, and **Notes** events enabled.

## What It Does

- **Assigned an issue** — triages, asks clarifying questions if needed, creates a branch + MR with code
- **Assigned as MR reviewer** — performs a code review with severity markers
- **@mentioned in a comment** — responds helpfully, answers questions
- **Comment on bot's MR** — pushes new commits to the branch (no @mention needed)
- **Complex requests** — breaks work into sub-issues, creates epics/milestones, manages projects

## Configuration

All config via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITBOT_GITLAB_URL` | Yes | `https://gitlab.com` | GitLab instance URL |
| `GITBOT_GITLAB_TOKEN` | Yes | | Personal access token for the bot user |
| `GITBOT_BOT_USERNAME` | Yes | `gitbot` | GitLab username of the bot account |
| `GITBOT_LLM_FAMILY` | Yes | `claude-code` | LLM provider: `anthropic`, `gemini`, `openai`, `ollama`, `claude-code` |
| `GITBOT_LLM_API_KEY` | Yes* | | API key for the LLM provider |
| `GITBOT_WEBHOOK_SECRET` | No | | Webhook secret token for verification |
| `GITBOT_GITLAB_SSL_VERIFY` | No | `true` | Set `false` for self-signed certs |
| `GITBOT_ADMIN_ENABLED` | No | `true` | Enable admin panel at `/admin` |
| `GITBOT_LLM_MODEL_CHEAP` | No | | Override cheap tier model |
| `GITBOT_LLM_MODEL_MID` | No | | Override mid tier model |
| `GITBOT_LLM_MODEL_STRONG` | No | | Override strong tier model |

*Not required for `claude-code` or `ollama` families.

## Persistence

Mount `/app/data` to persist the SQLite state database across restarts. This stores pending questions and work-in-progress tracking for crash recovery.

```bash
# Named volume (recommended)
-v gitbot-data:/app/data

# Or bind mount to a host directory
-v /path/on/host/gitbot-data:/app/data
```

## LLM Providers

| Provider | Family | Cheap | Mid | Strong |
|---|---|---|---|---|
| **Google Gemini** | `gemini` | 2.5 Flash | 2.5 Pro | 2.5 Pro |
| **Anthropic** | `anthropic` | Haiku 4.5 | Sonnet 4.6 | Opus 4.8 |
| **OpenAI** | `openai` | GPT-4o-mini | GPT-4o | GPT-4o |
| **Ollama** | `ollama` | Qwen 2.5 7B | Qwen 2.5 32B | Qwen 2.5 32B |
| **Claude Code CLI** | `claude-code` | CLI | CLI | CLI |

The bot uses three model tiers: **cheap** (triage/context gathering), **mid** (planning/code/assignments), **strong** (code review/architecture). The brain dynamically selects the tier per task step.

## Admin Panel

Access the admin panel at `http://your-host:8042/admin` to:
- View connection status for GitLab and LLM
- Test connectivity
- Monitor active workflows with live progress
- View activity feed and workflow history

Disable with `GITBOT_ADMIN_ENABLED=false`.

## Architecture

```
Webhook → Gather (cheap) → Plan (mid) → Execute (model per step)
```

1. **Gather** — cheapest model fetches only the context needed
2. **Plan** — breaks task into atomic steps, assigns model tier per step
3. **Execute** — each step runs with 37 GitLab tools via native LLM tool calling
4. **Escalate** — if a step fails, automatically retries with a stronger model

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
      - GITBOT_LLM_FAMILY=gemini
      - GITBOT_LLM_API_KEY=AIza...
    volumes:
      - gitbot-data:/app/data
    restart: unless-stopped

volumes:
  gitbot-data:
```
