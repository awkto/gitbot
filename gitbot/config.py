import logging
from pathlib import Path

from pydantic_settings import BaseSettings

from gitbot.models import Family, Tier

log = logging.getLogger(__name__)

# Keys that indicate a specific provider
_ANTHROPIC_PREFIX = "sk-ant-"
_GEMINI_PREFIX = "AIza"
_OPENAI_PREFIX = "sk-"


class Settings(BaseSettings):
    model_config = {"env_prefix": "GITBOT_", "env_file": ".env"}

    # GitLab
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    webhook_secret: str = ""
    bot_username: str = "gitbot"
    gitlab_ssl_verify: bool = True

    # LLM — family is auto-detected from API key if not set explicitly
    llm_family: Family | None = None
    llm_api_key: str = ""
    llm_api_base: str | None = None  # for ollama: "http://localhost:11434"

    # Optional per-tier model overrides (litellm model strings)
    llm_model_cheap: str | None = None
    llm_model_mid: str | None = None
    llm_model_strong: str | None = None

    # Claude Code backend
    claude_code_path: str = "claude"

    # State
    state_db_path: str = "data/gitbot.db"

    # Admin panel
    admin_enabled: bool = True
    admin_password: str = ""

    # Server
    host: str = "0.0.0.0"
    port: int = 8042

    def get_llm_family(self) -> Family:
        """Resolve LLM family: explicit setting > auto-detect from key > fallback."""
        if self.llm_family:
            return self.llm_family

        key = self.llm_api_key
        if key.startswith(_ANTHROPIC_PREFIX):
            return Family.ANTHROPIC
        if key.startswith(_GEMINI_PREFIX):
            return Family.GEMINI
        if key.startswith(_OPENAI_PREFIX):
            return Family.OPENAI
        if self.llm_api_base and "11434" in self.llm_api_base:
            return Family.OLLAMA

        return Family.CLAUDE_CODE  # fallback

    def tier_overrides(self) -> dict[Tier, str] | None:
        overrides = {}
        if self.llm_model_cheap:
            overrides[Tier.CHEAP] = self.llm_model_cheap
        if self.llm_model_mid:
            overrides[Tier.MID] = self.llm_model_mid
        if self.llm_model_strong:
            overrides[Tier.STRONG] = self.llm_model_strong
        return overrides or None

    @property
    def is_configured(self) -> bool:
        return bool(self.gitlab_token and self.llm_api_key)

    @property
    def setup_needed(self) -> bool:
        return not self.gitlab_token


_ENV_TEMPLATE = """\
# GitBot Configuration
# Edit this file and restart, or configure via the admin panel at /admin

# GitLab instance
GITBOT_GITLAB_URL=https://gitlab.example.com
GITBOT_GITLAB_TOKEN=
GITBOT_BOT_USERNAME=gitbot
# GITBOT_GITLAB_SSL_VERIFY=true
# GITBOT_WEBHOOK_SECRET=

# LLM Provider — auto-detected from API key if not set:
#   sk-ant-... → anthropic
#   AIza...    → gemini
#   sk-...     → openai
# Or set explicitly: anthropic, gemini, openai, ollama, claude-code
# GITBOT_LLM_FAMILY=
GITBOT_LLM_API_KEY=

# Optional: override models per tier
# GITBOT_LLM_MODEL_CHEAP=
# GITBOT_LLM_MODEL_MID=
# GITBOT_LLM_MODEL_STRONG=

# Admin panel (disable with false for production)
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
