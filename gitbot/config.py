import logging
from pathlib import Path

from pydantic_settings import BaseSettings

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    # extra="ignore": a stale .env from the multi-provider era must not
    # break startup.
    model_config = {"env_prefix": "GITBOT_", "env_file": ".env", "extra": "ignore"}

    # GitLab
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    webhook_secret: str = ""
    bot_username: str = "gitbot"
    gitlab_ssl_verify: bool = True

    # Anthropic — GitBot is a Claude Agent SDK app
    anthropic_api_key: str = ""

    # Model for the two cheap triage classifier calls (the only LLM calls
    # outside the Agent SDK loops). Dateless alias so it tracks the tier.
    classifier_model: str = "claude-haiku-4-5"

    # Shell policy for SDK clone workspaces. GitBot's container is not a
    # devbox — builds and tests belong to the project's CI/CD pipelines.
    #   none  — no shell; agent edits files, harness commits/pushes/opens the MR
    #   light — git + a small allowlist (run existing code/tests); installs,
    #           docker, sudo and network tools are denied (default)
    #   full  — unrestricted shell (explicit override; or point the bot at a
    #           dedicated devbox via a future executor tool/MCP instead)
    local_exec: str = "light"

    # Reconciliation sweep interval in minutes (0 = disabled). Picks up
    # orphaned (crashed) and parked (gitbot::waiting) work. An external
    # scheduler can also POST /reconcile with the webhook secret.
    reconcile_minutes: int = 10

    # Clarifying questions: after researching, the agent scores a would-be
    # question 1-10 against the rubric in engine_sdk.QUESTION_SCALE (defined
    # in app code so every model instance ranks consistently) and only asks
    # when the score meets this threshold. Tunable live from the admin panel.
    question_threshold: int = 7

    # Per-workflow model for the SDK engine. "auto" = the harness decides
    # from the triage classifier's complexity score. Otherwise an alias
    # ("haiku"/"sonnet"/"opus" — the SDK resolves these to the CURRENT model
    # of that tier, so we never chase Anthropic releases) or a pinned model
    # id (e.g. "claude-opus-4-8"). Live-tunable from the admin panel.
    model_mention: str = "auto"
    model_implement: str = "auto"
    model_orchestrate: str = "auto"
    model_review: str = "auto"  # consumed by the SDK review workflow (#22)

    # State
    state_db_path: str = "data/gitbot.db"

    # Admin panel
    admin_enabled: bool = True
    admin_password: str = ""

    # Debug output — when enabled, failed workflows store debug logs
    # accessible via admin panel and optionally linked in error comments
    debug_output: bool = False

    # Server
    host: str = "0.0.0.0"
    port: int = 8042

    @property
    def is_configured(self) -> bool:
        return bool(self.gitlab_token and self.anthropic_api_key)

    @property
    def setup_needed(self) -> bool:
        return not self.is_configured


_ENV_TEMPLATE = """\
# GitBot Configuration
# Edit this file and restart, or configure via the admin panel at /admin

# GitLab
GITBOT_GITLAB_URL=https://gitlab.example.com
GITBOT_GITLAB_TOKEN=
GITBOT_BOT_USERNAME=gitbot
# GITBOT_GITLAB_SSL_VERIFY=true

# Anthropic
GITBOT_ANTHROPIC_API_KEY=sk-ant-...

# Admin panel
GITBOT_ADMIN_ENABLED=true
"""


def ensure_env_file():
    """Create a template .env if none exists."""
    for path in [Path(".env"), Path("data/.env")]:
        if path.exists():
            return
    Path(".env").write_text(_ENV_TEMPLATE)
    log.info("Created template .env file — configure via /admin or edit .env")


settings = Settings()
