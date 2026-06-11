"""Claude Agent SDK engine — single agentic loop per workflow.

Spike scope (github/gitbot#19): the mention/respond workflow runs through the
SDK when GITBOT_ENGINE=sdk. The legacy brain remains the default engine.

The SDK runs the loop (planning, tool sequencing, adaptation); we supply the
GitLab tools as an in-process MCP server and keep the surrounding harness
(webhooks, triage, locks, placeholder UX) unchanged.
"""

import asyncio
import logging
import os

from gitbot.config import settings
from gitbot.context import Situation
from gitbot.tools import TOOL_SCHEMAS, execute_tool

log = logging.getLogger(__name__)

MAX_TURNS = 40

MENTION_SYSTEM = """\
You are GitBot, an AI teammate inside a GitLab instance. A user mentioned you
in a comment (or replied to you) on {target_type} #{target_iid} ("{target_title}")
in project "{project_name}" (ID: {project_id}).

Use the GitLab tools to gather any context you need (conversation history,
diffs, files, related issues) and to take any actions the user asks for.

Rules:
- When a tool needs a project_id and the user means the current project, use {project_id}.
- If a tool returns an error, try a different approach rather than repeating the call.
- Do NOT use the post_comment tool for your final answer — your final text
  response is delivered to the user as a comment automatically. Only use
  post_comment for additional comments on OTHER issues or merge requests.
- Be concise and direct. Markdown is supported.
"""


def _build_gitlab_mcp_server(project_id: int):
    """Expose the existing GitLab tool set as an in-process SDK MCP server."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    sdk_tools = []
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]

        def make_handler(tool_name: str):
            async def handler(args: dict):
                # execute_tool is sync (python-gitlab) — keep the loop responsive
                result = await asyncio.to_thread(
                    execute_tool, tool_name, dict(args), project_id=project_id
                )
                is_error = isinstance(result, str) and result.startswith("TOOL_ERROR:")
                return {
                    "content": [{"type": "text", "text": str(result)}],
                    "is_error": is_error,
                }
            return handler

        sdk_tools.append(
            tool(fn["name"], fn["description"], fn["parameters"])(make_handler(fn["name"]))
        )

    return create_sdk_mcp_server(name="gitlab", version="1.0.0", tools=sdk_tools)


async def run_mention(sit: Situation) -> str:
    """Run the mention/respond workflow as a single SDK agentic loop.

    Returns the agent's final text (posted as the reply by the caller).
    """
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    # The SDK subprocess authenticates via ANTHROPIC_API_KEY
    if settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    options = ClaudeAgentOptions(
        system_prompt=MENTION_SYSTEM.format(
            target_type=sit.target_type,
            target_iid=sit.target_iid,
            target_title=sit.target_title,
            project_name=sit.project_name,
            project_id=sit.project_id,
        ),
        model="claude-sonnet-4-6",
        mcp_servers={"gitlab": _build_gitlab_mcp_server(sit.project_id)},
        allowed_tools=["mcp__gitlab__*", "WebSearch", "WebFetch"],
        permission_mode="dontAsk",  # deny anything not explicitly allowed
        max_turns=MAX_TURNS,
        setting_sources=[],  # don't load any .claude settings from disk
        cwd=settings.state_db_path.rsplit("/", 1)[0] or ".",
    )

    prompt_parts = []
    if sit.target_description:
        prompt_parts.append(
            f"{sit.target_type} #{sit.target_iid} description:\n{sit.target_description}"
        )
    prompt_parts.append(f"Comment by @{sit.actor}:\n{sit.comment_body}")
    prompt = "\n\n".join(prompt_parts)

    log.info("SDK engine: mention workflow start (%s #%s, max_turns=%d)",
             sit.target_type, sit.target_iid, MAX_TURNS)

    final_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            final_text = message.result or ""
            cost = getattr(message, "total_cost_usd", None)
            log.info("SDK engine: done (subtype=%s, cost=%s)",
                     getattr(message, "subtype", "?"), cost)

    return final_text or "*(no response produced)*"
