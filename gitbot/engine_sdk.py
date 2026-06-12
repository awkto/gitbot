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
import re

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


# ---------------------------------------------------------------------------
# Shell policy — the gitbot container is not a devbox (github/gitbot#24)
# ---------------------------------------------------------------------------

# Commands the agent may run in a workspace under local_exec=light.
# Deliberately small: enough to use git and run code that already exists.
# Builds, installs and services belong to the project's CI/CD.
_LIGHT_ALLOWED_COMMANDS = {
    "git", "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "diff",
    "echo", "pwd", "cd", "mkdir", "touch", "cp", "mv", "rm", "sed", "awk",
    "python", "python3", "pytest", "ruff", "make", "sh", "true", "false",
    "which", "env", "sort", "uniq", "tr", "cut", "xargs", "test", "[",
}

# Even for allowed interpreters, these substrings mean "installing things" —
# denied under light policy regardless of the leading command.
_LIGHT_DENY_MARKERS = (
    "pip install", "pip3 install", "apt ", "apt-get", "dnf ", "yum ",
    "docker", "podman", "sudo ", "curl ", "wget ", "nc ", "ssh ",
    "systemctl", "service ", "npm install", "npm i ", "yarn add",
    "pip download", "easy_install", "& disown", "nohup ",
)


def _light_policy_violation(command: str) -> str | None:
    """Return a denial reason if `command` violates the light shell policy."""
    lowered = f" {command.lower()} "
    for marker in _LIGHT_DENY_MARKERS:
        if marker in lowered:
            return (f"'{marker.strip()}' is not allowed: GitBot's container is not a "
                    "devbox. Push your branch and let the project's CI/CD run "
                    "builds, installs and full test suites.")

    # Check the leading word of every pipeline/sequence segment
    for segment in re.split(r"&&|\|\||;|\||\n", command):
        segment = segment.strip()
        if not segment:
            continue
        word = segment.split()[0].rsplit("/", 1)[-1]
        if word.startswith("$") or "=" in word:  # env assignment prefix
            parts = segment.split()
            word = next((p.rsplit("/", 1)[-1] for p in parts[1:] if "=" not in p), "")
        if word and word not in _LIGHT_ALLOWED_COMMANDS:
            return (f"'{word}' is not in the allowed command set for this "
                    "workspace (git + lightweight inspection/run commands only). "
                    "Rely on CI/CD for builds and tests.")
    return None


def _bash_policy_hook():
    """PreToolUse hook enforcing the light shell policy on Bash calls."""
    async def policy(input_data, tool_use_id, context):
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        if input_data.get("tool_name") != "Bash":
            return {}
        command = (input_data.get("tool_input") or {}).get("command", "")
        reason = _light_policy_violation(command)
        if reason:
            log.info("Shell policy denied: %s", command[:120])
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        return {}
    return policy


# ---------------------------------------------------------------------------
# Implement workflow (issue assigned → branch + MR) — github/gitbot#20
# ---------------------------------------------------------------------------

IMPLEMENT_MAX_TURNS = 60

IMPLEMENT_SYSTEM = """\
You are GitBot, an AI developer inside a GitLab instance. You have been
assigned issue #{target_iid} ("{target_title}") in project "{project_name}"
(ID: {project_id}).

A working clone of the repository is in your current directory. Implement
what the issue asks for:

1. Read the issue and explore the repo (files, structure, conventions).
2. Create a branch named exactly `{branch_name}` (`git checkout -b {branch_name}`).
3. Make the changes with the file tools. Match the existing code style.
4. You may run existing code, tests or linters for a quick sanity check, but
   this container is NOT a devbox: do not install packages, run docker, or
   start services. The project's CI/CD pipeline is what builds and tests
   your MR — rely on it.
5. Commit with a clear message and push: `git push -u origin {branch_name}`.
6. Create a merge request with the `create_merge_request` GitLab tool:
   source_branch={branch_name}, target the default branch, and include
   `Closes #{target_iid}` in the description.

Rules:
- The git remote is already authenticated — plain `git push` works.
- Branch name MUST be exactly `{branch_name}` — the harness verifies it.
- Do NOT close the issue, do NOT merge the MR, do NOT commit to the default branch.
- Do NOT use post_comment for your final summary — your final text response is
  posted to the issue automatically.
- If the issue is impossible or too ambiguous to implement, make no commits and
  explain why in your final response, starting it with the word BLOCKED.
"""


def _clone_repo(project_id: int, workdir: str) -> tuple[str, str]:
    """Clone the project into workdir/repo. Returns (repo_path, default_branch)."""
    import subprocess

    from gitbot import gitlab_client as glc

    gl = glc.get_client()
    project = gl.projects.get(project_id)
    default_branch = project.default_branch or "main"
    path_ns = project.path_with_namespace

    base = settings.gitlab_url.split("://", 1)[1].rstrip("/")
    url = f"https://oauth2:{settings.gitlab_token}@{base}/{path_ns}.git"

    repo_path = os.path.join(workdir, "repo")
    subprocess.run(
        ["git", "clone", "--depth", "50", url, repo_path],
        check=True, capture_output=True, timeout=120,
    )
    bot = settings.bot_username
    for k, v in (("user.name", "GitBot"), ("user.email", f"{bot}@{base}")):
        subprocess.run(["git", "-C", repo_path, "config", k, v],
                       check=True, capture_output=True)
    return repo_path, default_branch


def _finalize_no_shell(repo_path: str, branch_name: str, sit: Situation,
                       default_branch: str) -> str | None:
    """local_exec=none: branch, commit, push and open the MR on the agent's behalf.

    Returns an error string, or None on success (including 'nothing to commit',
    which the verification gate will then report).
    """
    import subprocess

    from gitbot import gitlab_client as glc

    def run(*args):
        return subprocess.run(["git", "-C", repo_path, *args],
                              check=True, capture_output=True, timeout=60)

    try:
        status = subprocess.run(["git", "-C", repo_path, "status", "--porcelain"],
                                check=True, capture_output=True, timeout=30)
        if not status.stdout.strip():
            return None  # no changes — gate will fail with a clear reason
        run("checkout", "-b", branch_name)
        run("add", "-A")
        run("commit", "-m", f"GitBot: {sit.target_title} (#{sit.target_iid})")
        run("push", "-u", "origin", branch_name)
        glc.create_merge_request(
            sit.project_id,
            source_branch=branch_name,
            target_branch=default_branch,
            title=f"Resolve \"{sit.target_title}\"",
            description=f"Closes #{sit.target_iid}",
        )
        return None
    except subprocess.CalledProcessError as e:
        return f"`git {' '.join(e.cmd[3:])}` failed: {e.stderr.decode()[:200]}"
    except Exception as e:
        return str(e)[:200]


def _verify_implement(project_id: int, issue_iid: int, branch_name: str) -> tuple[dict | None, str]:
    """Structural finish gate: the MR must exist, be open, and contain commits.

    Returns (mr_info, "") on success or (None, reason) on failure.
    """
    from gitbot import gitlab_client as glc

    gl = glc.get_client()
    project = gl.projects.get(project_id)

    mrs = project.mergerequests.list(source_branch=branch_name, state="opened")
    if not mrs:
        try:
            project.branches.get(branch_name)
            return None, f"branch `{branch_name}` was pushed but no open MR exists for it"
        except Exception:
            return None, f"no branch `{branch_name}` and no MR were created"

    mr = mrs[0]
    commits = list(mr.commits())
    if not commits:
        return None, f"MR !{mr.iid} exists but contains no commits"

    return {"iid": mr.iid, "url": mr.web_url, "title": mr.title,
            "commits": len(commits)}, ""


async def run_implement(sit: Situation) -> tuple[str, bool]:
    """Run the implement workflow: clone → SDK loop → verify.

    Returns (markdown_summary, success). The summary is posted by the caller;
    success=False means the finish gate failed or the agent reported BLOCKED.
    """
    import shutil
    import tempfile

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    if settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    branch_name = f"gitbot/issue-{sit.target_iid}"
    workdir = tempfile.mkdtemp(prefix=f"gitbot-impl-{sit.project_id}-{sit.target_iid}-")

    try:
        repo_path, default_branch = await asyncio.to_thread(
            _clone_repo, sit.project_id, workdir
        )
    except Exception as e:
        log.error("Clone failed for project %s: %s", sit.project_id, e)
        shutil.rmtree(workdir, ignore_errors=True)
        return (f":x: **Could not clone the repository**: `{str(e)[:200]}`\n\n"
                "Check that the repo is initialized and the bot has access."), False

    # Shell policy (github/gitbot#24): the container is not a devbox.
    mode = settings.local_exec
    allowed = ["Read", "Write", "Edit", "Glob", "Grep",
               "mcp__gitlab__*", "WebSearch", "WebFetch"]
    hooks = None
    system_prompt = IMPLEMENT_SYSTEM
    if mode == "none":
        system_prompt = system_prompt.replace(
            "2. Create a branch named exactly", "2. (handled by the harness)",
        )
        system_prompt += (
            "\nNOTE: You have NO shell in this mode. Just edit the files; the "
            "harness will branch, commit, push and open the merge request for you. "
            "Do not call create_merge_request yourself."
        )
    else:
        allowed.insert(5, "Bash")
        if mode != "full":  # light (default)
            from claude_agent_sdk import HookMatcher
            hooks = {"PreToolUse": [HookMatcher(matcher="Bash",
                                                hooks=[_bash_policy_hook()])]}

    options = ClaudeAgentOptions(
        system_prompt=system_prompt.format(
            target_iid=sit.target_iid,
            target_title=sit.target_title,
            project_name=sit.project_name,
            project_id=sit.project_id,
            branch_name=branch_name,
        ),
        model="claude-sonnet-4-6",
        mcp_servers={"gitlab": _build_gitlab_mcp_server(sit.project_id)},
        allowed_tools=allowed,
        permission_mode="dontAsk",
        max_turns=IMPLEMENT_MAX_TURNS,
        setting_sources=[],
        cwd=repo_path,
        hooks=hooks,
    )

    prompt = (
        f"Issue #{sit.target_iid}: {sit.target_title}\n\n"
        f"{sit.target_description or '(no description)'}\n\n"
        f"The repository default branch is `{default_branch}`."
    )

    log.info("SDK engine: implement workflow start (Issue #%s, branch=%s)",
             sit.target_iid, branch_name)

    final_text = ""
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                final_text = message.result or ""
                log.info("SDK engine: implement loop done (subtype=%s, cost=%s)",
                         getattr(message, "subtype", "?"),
                         getattr(message, "total_cost_usd", None))

        # local_exec=none: the agent only edited files — branch/commit/push/MR
        # are the harness's job.
        if mode == "none" and not final_text.strip().startswith("BLOCKED"):
            err = await asyncio.to_thread(
                _finalize_no_shell, repo_path, branch_name, sit, default_branch
            )
            if err:
                return (f":warning: **GitBot edited files but publishing failed**: "
                        f"{err}\n\nAgent's report:\n\n{final_text}"), False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if final_text.strip().startswith("BLOCKED"):
        return (f":no_entry: **GitBot could not implement this issue.**\n\n"
                f"{final_text.strip()[7:].strip()}"), False

    # Structural finish gate — never report success the API can't confirm
    mr_info, reason = await asyncio.to_thread(
        _verify_implement, sit.project_id, sit.target_iid, branch_name
    )
    if mr_info is None:
        log.warning("Implement gate failed for Issue #%s: %s", sit.target_iid, reason)
        return (f":warning: **GitBot finished but verification failed**: {reason}.\n\n"
                f"Agent's report:\n\n{final_text}"), False

    return (f":white_check_mark: **Implemented in MR !{mr_info['iid']}** — "
            f"[{mr_info['title']}]({mr_info['url']}) "
            f"({mr_info['commits']} commit{'s' if mr_info['commits'] != 1 else ''})\n\n"
            f"{final_text}"), True
