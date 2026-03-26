"""LLM completion layer — routes through litellm or Claude Code CLI."""

import asyncio
import logging
import shutil

import litellm

from gitbot.config import settings
from gitbot.models import Family, Task, resolve_model

litellm.suppress_debug_info = True
log = logging.getLogger(__name__)


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


async def _litellm_complete(model: str, system: str, prompt: str) -> str:
    """Standard litellm completion."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        api_base=settings.llm_api_base,
        api_key=settings.llm_api_key,
    )
    return response.choices[0].message.content


async def complete(task: Task, *, system: str = "", prompt: str) -> str:
    """Run a completion for the given task, using the configured family and tier."""
    family = settings.llm_family
    model = resolve_model(family, task, settings.tier_overrides())

    log.info("LLM request: task=%s family=%s model=%s", task, family, model)

    if family == Family.CLAUDE_CODE:
        return await _claude_code_complete(system, prompt)

    return await _litellm_complete(model, system, prompt)
