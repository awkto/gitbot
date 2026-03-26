from pydantic_settings import BaseSettings

from gitbot.models import Family, Tier


class Settings(BaseSettings):
    model_config = {"env_prefix": "GITBOT_", "env_file": ".env"}

    # GitLab
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    webhook_secret: str = ""
    bot_username: str = "gitbot"
    gitlab_ssl_verify: bool = True

    # Model family: anthropic, openai, ollama, claude-code
    llm_family: Family = Family.CLAUDE_CODE

    # Optional per-tier model overrides (litellm model strings)
    llm_model_cheap: str | None = None
    llm_model_mid: str | None = None
    llm_model_strong: str | None = None

    # API config (not needed for claude-code family)
    llm_api_base: str | None = None  # for ollama: "http://localhost:11434"
    llm_api_key: str | None = None

    # Claude Code backend
    claude_code_path: str = "claude"  # path to claude CLI binary

    # Server
    host: str = "0.0.0.0"
    port: int = 8042

    def tier_overrides(self) -> dict[Tier, str] | None:
        overrides = {}
        if self.llm_model_cheap:
            overrides[Tier.CHEAP] = self.llm_model_cheap
        if self.llm_model_mid:
            overrides[Tier.MID] = self.llm_model_mid
        if self.llm_model_strong:
            overrides[Tier.STRONG] = self.llm_model_strong
        return overrides or None


settings = Settings()
