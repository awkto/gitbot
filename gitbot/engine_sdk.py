"""Claude Agent SDK engine — single agentic loop per workflow.

The SDK runs the inner loop (planning, tool sequencing, adaptation); we
supply the GitLab tools as an in-process MCP server and keep the outer
harness (webhooks, triage, locks, labels, placeholder UX) in brain.py.

Workflows: mention (answer callouts), implement (issue → branch + MR),
orchestrate (multi-project/admin/CI), review (MR → inline findings + verdict).
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
MENTION_MAX_TURNS = 12  # answers are cheap: context is pre-fetched, keep it light


# Complexity rubric for the triage classifier — in app code so every model
# instance rates tasks on the same scale (like QUESTION_SCALE).
COMPLEXITY_SCALE = """\
Also rate the task's complexity 1-10:
- 1-3 TRIVIAL: single-step or factual — answer a question, rename something,
  post a comment, one small edit.
- 4-7 STANDARD: one feature/fix/config touching a few files or steps in one
  project; routine CI or project setup.
- 8-10 COMPLEX: multi-project orchestration, architecture or
  security-sensitive changes, deep debugging, long pipelines with many
  dependent stages, anything where a wrong step is expensive."""

_TIER_ALIAS = {"cheap": "haiku", "mid": "sonnet", "strong": "opus"}


def _workflow_model(workflow: str, complexity: int | None) -> str:
    """Model selection is the harness's job, not the SDK's.

    Admin override per workflow wins (alias or pinned id); "auto" maps the
    triage complexity score to a tier ALIAS — the SDK resolves haiku/sonnet/
    opus to the current model of that tier, so nothing here goes stale when
    Anthropic ships or sunsets models.
    """
    override = getattr(settings, f"model_{workflow}", "auto") or "auto"
    if override != "auto":
        return override
    c = complexity if complexity is not None else 5
    if workflow == "mention":
        tier = "cheap" if c <= 3 else "mid"
    elif workflow == "review":
        tier = "strong"
    else:  # implement / orchestrate — never below mid: it writes things
        tier = "strong" if c >= 8 else "mid"
    return _TIER_ALIAS[tier]


_CLASSIFY_RE = re.compile(r"\b([a-z_]+)\b(?:\s+(\d{1,2}))?")


def _parse_classification(raw: str, valid: set[str], default: str) -> tuple[str, int | None]:
    """Parse '<word> <complexity>' from a classifier reply, tolerantly."""
    complexity = None
    word = default
    for m in _CLASSIFY_RE.finditer(raw.strip().lower()):
        if m.group(1) in valid:
            word = m.group(1)
            if m.group(2):
                complexity = max(1, min(10, int(m.group(2))))
            break
    if complexity is None:
        m = re.search(r"\b(\d{1,2})\b", raw)
        if m:
            complexity = max(1, min(10, int(m.group(1))))
    return word, complexity


def _data_dir() -> str:
    # Absolute: this path is consumed by SDK subprocesses whose cwd is a
    # per-task workspace — a relative path would resolve somewhere else for
    # every workflow and break session resume (#25).
    return os.path.abspath(os.path.dirname(settings.state_db_path) or "data")


def _setup_subprocess_env() -> None:
    """Env for the SDK subprocess (inherited by Bash tools): Anthropic auth
    and glab CLI auth against the configured GitLab instance."""
    if settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.gitlab_token:
        host = settings.gitlab_url.split("://", 1)[-1].rstrip("/")
        os.environ.setdefault("GITLAB_HOST", host)
        os.environ.setdefault("GITLAB_TOKEN", settings.gitlab_token)
    # SDK session transcripts must live in the data volume so resume
    # (github/gitbot#25) survives container recreation.
    cfg = os.path.join(_data_dir(), "claude")
    os.makedirs(cfg, exist_ok=True)
    os.environ.setdefault("CLAUDE_CONFIG_DIR", cfg)


def _workspace_dir(kind: str, sit) -> str:
    """Deterministic per-target workspace (github/gitbot#25). SDK sessions are
    stored per-cwd, so resuming a session requires running from the SAME
    directory — a random tempdir would orphan the transcript. Living in the
    data volume also preserves partial work (clones, scratch) across restarts."""
    return os.path.join(_data_dir(), "workspaces",
                        f"{kind}-{sit.project_id}-{sit.target_iid}")


MENTION_SYSTEM = """\
You are GitBot, an AI teammate inside a GitLab instance. A user mentioned you
in a comment (or replied to you) on {target_type} #{target_iid} ("{target_title}")
in project "{project_name}" (ID: {project_id}).

This is a QUESTION/ANSWER interaction, not a work task. The conversation
context is already provided below — answer directly from it whenever you can.
You have a small turn budget ({max_turns} turns); use tools only when the
provided context genuinely doesn't contain the answer.

Rules:
- When a tool needs a project_id and the user means the current project, use {project_id}.
- The conversation may span several past runs of work on this issue. Resources
  mentioned in older comments (projects, branches, pipelines) may have been
  deleted or replaced since — verify a resource still exists before citing it
  as current fact in your answer.
- If a tool returns an error, try a different approach rather than repeating the call.
- Do NOT use the post_comment tool for your final answer — your final text
  response is delivered to the user as a threaded reply automatically.
- Be concise and direct. Markdown is supported.
"""


def _mention_context(sit: Situation) -> str:
    """Pre-fetch the conversation so a simple answer needs zero tool calls:
    the triggering discussion's full thread, plus recent comments elsewhere
    on the target."""
    from gitbot import gitlab_client as glc

    parts = []
    try:
        gl = glc.get_client()
        project = gl.projects.get(sit.project_id)
        if sit.target_type == "MergeRequest":
            target = project.mergerequests.get(sit.target_iid)
        else:
            target = project.issues.get(sit.target_iid)

        if sit.discussion_id:
            try:
                disc = target.discussions.get(sit.discussion_id)
                thread = [
                    f"@{n.get('author', {}).get('username', '?')}: {n.get('body', '')[:500]}"
                    for n in disc.attributes.get("notes", []) if not n.get("system")
                ]
                if thread:
                    parts.append("Current thread (oldest first):\n" + "\n\n".join(thread))
            except Exception:
                pass

        notes = [n for n in target.notes.list(per_page=15, sort="desc")
                 if not n.system]
        recent = [
            f"[{n.created_at[:16]}] @{n.author.get('username', '?')}: "
            f"{' '.join(n.body.split())[:250]}"
            for n in reversed(notes)
        ]
        if recent:
            parts.append("Recent comments on this "
                         f"{sit.target_type} (oldest first):\n" + "\n".join(recent))
    except Exception as e:
        log.warning("Mention context prefetch failed: %s", e)
    return "\n\n".join(parts)


def _post_session_comment(sit: Situation, body: str) -> str:
    """The agent's post_comment, under the one-thread-per-session rule:
    every comment the bot makes during a session is a reply in the session's
    discussion thread — never a new top-level comment."""
    from gitbot import gitlab_client as glc

    if not body.strip():
        return "TOOL_ERROR: empty comment body"
    if sit.session_discussion_id:
        note_id = glc.reply_to_discussion(
            sit.project_id, sit.target_type, sit.target_iid,
            sit.session_discussion_id, body)
    elif sit.target_type == "MergeRequest":
        note_id = glc.post_note_on_mr(sit.project_id, sit.target_iid, body)
    else:
        note_id = glc.post_note_on_issue(sit.project_id, sit.target_iid, body)
    return f"Comment posted in the session thread (note_id={note_id})."


# ---------------------------------------------------------------------------
# Clarifying questions — research, score, ask only above threshold
# ---------------------------------------------------------------------------

# The importance scale lives in app code (not the model) so every model
# instance ranks questions on the same rubric.
QUESTION_SCALE = """\
     - 9-10 BLOCKING: proceeding would be wrong or destructive. Required
       info is missing AND unguessable even after research — a destination
       (group/namespace/project) that matches nothing that exists, access or
       credentials you don't have, contradictory requirements, or an
       irreversible action (deletion, force-push) with an unclear target.
     - 7-8 HIGH: a wrong guess is costly to undo or visible to others
       (wrong namespace, wrong visibility on something shared), and research
       left two or more equally plausible candidates.
     - 5-6 MEDIUM: research surfaced a clearly-best candidate with residual
       doubt; a wrong guess is easy to correct later.
     - 3-4 LOW: stylistic or minor scope choices (naming details, label
       colors, README wording) where a sensible default exists.
     - 1-2 TRIVIAL: anything a reasonable teammate would just decide."""

ASKING_RULES = """\
- Asking the user ({requester}) a question:
  1. RESEARCH first — try to answer it yourself (list groups/projects, read
     related issues and comments, check existing conventions). Research
     usually lowers a question's importance and often eliminates it: a 10
     "which group?" becomes a 2 if exactly one plausible group exists.
  2. SCORE the question that remains, on this scale:
{scale}
  3. The current ask threshold is {threshold}/10. If your score is BELOW the
     threshold: do not ask. Make the best assumption, proceed, and record
     both the assumption and the unasked question (with its score) in your
     final report.
  4. If the score MEETS the threshold: post ONE consolidated comment via
     post_comment tagging {requester} with ALL your questions and exactly
     what you need, then end your final response with:
     NEEDS_INPUT
     SCORE: <your score>
     <one-line summary of what you are waiting for>
     Their reply will re-trigger you with full context.
  5. A destination that does not exist or cannot be identified is a QUESTION
     (score 9-10), never a problem to solve yourself: do NOT create groups or
     namespaces the user didn't explicitly ask for. And do not end BLOCKED
     when an answer from {requester} would unblock you — BLOCKED is only for
     tasks that no answer could make possible."""


def _asking_rules(requester: str) -> str:
    return ASKING_RULES.format(
        requester=requester,
        scale=QUESTION_SCALE,
        threshold=settings.question_threshold,
    )


# Self-handoff (github/gitbot#28): in every workflow that can create issues,
# not just orchestrate — an implement session that queues a follow-up without
# the label leaves it invisible forever.
QUEUE_RULES = """\
- If you create new issues/tasks that GitBot itself should work on LATER as
  separate tasks: assign them to {bot_username} AND add the label
  `gitbot::queued`, then do NOT work on them in this session — the harness
  picks them up on its own. Never both queue an issue and do its work
  yourself; that creates duplicate work. (Issues you create are otherwise
  invisible to the harness even if self-assigned — without the label, nothing
  will ever pick them up.)"""


def _queue_rules() -> str:
    return QUEUE_RULES.format(bot_username=settings.bot_username)


_SCORE_RE = re.compile(r"^\s*SCORE:\s*(\d{1,2})\s*$", re.M)
# Models are told to END with the sentinel but sometimes put a summary after
# it — accept NEEDS_INPUT at the start of any line, not just position 0.
_NEEDS_INPUT_RE = re.compile(r"^[ \t]*NEEDS_INPUT\b[ \t]*:?", re.M)


def _is_needs_input(final_text: str) -> bool:
    return bool(_NEEDS_INPUT_RE.search(final_text))


def _parse_needs_input(final_text: str) -> tuple[int | None, str]:
    """Strip the NEEDS_INPUT sentinel and SCORE line. Returns (score, body)."""
    body = final_text.strip()
    m = _NEEDS_INPUT_RE.search(body)
    if m:
        body = (body[:m.start()] + body[m.end():]).strip()
    m = _SCORE_RE.search(body)
    score = None
    if m:
        score = max(1, min(10, int(m.group(1))))
        body = (body[:m.start()] + body[m.end():]).strip()
    return score, body


def _needs_input_result(sit: Situation, final_text: str) -> tuple[str, str]:
    score, body = _parse_needs_input(final_text)
    sit.question_score = score
    if score is not None and score < settings.question_threshold:
        log.warning("Agent asked below threshold (score=%s < %s) on %s #%s",
                    score, settings.question_threshold, sit.target_type,
                    sit.target_iid)
    importance = f" *(importance {score}/10)*" if score else ""
    return (f":raising_hand: **GitBot needs input to continue.**{importance}\n\n"
            f"{body}\n\n"
            f"*Reply on this issue to resume the task.*"), "needs_input"


def _build_gitlab_mcp_server(project_id: int, sit: Situation | None = None):
    """Expose the existing GitLab tool set as an in-process SDK MCP server.

    post_comment is special-cased: execute_tool treats it as caller-handled
    (a stub), so the SDK engine must post it itself — threaded into the
    session discussion when one exists.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    sdk_tools = []
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]

        def make_handler(tool_name: str):
            async def handler(args: dict):
                # execute_tool is sync (python-gitlab) — keep the loop responsive
                if tool_name == "post_comment" and sit is not None:
                    try:
                        result = await asyncio.to_thread(
                            _post_session_comment, sit, str(args.get("body", "")))
                    except Exception as e:
                        result = f"TOOL_ERROR: {e}"
                else:
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


class Progress:
    """Harness-side liveness: periodically updates the placeholder comment with
    the latest activity, so the issue shows progress even if the agent never
    posts a comment itself."""

    INTERVAL = 45  # seconds between placeholder updates

    def __init__(self, sit: Situation, placeholder_id: int | None):
        import time
        self.sit = sit
        self.placeholder_id = placeholder_id
        self.started = time.monotonic()
        self.last_action = "starting up"
        self.last_action_at = time.monotonic()
        self.last_words = ""
        self.actions = 0
        self._task: asyncio.Task | None = None

    def note(self, tool_name: str, args: dict | None = None) -> None:
        import time
        self.actions += 1
        detail = ""
        if isinstance(args, dict) and args.get("project_id"):
            detail = f" (project {args['project_id']})"
        self.last_action = f"{tool_name}{detail}"
        self.last_action_at = time.monotonic()

    def say(self, text: str) -> None:
        text = " ".join(text.split())
        if text:
            self.last_words = text[:180]

    def _body(self) -> str:
        import time
        mins = int((time.monotonic() - self.started) / 60)
        idle = time.monotonic() - self.last_action_at
        if idle > 90:
            state = f"writing/reasoning (last tool `{self.last_action}` {int(idle/60)}m ago)"
        else:
            state = f"latest: `{self.last_action}`"
        body = (f":hourglass_flowing_sand: **GitBot is working...** "
                f"({mins} min, {self.actions} tool calls — {state})")
        if self.last_words:
            body += f"\n\n> {self.last_words}"
        return body

    async def _loop(self) -> None:
        from gitbot import gitlab_client as glc

        while True:
            await asyncio.sleep(self.INTERVAL)
            if not self.placeholder_id:
                continue
            try:
                if self.sit.target_type == "Issue":
                    await asyncio.to_thread(
                        glc.update_note_on_issue, self.sit.project_id,
                        self.sit.target_iid, self.placeholder_id, self._body())
                elif self.sit.target_type == "MergeRequest":
                    await asyncio.to_thread(
                        glc.update_note_on_mr, self.sit.project_id,
                        self.sit.target_iid, self.placeholder_id, self._body())
            except Exception:
                log.debug("Progress heartbeat update failed", exc_info=True)

    def start(self) -> None:
        import time
        self.started = time.monotonic()
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()


async def _drive(query_iter, progress: Progress, wf_id: str, label: str,
                 sit=None) -> str:
    """Consume the SDK message stream: feed tool calls and narration to the
    activity tracker + heartbeat as they happen, return the final text.

    When `sit` is given, the SDK session id is captured from the first message
    that carries one and persisted into the work item immediately — so even a
    crashed session leaves behind the id a resume needs (#25)."""
    from claude_agent_sdk import AssistantMessage, ResultMessage

    from gitbot.activity import tracker

    final_text = ""
    async for message in query_iter:
        sid = getattr(message, "session_id", None)
        if sid and sit is not None and sit.sdk_session_id != sid:
            sit.sdk_session_id = sid
            work_id = getattr(sit, "_work_id", None)
            if work_id:
                try:
                    from gitbot import state
                    state.update_context(work_id, {"sdk_session_id": sid})
                except Exception:  # pragma: no cover - persistence best-effort
                    log.warning("Could not persist sdk_session_id", exc_info=True)
        if isinstance(message, AssistantMessage):
            for block in getattr(message, "content", []):
                name = getattr(block, "name", None)
                if name and hasattr(block, "input"):  # tool use
                    short = name.replace("mcp__gitlab__", "")
                    args = block.input if isinstance(block.input, dict) else {}
                    progress.note(short, args)
                    if wf_id:
                        tracker.tool_called(wf_id)
                        preview = ", ".join(
                            f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])
                        tracker.log("info", f"⚙ {short}({preview})", wf_id)
                else:
                    text = getattr(block, "text", "") or ""
                    if text.strip():
                        progress.say(text)
                        if wf_id:
                            tracker.log("info", f"💬 {' '.join(text.split())[:150]}", wf_id)
        elif isinstance(message, ResultMessage):
            final_text = message.result or ""
            log.info("SDK engine: %s loop done (subtype=%s, cost=%s)",
                     label, getattr(message, "subtype", "?"),
                     getattr(message, "total_cost_usd", None))
    return final_text


def _resume_id(sit) -> str | None:
    """Session id to resume, when this run continues earlier work (#25)."""
    return sit.sdk_session_id or None if sit.is_replay else None


async def _drive_resumable(make_query, options, progress, wf_id: str,
                           label: str, sit) -> str:
    """_drive with graceful resume fallback: if resuming a prior SDK session
    fails (transcript gone — e.g. pre-#25 run, wiped volume), retry once as a
    fresh session. The snapshot/replay prompt covers the fresh path."""
    try:
        return await _drive(make_query(), progress, wf_id, label, sit)
    except Exception:
        if not getattr(options, "resume", None):
            raise
        log.warning("SDK resume of session %s failed — retrying fresh",
                    options.resume, exc_info=True)
        options.resume = None
        sit.sdk_session_id = ""
        return await _drive(make_query(), progress, wf_id, label, sit)


async def run_mention(sit: Situation, wf_id: str = "",
                      placeholder_id: int | None = None) -> str:
    """Run the mention/respond workflow as a single SDK agentic loop.

    Returns the agent's final text (posted as the reply by the caller).
    """
    from claude_agent_sdk import ClaudeAgentOptions, query

    _setup_subprocess_env()

    progress = Progress(sit, placeholder_id)

    options = ClaudeAgentOptions(
        system_prompt=MENTION_SYSTEM.format(
            target_type=sit.target_type,
            target_iid=sit.target_iid,
            target_title=sit.target_title,
            project_name=sit.project_name,
            project_id=sit.project_id,
            max_turns=MENTION_MAX_TURNS,
        ),
        model=_workflow_model("mention", sit.task_complexity),
        mcp_servers={"gitlab": _build_gitlab_mcp_server(sit.project_id, sit)},
        allowed_tools=["mcp__gitlab__*", "WebSearch", "WebFetch"],
        permission_mode="dontAsk",  # deny anything not explicitly allowed
        max_turns=MENTION_MAX_TURNS,
        setting_sources=[],  # don't load any .claude settings from disk
        cwd=settings.state_db_path.rsplit("/", 1)[0] or ".",
    )

    prompt_parts = []
    if sit.target_description:
        prompt_parts.append(
            f"{sit.target_type} #{sit.target_iid} description:\n{sit.target_description}"
        )
    conversation = await asyncio.to_thread(_mention_context, sit)
    if conversation:
        prompt_parts.append(conversation)
    prompt_parts.append(f"Comment by @{sit.actor} (answer this):\n{sit.comment_body}")
    prompt = "\n\n".join(prompt_parts)

    log.info("SDK engine: mention workflow start (%s #%s, max_turns=%d, model=%s, complexity=%s)",
             sit.target_type, sit.target_iid, MENTION_MAX_TURNS,
             options.model, sit.task_complexity)

    progress.start()
    try:
        final_text = await _drive(query(prompt=prompt, options=options),
                                  progress, wf_id, "mention")
    finally:
        progress.stop()

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
    "python", "python3", "pytest", "ruff", "make", "true", "false",
    "which", "env", "sort", "uniq", "tr", "cut", "xargs", "test", "[",
    "sleep", "date", "glab",
}

# Even for allowed interpreters, these substrings mean "installing things" —
# denied under light policy regardless of the leading command.
_LIGHT_DENY_MARKERS = (
    "pip install", "pip3 install", "apt ", "apt-get", "dnf ", "yum ",
    "docker", "podman", "sudo ", "curl ", "wget ", "nc ", "ssh ",
    "systemctl", "service ", "npm install", "npm i ", "yarn add",
    "pip download", "easy_install", "& disown", "nohup ",
)


def _strip_literals(command: str) -> str:
    """Remove quoted strings, heredoc bodies and $() bodies so the segment
    checker only sees actual command positions (a commit message containing
    'docker' must not trip the allowlist)."""
    # Heredoc bodies: from <<MARKER to the line that is exactly MARKER
    out = re.sub(
        r"<<-?\s*'?\"?(\w+)'?\"?.*?\n\1\b", "<<HEREDOC", command, flags=re.S
    )
    out = re.sub(r"\$\([^)]*\)", "SUBST", out)     # command substitutions
    out = re.sub(r"'[^']*'", "STR", out)            # single-quoted
    out = re.sub(r'"[^"]*"', "STR", out)            # double-quoted
    return out


def _light_policy_violation(command: str) -> str | None:
    """Return a denial reason if `command` violates the light shell policy."""
    stripped = _strip_literals(command)
    lowered = f" {stripped.lower()} "
    for marker in _LIGHT_DENY_MARKERS:
        if marker in lowered:
            return (f"'{marker.strip()}' is not allowed: GitBot's container is not a "
                    "devbox. Push your branch and let the project's CI/CD run "
                    "builds, installs and full test suites.")

    # Check the leading word of every pipeline/sequence segment
    for segment in re.split(r"&&|\|\||;|\||\n", stripped):
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
- If completing the issue turns out to require NO changes to repository files
  (it is only about branches/issues/labels, or the work already exists), do
  NOT invent file changes just to open an MR. Do the non-file actions with
  your GitLab tools, then start your final response with the word NO_CHANGES
  followed by your report — the harness accepts that as success.
- If the issue is impossible to implement, make no commits and explain why in
  your final response, starting it with the word BLOCKED.
{queue_rules}
{asking_rules}
"""


# MR-update mode (github/gitbot#26): a task/steer comment ON a merge request
# means "change THIS MR" — work on its existing source branch and push; never
# open a second MR.
IMPLEMENT_MR_SYSTEM = """\
You are GitBot, an AI developer inside a GitLab instance. {requester} asked
you, in a comment on merge request !{target_iid} ("{target_title}") in
project "{project_name}" (ID: {project_id}), to make changes to that MR.

A working clone of the repository is in your current directory, already
checked out on the MR's source branch `{branch_name}`.

1. Read the request and the MR context, explore the repo.
2. Make the changes with the file tools. Match the existing code style.
3. You may run existing code, tests or linters for a quick sanity check, but
   this container is NOT a devbox: do not install packages, run docker, or
   start services. The MR's CI pipeline is what builds and tests your push.
4. Commit with a clear message and push with plain `git push` — the branch's
   upstream is already set.

Rules:
- Stay on branch `{branch_name}`: do NOT create a new branch and do NOT open
  a new merge request — your pushed commits update MR !{target_iid} directly.
- The git remote is already authenticated — plain `git push` works.
- Do NOT merge or close the MR.
- Do NOT use post_comment for your final summary — your final text response is
  posted to the MR automatically.
- If the request turns out to require NO changes to repository files, do NOT
  invent changes just to push something: do any non-file actions with your
  GitLab tools, then start your final response with the word NO_CHANGES
  followed by your report.
- If the request is impossible, push nothing and explain why in your final
  response, starting it with the word BLOCKED.
{queue_rules}
{asking_rules}
"""


def _clone_repo(project_id: int, workdir: str,
                branch: str | None = None) -> tuple[str, str, str]:
    """Clone the project into workdir/repo, optionally checked out on `branch`
    (MR-update mode clones the MR's source branch directly).

    Returns (repo_path, default_branch, head_sha)."""
    import subprocess

    from gitbot import gitlab_client as glc

    gl = glc.get_client()
    project = gl.projects.get(project_id)
    default_branch = project.default_branch or "main"
    path_ns = project.path_with_namespace

    base = settings.gitlab_url.split("://", 1)[1].rstrip("/")
    url = f"https://oauth2:{settings.gitlab_token}@{base}/{path_ns}.git"

    repo_path = os.path.join(workdir, "repo")
    cmd = ["git", "clone", "--depth", "50"]
    if branch:
        cmd += ["--branch", branch]
    subprocess.run(cmd + [url, repo_path],
                   check=True, capture_output=True, timeout=120)
    bot = settings.bot_username
    for k, v in (("user.name", "GitBot"), ("user.email", f"{bot}@{base}")):
        subprocess.run(["git", "-C", repo_path, "config", k, v],
                       check=True, capture_output=True)
    head = subprocess.run(["git", "-C", repo_path, "rev-parse", "HEAD"],
                          check=True, capture_output=True, timeout=30)
    return repo_path, default_branch, head.stdout.decode().strip()


def _finalize_no_shell(repo_path: str, branch_name: str, sit: Situation,
                       default_branch: str, mr_mode: bool = False) -> str | None:
    """local_exec=none: commit, push (and for issues, branch + open the MR)
    on the agent's behalf. In MR-update mode the clone is already on the MR's
    source branch — just commit and push to it.

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
        if mr_mode:
            run("add", "-A")
            run("commit", "-m", f"GitBot: {sit.target_title} (!{sit.target_iid})")
            run("push")
            return None
        try:
            run("checkout", "-b", branch_name)
        except subprocess.CalledProcessError:
            run("checkout", branch_name)  # resumed workspace: branch exists
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


def _refresh_repo(project_id: int, repo_path: str) -> str:
    """Reused workspace (resume, #25): fetch the latest refs and return the
    project's default branch. Local partial work is left untouched — it IS
    the progress the resumed session continues from."""
    import subprocess

    from gitbot import gitlab_client as glc

    subprocess.run(["git", "-C", repo_path, "fetch", "origin"],
                   check=True, capture_output=True, timeout=120)
    project = glc.get_client().projects.get(project_id)
    return project.default_branch or "main"


def _branch_head(project_id: int, branch: str) -> str:
    """Remote head SHA of a branch (the MR-update gate's baseline)."""
    from gitbot import gitlab_client as glc

    return glc.get_client().projects.get(project_id).branches.get(branch).commit["id"]


def _verify_mr_update(project_id: int, branch: str, pre_sha: str) -> tuple[dict | None, str]:
    """Finish gate for MR-update mode: the source branch must have new commits."""
    from gitbot import gitlab_client as glc

    gl = glc.get_client()
    project = gl.projects.get(project_id)
    try:
        head = project.branches.get(branch).commit["id"]
    except Exception as e:
        return None, f"could not read branch `{branch}`: {str(e)[:120]}"
    if head == pre_sha:
        return None, f"no new commits were pushed to `{branch}`"
    try:
        cmp = project.repository_compare(pre_sha, head)
        n = len(cmp.get("commits", [])) or 1
    except Exception:
        n = 1
    return {"commits": n, "head": head[:8]}, ""


async def run_implement(sit: Situation, wf_id: str = "",
                        placeholder_id: int | None = None) -> tuple[str, bool]:
    """Run the implement workflow: clone → SDK loop → verify.

    Issue target: new branch + merge request (the classic path). MergeRequest
    target (a task/steer comment on an MR, #26): work on the MR's existing
    source branch and push — the gate checks for new commits, not a new MR.

    The workspace is a deterministic per-target dir in the data volume (#25):
    resumed runs reuse it (same cwd → the SDK finds the prior session's
    transcript; the clone with partial work is still there). It is removed
    when the task finishes; it survives parking (needs_input/waiting) and
    process crashes so the resume can continue in place.

    Returns (markdown_summary, success). The summary is posted by the caller;
    success=False means the finish gate failed or the agent reported BLOCKED.
    """
    import shutil

    workdir = _workspace_dir("impl", sit)
    reuse = bool(sit.is_replay and os.path.isdir(os.path.join(workdir, "repo")))
    if not reuse:
        shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir, exist_ok=True)
    try:
        text, ok = await _run_implement_in(
            sit, wf_id, placeholder_id, workdir, reuse)
    except BaseException:
        # In-process failure: brain marks the item failed (no auto-resume
        # follows), so don't leak the workspace. A hard process crash skips
        # this handler — the surviving workspace + persisted session id are
        # exactly what gives the crash-resume a true resume.
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    if ok in ("needs_input", "waiting"):
        return text, ok  # parked: keep workspace + session for the resume
    shutil.rmtree(workdir, ignore_errors=True)
    return text, ok


async def _run_implement_in(sit: Situation, wf_id: str,
                            placeholder_id: int | None,
                            workdir: str, reuse: bool) -> tuple[str, bool]:
    from claude_agent_sdk import ClaudeAgentOptions, query

    _setup_subprocess_env()

    progress = Progress(sit, placeholder_id)
    mr_mode = sit.target_type == "MergeRequest"
    if mr_mode:
        from gitbot import gitlab_client as glc
        try:
            mr_details = await asyncio.to_thread(
                glc.get_mr_details, sit.project_id, sit.target_iid)
        except Exception as e:
            return (f":x: **Could not read MR !{sit.target_iid}**: "
                    f"`{str(e)[:200]}`"), False
        if mr_details["state"] != "opened":
            return (f":no_entry: **MR !{sit.target_iid} is {mr_details['state']}** — "
                    "GitBot can only push changes to an open merge request."), False
        branch_name = mr_details["source_branch"]
    else:
        branch_name = f"gitbot/issue-{sit.target_iid}"

    try:
        if reuse:
            repo_path = os.path.join(workdir, "repo")
            default_branch = await asyncio.to_thread(
                _refresh_repo, sit.project_id, repo_path)
        else:
            repo_path, default_branch, _ = await asyncio.to_thread(
                _clone_repo, sit.project_id, workdir,
                branch_name if mr_mode else None
            )
        # MR gate baseline = the branch's REMOTE head as of session start.
        # (Known edge: if an interrupted run already pushed everything and the
        # resume has nothing left to push, the gate reports no new commits.)
        pre_sha = (await asyncio.to_thread(
            _branch_head, sit.project_id, branch_name) if mr_mode else "")
    except Exception as e:
        log.error("Clone failed for project %s: %s", sit.project_id, e)
        return (f":x: **Could not clone the repository**: `{str(e)[:200]}`\n\n"
                "Check that the repo is initialized and the bot has access."), False

    # Shell policy (github/gitbot#24): the container is not a devbox.
    mode = settings.local_exec
    allowed = ["Read", "Write", "Edit", "Glob", "Grep",
               "mcp__gitlab__*", "WebSearch", "WebFetch"]
    hooks = None
    system_prompt = IMPLEMENT_MR_SYSTEM if mr_mode else IMPLEMENT_SYSTEM
    if mode == "none":
        system_prompt = system_prompt.replace(
            "2. Create a branch named exactly", "2. (handled by the harness)",
        )
        system_prompt += (
            "\nNOTE: You have NO shell in this mode. Just edit the files; the "
            "harness will commit and push for you"
            + ("." if mr_mode else " and open the merge request. "
               "Do not call create_merge_request yourself.")
        )
    else:
        allowed.insert(5, "Bash")
        if mode != "full":  # light (default)
            from claude_agent_sdk import HookMatcher
            hooks = {"PreToolUse": [HookMatcher(matcher="Bash",
                                                hooks=[_bash_policy_hook()])]}

    requester = (f"@{sit.actor}" if sit.actor not in ("", "system", "unknown")
                 else "the issue author")

    options = ClaudeAgentOptions(
        system_prompt=system_prompt.format(
            target_iid=sit.target_iid,
            target_title=sit.target_title,
            project_name=sit.project_name,
            project_id=sit.project_id,
            branch_name=branch_name,
            requester=requester,
            queue_rules=_queue_rules(),
            asking_rules=_asking_rules(requester),
        ),
        model=_workflow_model("implement", sit.task_complexity),
        mcp_servers={"gitlab": _build_gitlab_mcp_server(sit.project_id, sit)},
        allowed_tools=allowed,
        permission_mode="dontAsk",
        max_turns=IMPLEMENT_MAX_TURNS,
        setting_sources=[],
        cwd=repo_path,
        hooks=hooks,
        resume=_resume_id(sit),  # true SDK resume when continuing work (#25)
    )

    if mr_mode:
        prompt = (
            f"Merge request !{sit.target_iid}: {sit.target_title}\n\n"
            f"{sit.target_description or '(no description)'}\n\n"
            f"Branch: `{branch_name}` → `{mr_details['target_branch']}`."
        )
        if sit.comment_body:
            prompt += f"\n\nThe request, from @{sit.actor}:\n{sit.comment_body}"
        if sit.is_replay:
            prompt += (
                "\n\nIMPORTANT — RESUMED TASK: earlier work on this request may "
                "already be committed or pushed on this branch. Check `git log` "
                "and `git status` and continue from the existing state instead "
                "of redoing it."
            )
    else:
        prompt = (
            f"Issue #{sit.target_iid}: {sit.target_title}\n\n"
            f"{sit.target_description or '(no description)'}\n\n"
            f"The repository default branch is `{default_branch}`."
        )
        if sit.comment_body:
            prompt += f"\n\nLatest comment from @{sit.actor}:\n{sit.comment_body}"
        if sit.is_replay:
            prompt += (
                f"\n\nIMPORTANT — RESUMED TASK: this work was interrupted and partial "
                f"progress may exist. Check whether branch `{branch_name}` already exists "
                f"(locally after fetch, or on the remote) and whether an MR is already "
                f"open; continue from the existing state instead of starting over."
            )

    log.info("SDK engine: implement workflow start (%s %s%s, branch=%s, resumed=%s, "
             "resume_session=%s, model=%s, complexity=%s)",
             "MR" if mr_mode else "Issue",
             "!" if mr_mode else "#", sit.target_iid, branch_name, sit.is_replay,
             options.resume or "-", options.model, sit.task_complexity)

    final_text = ""
    progress.start()
    try:
        final_text = await _drive_resumable(
            lambda: query(prompt=prompt, options=options),
            options, progress, wf_id, "implement", sit)

        # local_exec=none: the agent only edited files — branch/commit/push/MR
        # are the harness's job.
        if (mode == "none"
                and not final_text.strip().startswith(("BLOCKED", "NO_CHANGES"))):
            err = await asyncio.to_thread(
                _finalize_no_shell, repo_path, branch_name, sit, default_branch,
                mr_mode
            )
            if err:
                return (f":warning: **GitBot edited files but publishing failed**: "
                        f"{err}\n\nAgent's report:\n\n{final_text}"), False
    finally:
        progress.stop()

    if final_text.strip().startswith("BLOCKED"):
        return (f":no_entry: **GitBot could not implement this "
                f"{'change' if mr_mode else 'issue'}.**\n\n"
                f"{final_text.strip()[7:].strip()}"), False
    if _is_needs_input(final_text):
        return _needs_input_result(sit, final_text)

    if mr_mode:
        # Finish gate for MR-update mode: new commits on the source branch.
        upd_info, reason = await asyncio.to_thread(
            _verify_mr_update, sit.project_id, branch_name, pre_sha)
        if final_text.strip().startswith("NO_CHANGES"):
            body = final_text.strip()[len("NO_CHANGES"):].lstrip(" :\n")
            if upd_info is None:  # really pushed nothing — accept
                return (f":white_check_mark: **Done — no code changes were "
                        f"needed.**\n\n{body}"), True
            final_text = body
        if upd_info is None:
            log.warning("MR-update gate failed for MR !%s: %s", sit.target_iid, reason)
            return (f":warning: **GitBot finished but verification failed**: {reason}.\n\n"
                    f"Agent's report:\n\n{final_text}"), False
        return (f":white_check_mark: **Pushed {upd_info['commits']} "
                f"commit{'s' if upd_info['commits'] != 1 else ''} to this MR** "
                f"(`{branch_name}` @ {upd_info['head']})\n\n{final_text}"), True

    if final_text.strip().startswith("NO_CHANGES"):
        # The issue needed no file changes — accept without the MR gate, but
        # only if the agent really didn't push a branch (otherwise fall
        # through and report the MR like any other run).
        body = final_text.strip()[len("NO_CHANGES"):].lstrip(" :\n")
        mr_info, _ = await asyncio.to_thread(
            _verify_implement, sit.project_id, sit.target_iid, branch_name)
        if mr_info is None:
            return (f":white_check_mark: **Done — no code changes were needed.**\n\n"
                    f"{body}"), True
        final_text = body

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


# ---------------------------------------------------------------------------
# Orchestrate workflow (multi-project / admin / CI tasks) — no clone, no MR gate
# ---------------------------------------------------------------------------

ORCHESTRATE_MAX_TURNS = 120

ORCHESTRATE_SYSTEM = """\
You are GitBot, an AI DevOps engineer inside a GitLab instance
({gitlab_url}). You have been assigned issue #{target_iid}
("{target_title}") in project "{project_name}" (ID: {project_id}).

This is an orchestration task: it may span multiple projects, groups, CI/CD
configuration, and GitLab settings. Work through it step by step.

Capabilities:
- Your GitLab tools cover most operations (projects, issues, MRs, files,
  commits, pipelines, labels, milestones, members...).
- For anything the tools don't cover, use the `glab` CLI (installed and
  pre-authenticated against this instance) — e.g.
  `glab api projects/<id>/job_token_scope/allowlist -X POST -f target_project_id=<id>`.
  Raw REST via a short Python stdlib script is the fallback (base
  {gitlab_url}/api/v4, token in env var GITBOT_GITLAB_TOKEN as PRIVATE-TOKEN).
  Never print tokens.
- To wait for CI, use the `wait_for_pipeline` tool — it blocks until the
  pipeline finishes and returns the final status with a job summary. Do NOT
  poll with sleep loops; one wait_for_pipeline call per pipeline.

Rules:
- This container is NOT a devbox: no package installs, no docker locally, no
  services. Anything that needs a real build environment (builds, scans,
  container image builds, docker-in-docker) must run in GitLab CI/CD
  pipelines that you create in the projects themselves.
- VERIFY each step with the API or pipeline results before calling it done.
  If a step fails, investigate (job logs!) and fix it — iterate until green
  or genuinely blocked.
- Use post_comment on the issue to report progress as you complete major
  steps (short, one or two lines each). Your comments are threaded into this
  session's discussion automatically — keep the issue history clean.
{queue_rules}
- Your final text response is posted to the issue automatically. End it with
  a markdown checklist of every requested item marked ✅ done / ❌ failed
  (with reason). Be honest — the harness audits your claims.
- If the whole task is impossible, start your final response with BLOCKED.
- If you are stuck waiting on something genuinely slow (a pipeline beyond a
  wait_for_pipeline timeout, an external dependency), you may PARK the task:
  post a comment stating exactly what you are waiting for and what remains to
  be done, then start your final response with the word WAITING. The harness
  will re-run you periodically; your resumed self will see your comments and
  continue.
{asking_rules}
"""


def _resume_snapshot(sit: Situation) -> str:
    """Harness-gathered state for a resumed task: what the previous run already
    created. Injected into the prompt so the agent continues instead of
    re-planning from scratch."""
    from gitbot import gitlab_client as glc

    parts = []
    try:
        gl = glc.get_client()
        project = gl.projects.get(sit.project_id)
        ns = project.namespace["full_path"]
        group = gl.groups.get(ns)
        projects = group.projects.list(order_by="created_at", sort="desc",
                                       per_page=8, get_all=False)
        lines = [f"- {p.path_with_namespace} (id={p.id}, created {p.created_at[:16]})"
                 for p in projects if "deletion_scheduled" not in p.path]
        if lines:
            parts.append(f"Existing projects in group `{ns}` (newest first):\n" + "\n".join(lines))
    except Exception as e:
        log.warning("Resume snapshot: project listing failed: %s", e)

    try:
        gl = glc.get_client()
        issue = gl.projects.get(sit.project_id).issues.get(sit.target_iid)
        notes = [n for n in issue.notes.list(per_page=30, sort="desc")
                 if not n.system and n.author.get("username") == settings.bot_username]
        recent = [f"- [{n.created_at[11:19]}] {' '.join(n.body.split())[:200]}"
                  for n in notes[:6]]
        if recent:
            parts.append("Your own recent comments on this issue (newest first):\n"
                         + "\n".join(recent))
    except Exception as e:
        log.warning("Resume snapshot: notes fetch failed: %s", e)

    return "\n\n".join(parts) if parts else "(no prior state could be gathered)"


async def run_orchestrate(sit: Situation, wf_id: str = "",
                          placeholder_id: int | None = None) -> tuple[str, bool]:
    """Run a multi-project orchestration task as a single SDK loop.

    There is no structural gate (the task shape is arbitrary) — success is
    the loop completing with a non-BLOCKED report. The prompt demands
    per-item verification and an honest final checklist.

    The scratch workspace is a deterministic per-target dir (#25) so a parked
    or crashed session can be truly resumed (SDK sessions are stored per-cwd);
    it is removed when the task finishes and kept while parked.
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query

    _setup_subprocess_env()

    progress = Progress(sit, placeholder_id)
    workdir = _workspace_dir("orch", sit)
    os.makedirs(workdir, exist_ok=True)

    allowed = ["Read", "Write", "Edit", "Glob", "Grep",
               "mcp__gitlab__*", "WebSearch", "WebFetch"]
    hooks = None
    if settings.local_exec != "none":
        allowed.insert(5, "Bash")
        if settings.local_exec != "full":
            hooks = {"PreToolUse": [HookMatcher(matcher="Bash",
                                                hooks=[_bash_policy_hook()])]}

    requester = (f"@{sit.actor}" if sit.actor not in ("", "system", "unknown")
                 else "the issue author")

    options = ClaudeAgentOptions(
        system_prompt=ORCHESTRATE_SYSTEM.format(
            gitlab_url=settings.gitlab_url.rstrip("/"),
            target_iid=sit.target_iid,
            target_title=sit.target_title,
            project_name=sit.project_name,
            project_id=sit.project_id,
            requester=requester,
            queue_rules=_queue_rules(),
            asking_rules=_asking_rules(requester),
        ),
        model=_workflow_model("orchestrate", sit.task_complexity),
        mcp_servers={"gitlab": _build_gitlab_mcp_server(sit.project_id, sit)},
        allowed_tools=allowed,
        permission_mode="dontAsk",
        max_turns=ORCHESTRATE_MAX_TURNS,
        setting_sources=[],
        cwd=workdir,
        hooks=hooks,
        resume=_resume_id(sit),  # true SDK resume when continuing work (#25)
    )

    prompt = (
        f"Issue #{sit.target_iid}: {sit.target_title}\n\n"
        f"{sit.target_description or '(no description)'}"
    )
    if sit.comment_body:
        prompt += f"\n\nLatest comment from @{sit.actor}:\n{sit.comment_body}"
    if sit.is_replay:
        snapshot = await asyncio.to_thread(_resume_snapshot, sit)
        prompt = (
            "⚠️ RESUMED TASK — READ THIS FIRST. A previous run of this exact task "
            "was interrupted mid-flight. The state snapshot below shows what it "
            "already created. You MUST continue from this state: adopt the existing "
            "projects/branches/pipelines as your own work, verify what stage they "
            "reached, and proceed with the remaining steps. Creating duplicate "
            "projects is a failure.\n\n"
            f"<state_snapshot>\n{snapshot}\n</state_snapshot>\n\n"
            "--- Original task below ---\n\n" + prompt
        )

    log.info("SDK engine: orchestrate workflow start (Issue #%s, max_turns=%d, resumed=%s, "
             "resume_session=%s, model=%s, complexity=%s)",
             sit.target_iid, ORCHESTRATE_MAX_TURNS, sit.is_replay,
             options.resume or "-", options.model, sit.task_complexity)

    progress.start()
    try:
        final_text = await _drive_resumable(
            lambda: query(prompt=prompt, options=options),
            options, progress, wf_id, "orchestrate", sit)
    finally:
        progress.stop()

    if not final_text:
        result = (":warning: **GitBot produced no report** "
                  "(loop ended without a result).", False)
    elif final_text.strip().startswith("BLOCKED"):
        result = (f":no_entry: **GitBot could not complete this task.**\n\n"
                  f"{final_text.strip()[7:].strip()}"), False
    elif final_text.strip().startswith("WAITING"):
        result = (f":double_vertical_bar: **Task parked — waiting on something slow.**\n\n"
                  f"{final_text.strip()[7:].strip()}\n\n"
                  f"*GitBot will check back periodically and resume.*"), "waiting"
    elif _is_needs_input(final_text):
        result = _needs_input_result(sit, final_text)
    else:
        result = final_text, True

    if result[1] not in ("waiting", "needs_input"):
        # Finished (either way): scratch is disposable. Parked keeps it so the
        # resumed session finds its cwd (and any scratch files) intact.
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    return result


# ---------------------------------------------------------------------------
# Review workflow (MR review requested → inline findings + verdict) — github/gitbot#22
# ---------------------------------------------------------------------------

REVIEW_MAX_TURNS = 50

# In app code so every model instance marks findings the same way (same
# reasoning as QUESTION_SCALE / COMPLEXITY_SCALE).
REVIEW_SEVERITIES = """\
Severity scale (use these markers exactly, both inline and in the summary):
- 🔴 **critical** — bugs, data loss, security holes, broken behavior. Blocks merge.
- 🟠 **major** — likely bugs, missing error handling, races, API misuse. Should fix before merge.
- 🟡 **minor** — style inconsistency, naming, missing tests, small refactors. Worth fixing.
- 🔵 **nit** — taste-level suggestion. The author may ignore freely.\
"""

REVIEW_SYSTEM = """\
You are GitBot, an AI code reviewer inside a GitLab instance. You have been
asked to review merge request !{mr_iid} ("{mr_title}") in project
"{project_name}" (ID: {project_id}).

{checkout_note} The full MR diff is in the task prompt.

How to review:
1. Read the diff, then judge the changes in context: read the surrounding
   code, callers, and tests with the file tools (git log/blame may help).
   Review the CHANGE, not the whole repository.
2. Verify claims before asserting them — if you think "X is never called" or
   "this breaks Y", grep for it first. Wrong review comments destroy trust.
3. Post each concrete finding as an inline comment with the
   `post_inline_comment` tool, on the exact line, starting with its severity
   marker. Only lines that appear in the diff are valid anchors; if the right
   line is not in the diff, put the finding in the summary instead.

{severities}

Rules:
- You are a reviewer: do NOT modify files, push, merge, approve, close, or
  edit the MR. Comments only.
- A clean MR deserves a short approval, not invented nitpicks.
- Do NOT use post_comment for your summary — your final text response is
  posted to the MR automatically.
- Your final response is the review summary: a 2-4 sentence assessment, then
  your findings with severity markers (matching the inline comments), then
  end with exactly one line:
  VERDICT: approve            (nothing 🔴/🟠 found)
  VERDICT: request_changes    (at least one 🔴/🟠 finding)
- If the MR cannot be reviewed at all (empty diff, unreadable repo), start
  your final response with BLOCKED and say why.
"""

_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(approve|request_changes)\s*$", re.M | re.I)

REVIEW_DIFF_CAP = 50_000  # chars of diff inlined in the prompt


def _checkout_mr_head(project_id: int, mr_iid: int, workdir: str) -> str:
    """Clone the repo and check out the MR's head commit (works for fork MRs
    too via the refs/merge-requests/ ref). Returns the repo path."""
    import subprocess

    repo_path, _, _ = _clone_repo(project_id, workdir)
    subprocess.run(
        ["git", "-C", repo_path, "fetch", "--depth", "50", "origin",
         f"refs/merge-requests/{mr_iid}/head"],
        check=True, capture_output=True, timeout=120,
    )
    subprocess.run(
        ["git", "-C", repo_path, "checkout", "-b", f"mr-{mr_iid}", "FETCH_HEAD"],
        check=True, capture_output=True, timeout=60,
    )
    return repo_path


async def run_review(sit: Situation, wf_id: str = "",
                     placeholder_id: int | None = None) -> tuple[str, bool]:
    """Review an MR: checkout head → SDK loop → inline findings + verdict.

    Returns (markdown_summary, success). There is no structural gate beyond a
    non-empty report — the deliverable IS the report (plus any inline
    comments the agent posted along the way).
    """
    import shutil
    import tempfile

    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query

    from gitbot import gitlab_client as glc

    _setup_subprocess_env()

    progress = Progress(sit, placeholder_id)
    workdir = tempfile.mkdtemp(prefix=f"gitbot-review-{sit.project_id}-{sit.target_iid}-")

    checkout_note = ("A working clone of the repository, checked out at the "
                     "MR's head commit, is in your current directory.")
    try:
        repo_path = await asyncio.to_thread(
            _checkout_mr_head, sit.project_id, sit.target_iid, workdir)
    except Exception as e:
        # Degraded mode: review from the API diff + read_file tools only.
        log.warning("Review checkout failed for MR !%s: %s", sit.target_iid, e)
        repo_path = workdir
        checkout_note = ("No local checkout is available — use the read_file "
                         "and get_mr_diff GitLab tools to inspect code.")

    try:
        diff = await asyncio.to_thread(glc.get_mr_diff, sit.project_id, sit.target_iid)
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return (f":x: **Could not fetch the MR diff**: `{str(e)[:200]}`", False)
    if not diff.strip():
        shutil.rmtree(workdir, ignore_errors=True)
        return (":no_entry: **Nothing to review** — the merge request has an empty diff.", False)
    if len(diff) > REVIEW_DIFF_CAP:
        diff = diff[:REVIEW_DIFF_CAP] + "\n... (diff truncated — use the get_mr_diff tool or the local checkout for the rest)"

    try:
        details = await asyncio.to_thread(glc.get_mr_details, sit.project_id, sit.target_iid)
    except Exception:
        details = {}

    allowed = ["Read", "Glob", "Grep", "mcp__gitlab__*", "WebSearch", "WebFetch"]
    hooks = None
    if settings.local_exec != "none":
        allowed.insert(3, "Bash")
        if settings.local_exec != "full":
            hooks = {"PreToolUse": [HookMatcher(matcher="Bash",
                                                hooks=[_bash_policy_hook()])]}

    options = ClaudeAgentOptions(
        system_prompt=REVIEW_SYSTEM.format(
            mr_iid=sit.target_iid,
            mr_title=sit.target_title,
            project_name=sit.project_name,
            project_id=sit.project_id,
            checkout_note=checkout_note,
            severities=REVIEW_SEVERITIES,
        ),
        model=_workflow_model("review", sit.task_complexity),
        mcp_servers={"gitlab": _build_gitlab_mcp_server(sit.project_id, sit)},
        allowed_tools=allowed,
        permission_mode="dontAsk",
        max_turns=REVIEW_MAX_TURNS,
        setting_sources=[],
        cwd=repo_path,
        hooks=hooks,
    )

    author = details.get("author") or "?"
    prompt = (
        f"Review MR !{sit.target_iid}: {sit.target_title}\n"
        f"Author: @{author} | Branch: {details.get('source_branch', '?')} → "
        f"{details.get('target_branch', '?')}\n\n"
        f"{sit.target_description or '(no description)'}\n\n"
        f"<diff>\n{diff}\n</diff>"
    )
    if sit.comment_body:
        prompt += f"\n\nLatest comment from @{sit.actor}:\n{sit.comment_body}"

    log.info("SDK engine: review workflow start (MR !%s, max_turns=%d, model=%s, complexity=%s)",
             sit.target_iid, REVIEW_MAX_TURNS, options.model, sit.task_complexity)

    progress.start()
    try:
        final_text = await _drive(query(prompt=prompt, options=options),
                                  progress, wf_id, "review")
    finally:
        progress.stop()
        shutil.rmtree(workdir, ignore_errors=True)

    if not final_text:
        return ":warning: **GitBot produced no review** (loop ended without a result).", False
    if final_text.strip().startswith("BLOCKED"):
        return (f":no_entry: **GitBot could not review this MR.**\n\n"
                f"{final_text.strip()[7:].strip()}"), False

    m = _VERDICT_RE.search(final_text)
    verdict = m.group(1).lower() if m else None
    if verdict == "approve":
        header = ":white_check_mark: **Review complete — looks good.**"
    elif verdict == "request_changes":
        header = ":warning: **Review complete — changes requested.**"
    else:
        log.warning("Review for MR !%s ended without a VERDICT line", sit.target_iid)
        header = ":mag: **Review complete.**"
    return f"{header}\n\n{final_text}", True


# ---------------------------------------------------------------------------
# Assigned-issue triage: single-repo code change vs orchestration
# ---------------------------------------------------------------------------

async def _classify_complete(system: str, prompt: str) -> str:
    """One-shot Haiku completion for the triage classifiers — the only LLM
    calls outside the Agent SDK loops."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    msg = await client.messages.create(
        model=settings.classifier_model,
        max_tokens=32,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


CLASSIFY_COMMENT_PROMPT = """\
A user commented on a GitLab issue where a bot is participating. Decide how
the bot should treat the comment:

- "answer": a question or side request that just needs a reply — information,
  a report, an explanation, something not requiring changes to repositories,
  projects or the issue's actual task.
- "steer": feedback or direction on work the bot already did or is doing for
  THIS issue's task — corrections ("that's wrong, fix it"), adjustments
  ("use X instead of Y"), additions ("also cover Z"), or "continue/retry".
  The bot should pick the task back up, adopting its prior work.
- "task": a NEW request to do work — implement/fix/configure something the
  issue describes but the bot hasn't started, or a fresh ask requiring
  commits, pipelines or project changes.

Issue title: {title}
Comment by @{actor}:
{comment}

{complexity_scale}

Respond with exactly: <answer|steer|task> <complexity>
Example: answer 2"""


async def classify_comment(sit: Situation) -> str:
    """Cheap triage for comment callouts: 'answer' (just reply, no labels, no
    takeover), 'steer' (resume the issue's work incorporating the comment) or
    'task' (treat like being freshly assigned the work)."""
    try:
        raw = await _classify_complete(
            system="You are a precise classifier. Answer in the exact format requested.",
            prompt=CLASSIFY_COMMENT_PROMPT.format(
                title=sit.target_title,
                actor=sit.actor,
                comment=(sit.comment_body or "")[:2000],
                complexity_scale=COMPLEXITY_SCALE,
            ),
        )
        word, complexity = _parse_classification(
            raw, {"answer", "steer", "task"}, "answer")
        sit.task_complexity = complexity
        return word
    except Exception as e:
        log.warning("Comment classification failed (%s) — defaulting to answer", e)
    return "answer"


CLASSIFY_PROMPT = """\
Classify this GitLab issue assignment for a bot that has two workflows:

- "implement": a change to FILES inside THIS repository — write/modify code
  or docs here, open a merge request.
- "orchestrate": anything else — creating/configuring projects or groups,
  CI/CD setup across projects, GitLab settings, security scanning, registry,
  Pages, multi-step admin tasks, or work spanning multiple repositories.
  Managing branches, issues, labels, milestones or members WITHOUT changing
  repository files is orchestrate, not implement.

Issue title: {title}
Issue description:
{description}

{complexity_scale}

Respond with exactly: <implement|orchestrate> <complexity>
Example: orchestrate 7"""


async def classify_assigned_issue(sit: Situation) -> str:
    """Cheap triage: 'implement' or 'orchestrate' (+ complexity score,
    stashed on sit.task_complexity for auto model selection)."""
    try:
        raw = await _classify_complete(
            system="You are a precise classifier. Answer in the exact format requested.",
            prompt=CLASSIFY_PROMPT.format(
                title=sit.target_title,
                description=(sit.target_description or "")[:3000],
                complexity_scale=COMPLEXITY_SCALE,
            ),
        )
        word, complexity = _parse_classification(
            raw, {"implement", "orchestrate"}, "implement")
        sit.task_complexity = complexity
        return word
    except Exception as e:
        log.warning("Issue classification failed (%s) — defaulting to implement", e)
    return "implement"
