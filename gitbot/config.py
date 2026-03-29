import logging
from pathlib import Path

from pydantic_settings import BaseSettings

from gitbot.models import Family, Tier

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = {"env_prefix": "GITBOT_", "env_file": ".env"}

    # GitLab
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    webhook_secret: str = ""
    bot_username: str = "gitbot"
    gitlab_ssl_verify: bool = True

    # LLM — which provider to use
    llm_family: Family | None = None  # anthropic, gemini, openai, ollama, claude-code

    # Provider API keys — set whichever ones you have
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""

    # Self-hosted (Ollama, vLLM, etc.)
    vllm_url: str = ""       # e.g. http://localhost:8000/v1
    vllm_api_key: str = ""   # optional, some vLLM setups need one
    ollama_url: str = ""     # e.g. http://localhost:11434

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

    # Debug output — when enabled, failed workflows store debug logs
    # accessible via admin panel and optionally linked in error comments
    debug_output: bool = False

    # LLM timeout per API call in seconds (0 = no timeout)
    llm_timeout: int = 300

    # Server
    host: str = "0.0.0.0"
    port: int = 8042

    def get_llm_family(self) -> Family:
        """Get the active LLM family. Explicit setting, or first available key."""
        if self.llm_family:
            return self.llm_family
        # Pick first available
        if self.anthropic_api_key:
            return Family.ANTHROPIC
        if self.gemini_api_key:
            return Family.GEMINI
        if self.openai_api_key:
            return Family.OPENAI
        if self.ollama_url:
            return Family.OLLAMA
        return Family.CLAUDE_CODE

    def get_api_key(self) -> str:
        """Get the API key for the active family."""
        family = self.get_llm_family()
        return {
            Family.ANTHROPIC: self.anthropic_api_key,
            Family.GEMINI: self.gemini_api_key,
            Family.OPENAI: self.openai_api_key,
            Family.OLLAMA: self.vllm_api_key,
        }.get(family, "")

    def get_api_base(self) -> str | None:
        """Get the API base URL for the active family (only for self-hosted)."""
        family = self.get_llm_family()
        if family == Family.OLLAMA:
            return self.ollama_url or self.vllm_url or None
        if self.vllm_url:
            return self.vllm_url
        return None

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
        return bool(self.gitlab_token and self.get_api_key())

    @property
    def setup_needed(self) -> bool:
        return not self.gitlab_token or not self.get_api_key()

    @property
    def available_providers(self) -> list[str]:
        """Which providers have keys configured."""
        providers = []
        if self.anthropic_api_key:
            providers.append("anthropic")
        if self.gemini_api_key:
            providers.append("gemini")
        if self.openai_api_key:
            providers.append("openai")
        if self.ollama_url or self.vllm_url:
            providers.append("ollama/vllm")
        return providers


_ENV_TEMPLATE = """\
# GitBot Configuration
# Edit this file and restart, or configure via the admin panel at /admin

# GitLab
GITBOT_GITLAB_URL=https://gitlab.example.com
GITBOT_GITLAB_TOKEN=
GITBOT_BOT_USERNAME=gitbot
# GITBOT_GITLAB_SSL_VERIFY=true

# LLM Provider Keys — set whichever you have
# GITBOT_ANTHROPIC_API_KEY=sk-ant-...
# GITBOT_GEMINI_API_KEY=AIza...
# GITBOT_OPENAI_API_KEY=sk-...

# Self-hosted (optional)
# GITBOT_OLLAMA_URL=http://localhost:11434
# GITBOT_VLLM_URL=http://localhost:8000/v1
# GITBOT_VLLM_API_KEY=

# Active provider (defaults to first available key)
# GITBOT_LLM_FAMILY=anthropic

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
