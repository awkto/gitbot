"""Layered configuration.

Precedence, highest first:
  1. Real environment variables (GITBOT_*) — automation owns these; the app
     never overrides them and the admin UI shows them read-only.
  2. The persisted store (data/config.json, inside the data volume) — what the
     admin panel and onboarding write. Survives container recreation.
  3. A legacy .env file — seed for old deployments; a UI edit (store) beats it.
  4. Built-in defaults.

This is the standard "env wins" pattern (Grafana/Gitea-style): declarative
deployments stay deterministic, UI-driven deployments persist their edits, and
hybrids work per-key.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

log = logging.getLogger(__name__)

# Overridable so tests can point at a scratch store.
STORE_PATH = Path(os.environ.get("GITBOT_CONFIG_STORE", "data/config.json"))


def read_store() -> dict:
    """The persisted runtime config (empty when none has been written)."""
    try:
        return json.loads(STORE_PATH.read_text())
    except Exception:
        return {}


class _StoreSource(PydanticBaseSettingsSource):
    """pydantic-settings source backed by the JSON store."""

    def __init__(self, settings_cls):
        super().__init__(settings_cls)
        self._data = read_store()

    def get_field_value(self, field, field_name):  # pragma: no cover - unused hook
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._data.items()
                if k in self.settings_cls.model_fields and v is not None}


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

    # Nightly deep audit (#30): every this-many hours, scan recently-DONE
    # todos for callouts lost invisibly (GitLab auto-completes a mention todo
    # when the bot posts ANY comment — a concurrent session's placeholder
    # counts — so a lost webhook + that race leaves no pending trace). Pure
    # API scan; an LLM session runs only if something lost is found. 0 = off.
    deep_audit_hours: int = 24

    # Failure-triggered escalation (#31): when an implement/orchestrate
    # attempt fails, a cheap diagnosis classifies the failure and the harness
    # retries ONCE — capability failures one model tier up, transient ones on
    # the same tier; environment/impossible failures stand. Bounded extra
    # spend: max 2 attempts, 1 tier step, never above a pinned model.
    escalation_enabled: bool = True

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

    # Admin panel. When admin_password is set, /admin and /admin/api/* require
    # HTTP Basic auth (any username, this password).
    admin_enabled: bool = True
    admin_password: str = ""

    # Debug output — when enabled, failed workflows store debug logs
    # accessible via admin panel and optionally linked in error comments
    debug_output: bool = False

    # Server
    host: str = "0.0.0.0"
    port: int = 8042

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings,
        dotenv_settings, file_secret_settings,
    ):
        # env wins over the store; the store wins over legacy .env and defaults.
        return (init_settings, env_settings, _StoreSource(settings_cls),
                dotenv_settings, file_secret_settings)

    @property
    def is_configured(self) -> bool:
        return bool(self.gitlab_token and self.anthropic_api_key)

    @property
    def setup_needed(self) -> bool:
        return not self.is_configured


def env_locked(key: str) -> bool:
    """True when a real environment variable owns this key — the UI must not
    edit it (env always wins, so a store write would be a silent no-op)."""
    return f"GITBOT_{key.upper()}" in os.environ


def config_sources() -> dict[str, str]:
    """Per-key provenance for the admin UI: env (locked) / store / default.

    Legacy .env values report as "default" — they behave like editable
    defaults, since a store write overrides them."""
    store = read_store()
    out = {}
    for key in Settings.model_fields:
        if env_locked(key):
            out[key] = "env"
        elif key in store:
            out[key] = "store"
        else:
            out[key] = "default"
    return out


def save_config(updates: dict) -> tuple[list[str], list[str]]:
    """Persist UI-edited keys to the store and apply them live.

    Env-owned keys are skipped (env wins). Returns (applied, locked)."""
    store = read_store()
    applied, locked = [], []
    for key, value in updates.items():
        if key not in Settings.model_fields:
            continue
        if env_locked(key):
            locked.append(key)
            continue
        coerced = TypeAdapter(Settings.model_fields[key].annotation).validate_python(value)
        store[key] = coerced
        setattr(settings, key, coerced)
        applied.append(key)
    if applied:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.write_text(json.dumps(store, indent=2))
        try:
            STORE_PATH.chmod(0o600)
        except Exception:  # pragma: no cover - permissions best-effort
            pass
        log.info("Config saved to store: %s", ", ".join(applied))
    return applied, locked


settings = Settings()
