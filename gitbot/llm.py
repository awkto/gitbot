"""LLM completion layer — routes through litellm or Claude Code CLI.

Supports both simple completions and tool-use agentic loops.
"""

import asyncio
import json
import logging
import shutil

import litellm

from gitbot.config import settings
from gitbot.models import Family, Task, resolve_model

litellm.suppress_debug_info = True
log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 75


async def _claude_code_complete(system: str, prompt: str) -> str:
    """Shell out to `claude -p` for a completion. For dev/testing use."""
    claude_bin = settings.claude_code_path or shutil.which("claude") or "claude"

    full_prompt = prompt
    if system:
        full_prompt = f"{system}\n\n---\n\n{prompt}"

    proc = await asyncio.create_subprocess_exec(
        claude_bin, "-p", full_prompt, "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip()
        log.error("Claude Code failed (rc=%d): %s", proc.returncode, err)
        raise RuntimeError(f"Claude Code CLI error: {err}")

    return stdout.decode().strip()


async def complete(task: Task, *, system: str = "", prompt: str) -> str:
    """Simple completion — no tools."""
    family = settings.get_llm_family()
    model = resolve_model(family, task, settings.tier_overrides())

    log.info("LLM request: task=%s family=%s model=%s", task, family, model)

    if family == Family.CLAUDE_CODE:
        return await _claude_code_complete(system, prompt)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    timeout = settings.llm_timeout or None
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        api_base=settings.get_api_base(),
        api_key=settings.get_api_key(),
        timeout=timeout,
    )
    return response.choices[0].message.content


async def tool_loop(
    task: Task,
    *,
    system: str,
    prompt: str,
    tools: list[dict],
    execute_fn,
) -> list[dict]:
    """Run a tool-use loop using the model resolved from the task tier."""
    family = settings.get_llm_family()
    model = resolve_model(family, task, settings.tier_overrides())
    return await tool_loop_with_model(
        model=model, system=system, prompt=prompt, tools=tools, execute_fn=execute_fn,
    )


async def tool_loop_with_model(
    *,
    model: str,
    system: str,
    prompt: str,
    tools: list[dict],
    execute_fn,
) -> list[dict]:
    """Run an agentic tool-use loop with an explicit model.

    Sends tools to the LLM, executes tool_calls, feeds results back,
    repeats until the model stops calling tools (sends a text response).

    Args:
        model: litellm model string (e.g. "anthropic/claude-haiku-4-5-20251001")
        system: System prompt
        prompt: Initial user prompt
        tools: Tool schemas (litellm/OpenAI format)
        execute_fn: Callable(tool_name, args) -> str result

    Returns:
        List of actions taken: [{"tool": name, "args": {...}, "result": str}, ...]
    """
    log.info("Tool loop start: model=%s tools=%d", model, len(tools))

    # Claude Code CLI doesn't support tool_use
    if settings.get_llm_family() == Family.CLAUDE_CODE:
        log.warning("Claude Code backend doesn't support tool calling, using simple completion")
        result = await _claude_code_complete(system, prompt)
        return [{"tool": "_text_response", "args": {}, "result": result}]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    actions_taken = []

    for round_num in range(1, MAX_TOOL_ROUNDS + 1):
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            tools=tools,
            api_base=settings.get_api_base(),
            api_key=settings.get_api_key(),
            timeout=settings.llm_timeout or None,
        )

        if not response.choices:
            log.warning("Empty choices from model (round %d)", round_num)
            # Don't retry forever — after 3 empty responses, bail
            empty_count = sum(1 for a in actions_taken if a.get("tool") == "_empty_response")
            actions_taken.append({"tool": "_empty_response", "args": {}, "result": ""})
            if empty_count >= 2:
                log.error("Model returning empty choices repeatedly, aborting tool loop")
                actions_taken.append({"tool": "_text_response", "args": {},
                                      "result": "I encountered issues with the AI model. Some steps may not have completed."})
                break
            continue
        choice = response.choices[0]
        message = choice.message

        messages.append(message.model_dump(exclude_none=True))

        if message.tool_calls:
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = tool_call.function.arguments
                if isinstance(fn_args, str):
                    fn_args = json.loads(fn_args)

                log.info("Tool call [round %d]: %s(%s)",
                         round_num, fn_name, {k: str(v)[:60] for k, v in fn_args.items()})

                result = execute_fn(fn_name, fn_args)
                is_error = isinstance(result, str) and result.startswith("TOOL_ERROR:")
                actions_taken.append({"tool": fn_name, "args": fn_args, "result": result, "error": is_error})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

        elif message.content:
            actions_taken.append({"tool": "_text_response", "args": {}, "result": message.content})
            break

        else:
            break

        if choice.finish_reason == "stop":
            break

    log.info("Tool loop done: %d rounds, %d actions", round_num, len(actions_taken))
    return actions_taken
