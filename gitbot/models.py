"""Model families, tiers, and routing.

Each task gets a 'tier' (cheap, mid, strong). Each model family maps tiers to
concrete model identifiers that litellm understands.
"""

from enum import StrEnum


class Tier(StrEnum):
    CHEAP = "cheap"    # classification, routing, clarification questions
    MID = "mid"        # summaries, issue analysis, @mention responses
    STRONG = "strong"  # code review, security analysis, architecture decisions


class Family(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    CLAUDE_CODE = "claude-code"


# Default model for each (family, tier). Users can override via config.
FAMILY_DEFAULTS: dict[Family, dict[Tier, str]] = {
    Family.ANTHROPIC: {
        Tier.CHEAP: "anthropic/claude-haiku-4-5-20251001",
        Tier.MID: "anthropic/claude-sonnet-4-20250514",
        Tier.STRONG: "anthropic/claude-opus-4-20250514",
    },
    Family.OPENAI: {
        Tier.CHEAP: "gpt-4o-mini",
        Tier.MID: "gpt-4o",
        Tier.STRONG: "o3",
    },
    Family.GEMINI: {
        Tier.CHEAP: "gemini/gemini-3-flash-preview",
        Tier.MID: "gemini/gemini-3-flash-preview",
        Tier.STRONG: "gemini/gemini-3.1-pro-preview",
    },
    Family.OLLAMA: {
        Tier.CHEAP: "ollama/qwen2.5-coder:7b",
        Tier.MID: "ollama/qwen2.5-coder:32b",
        Tier.STRONG: "ollama/qwen2.5-coder:32b",
    },
    Family.CLAUDE_CODE: {
        # Claude Code CLI - tier doesn't matter, it uses whatever model is configured
        Tier.CHEAP: "claude-code",
        Tier.MID: "claude-code",
        Tier.STRONG: "claude-code",
    },
}


class Task(StrEnum):
    """The bot tasks, each mapped to a tier."""
    CLASSIFY = "classify"
    CLARIFY = "clarify"
    ISSUE_ANALYSIS = "issue_analysis"
    MR_SUMMARY = "mr_summary"
    CODE_REVIEW = "code_review"
    MENTION_RESPONSE = "mention_response"
    CONTEXT_GATHER = "context_gather"
    IMPLEMENT = "implement"
    TRIAGE = "triage"
    PLAN = "plan"


TASK_TIERS: dict[Task, Tier] = {
    Task.CLASSIFY: Tier.CHEAP,
    Task.CLARIFY: Tier.CHEAP,
    Task.CONTEXT_GATHER: Tier.CHEAP,
    Task.ISSUE_ANALYSIS: Tier.MID,
    Task.MR_SUMMARY: Tier.MID,
    Task.MENTION_RESPONSE: Tier.MID,
    Task.CODE_REVIEW: Tier.STRONG,
    Task.IMPLEMENT: Tier.MID,
    Task.TRIAGE: Tier.CHEAP,
    Task.PLAN: Tier.MID,
}


def resolve_model(family: Family, task: Task, overrides: dict[Tier, str] | None = None) -> str:
    """Get the concrete model string for a given family and task."""
    tier = TASK_TIERS[task]
    if overrides and tier in overrides:
        return overrides[tier]
    return FAMILY_DEFAULTS[family][tier]
