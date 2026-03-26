"""Prompt templates per (task, model_family).

Claude works best with XML structure and detailed instructions.
Open models work better with shorter, direct prompts.
OpenAI is in between.
"""

from gitbot.models import Family, Task


def _system(family: Family) -> str:
    """Base system prompt, tuned per family."""
    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        return """\
You are GitBot, an AI assistant embedded in a GitLab team.
You communicate exclusively through GitLab comments — be concise and professional.
Use markdown formatting. When reviewing code, be thorough but not pedantic.
If you need more context, ask specific questions rather than guessing.

<guidelines>
- Lead with the most important finding or answer
- Use code blocks with language hints for any code snippets
- Keep responses under 500 words unless the task demands more
- For reviews: categorize findings as 🔴 bug, 🟡 suggestion, or 🟢 nitpick
</guidelines>"""

    if family == Family.OPENAI:
        return """\
You are GitBot, an AI assistant on a GitLab team.
Communicate through GitLab comments. Be concise and professional. Use markdown.
When reviewing code: categorize as 🔴 bug, 🟡 suggestion, or 🟢 nitpick.
Ask for clarification rather than guessing."""

    # Ollama / open models — keep it short, they drift on long system prompts
    return """\
You are GitBot, an AI code assistant. Respond in markdown. Be concise.
For code reviews: mark issues as 🔴 bug, 🟡 suggestion, or 🟢 nitpick."""


# --- Task prompt templates ---
# Each returns (system, user_prompt) for the given family.
# Variables are passed as a dict and formatted into the template.


def issue_analysis(family: Family, *, title: str, description: str) -> tuple[str, str]:
    system = _system(family)

    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        user = f"""\
<task>You have been assigned to this GitLab issue.</task>

<issue>
<title>{title}</title>
<description>
{description}
</description>
</issue>

<instructions>
Analyze this issue and respond with:
1. Your understanding of what's being asked
2. A proposed approach or plan (if enough info)
3. Specific clarifying questions (if the issue is ambiguous)
</instructions>"""
    else:
        user = f"""\
You've been assigned to this issue.

**{title}**

{description}

---
Analyze this and either propose a plan or ask clarifying questions."""

    return system, user


def mr_summary(family: Family, *, title: str, description: str, diff: str) -> tuple[str, str]:
    system = _system(family)

    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        user = f"""\
<task>You have been assigned to this merge request.</task>

<merge_request>
<title>{title}</title>
<description>{description}</description>
<diff>
{diff}
</diff>
</merge_request>

<instructions>
Summarize what this MR does and flag any concerns.
</instructions>"""
    else:
        user = f"""\
You're assigned to this MR.

**{title}**: {description}

```diff
{diff}
```

Summarize the changes and flag any concerns."""

    return system, user


def code_review(family: Family, *, title: str, description: str, diff: str) -> tuple[str, str]:
    system = _system(family)

    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        user = f"""\
<task>Perform a thorough code review of this merge request.</task>

<merge_request>
<title>{title}</title>
<description>{description}</description>
<diff>
{diff}
</diff>
</merge_request>

<review_checklist>
- Bugs and logic errors
- Security vulnerabilities (injection, auth bypass, data exposure)
- Performance issues (N+1 queries, unbounded loops, memory leaks)
- Error handling gaps
- Code clarity and maintainability
- Race conditions or concurrency issues
</review_checklist>

<format>
Group findings by file. Use 🔴 🟡 🟢 severity markers.
If the code looks good, say so briefly — don't invent problems.
</format>"""
    else:
        user = f"""\
Review this MR carefully.

**{title}**: {description}

```diff
{diff}
```

Check for: bugs, security issues, performance, error handling, clarity.
Use 🔴 bug / 🟡 suggestion / 🟢 nitpick. Group by file."""

    return system, user


def mention_response(
    family: Family, *, note_body: str, noteable_type: str, noteable_title: str
) -> tuple[str, str]:
    system = _system(family)

    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        user = f"""\
<task>Someone mentioned you in a {noteable_type} comment. Respond helpfully.</task>

<context>
<noteable type="{noteable_type}" title="{noteable_title}" />
<comment>{note_body}</comment>
</context>

<instructions>
Respond to what was asked. If you need more context to give a good answer, ask.
</instructions>"""
    else:
        user = f"""\
You were mentioned in a {noteable_type} ({noteable_title}):

> {note_body}

Respond helpfully. Ask for context if needed."""

    return system, user


def implement(
    family: Family, *, title: str, description: str, repo_tree: str, default_branch: str
) -> tuple[str, str]:
    system = """\
You are GitBot, an AI developer embedded in a GitLab team.
You implement code by creating files directly in the repository.
You have full access to create branches, commit files, and open merge requests.

You MUST respond with valid JSON only — no markdown, no explanation outside the JSON.
"""

    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        user = f"""\
<task>Implement the following GitLab issue by writing the code.</task>

<issue>
<title>{title}</title>
<description>
{description}
</description>
</issue>

<repository>
<default_branch>{default_branch}</default_branch>
<tree>
{repo_tree}
</tree>
</repository>

<instructions>
You are working inside a GitLab repository. Implement the requested changes by producing
the files that need to be created or modified.

Respond with a JSON object in this exact format:
{{
  "branch_name": "feature/short-descriptive-name",
  "commit_message": "Add feature X as described in issue",
  "mr_title": "Short MR title",
  "mr_description": "Description of what was implemented and why",
  "files": [
    {{
      "action": "create",
      "file_path": "path/to/file.ext",
      "content": "full file content here"
    }}
  ]
}}

Rules:
- "action" must be "create" for new files or "update" for existing files
- File paths must be relative to the repo root
- Include ALL files needed for a complete, working implementation
- Write production-quality code, not stubs or placeholders
- Respond ONLY with the JSON object, no other text
</instructions>"""
    else:
        user = f"""\
Implement this GitLab issue by writing code.

**{title}**

{description}

**Repo files (default branch: {default_branch}):**
{repo_tree}

Respond with ONLY a JSON object:
{{
  "branch_name": "feature/short-name",
  "commit_message": "Description of changes",
  "mr_title": "Short MR title",
  "mr_description": "What was implemented",
  "files": [
    {{"action": "create", "file_path": "path/to/file", "content": "full content"}}
  ]
}}

action: "create" for new files, "update" for existing. Write complete code, not stubs."""

    return system, user


def triage(
    family: Family,
    *,
    title: str,
    description: str,
    target_type: str,
    assigner: str,
    existing_mrs: str,
    recent_comments: str,
    repo_tree: str,
) -> tuple[str, str]:
    system = """\
You are GitBot, an AI developer on a GitLab team. You've been assigned a task.
Before acting, you must decide the best course of action.

You MUST respond with valid JSON only — no markdown, no extra text."""

    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        user = f"""\
<task>You've been assigned to this {target_type}. Decide what to do.</task>

<context>
<assigned_by>{assigner}</assigned_by>
<title>{title}</title>
<description>
{description}
</description>
<existing_merge_requests>
{existing_mrs}
</existing_merge_requests>
<recent_comments>
{recent_comments}
</recent_comments>
<repository_files>
{repo_tree}
</repository_files>
</context>

<instructions>
Decide ONE of these actions:

1. **"implement"** — You have enough information to write code. The issue is clear,
   there's no conflicting MR, and you know what to build.

2. **"ask"** — Something is ambiguous or you need a decision from the team.
   Examples: an MR already exists and you're not sure whether to update it or
   create a new one; the issue is vague; there are conflicting requirements;
   you need to know which framework/approach to use.

3. **"discuss"** — This isn't a code task, or it needs analysis/planning first.
   Example: architecture questions, investigating a bug, proposing options.

Respond with JSON:
{{
  "action": "implement" | "ask" | "discuss",
  "reasoning": "Brief explanation of why you chose this action",
  "question": "The question to ask (only if action=ask)",
  "mention": "@username to mention (only if action=ask, usually the assigner)"
}}
</instructions>"""
    else:
        user = f"""\
You're assigned to this {target_type}.

**{title}**
{description}

Existing MRs: {existing_mrs}
Recent comments: {recent_comments}
Repo files: {repo_tree}
Assigned by: {assigner}

Decide: "implement" (clear task, write code), "ask" (need clarification), or "discuss" (analyze/plan).

JSON only:
{{"action": "implement"|"ask"|"discuss", "reasoning": "why", "question": "if asking", "mention": "@user if asking"}}"""

    return system, user


def followup_response(
    family: Family,
    *,
    original_question: str,
    user_reply: str,
    workflow: str,
    context: str,
) -> tuple[str, str]:
    system = """\
You are GitBot, an AI developer on a GitLab team.
You previously asked a question and received a response.
Decide how to proceed based on the answer.

You MUST respond with valid JSON only."""

    user = f"""\
<context>
<workflow>{workflow}</workflow>
<your_question>{original_question}</your_question>
<their_reply>{user_reply}</their_reply>
<additional_context>{context}</additional_context>
</context>

Based on their reply, decide what to do next.

Respond with JSON:
{{
  "action": "implement" | "ask" | "discuss",
  "reasoning": "What you understood from their reply and what you'll do",
  "question": "Follow-up question if action=ask",
  "mention": "@user if action=ask",
  "implementation_notes": "Specific guidance for implementation based on their answer (if action=implement)"
}}"""

    return system, user


def mr_change_request(
    family: Family, *, mr_title: str, request: str, current_diff: str, repo_tree: str, branch: str
) -> tuple[str, str]:
    system = """\
You are GitBot, an AI developer. You authored a merge request and someone
has requested changes. You must push new commits to the existing branch.

You MUST respond with valid JSON only — no markdown, no extra text."""

    if family in (Family.ANTHROPIC, Family.CLAUDE_CODE):
        user = f"""\
<task>Someone requested changes on your merge request. Push a new commit.</task>

<merge_request>
<title>{mr_title}</title>
<branch>{branch}</branch>
<current_diff>
{current_diff}
</current_diff>
</merge_request>

<request>{request}</request>

<repository_files>
{repo_tree}
</repository_files>

<instructions>
Respond with a JSON object describing the commit to push to branch `{branch}`:
{{
  "commit_message": "Description of the changes",
  "files": [
    {{
      "action": "create",
      "file_path": "path/to/file",
      "content": "full file content"
    }}
  ]
}}

Rules:
- "action": "create" for new files, "update" for modifying existing files on the branch
- For "update", include the COMPLETE new file content, not a partial patch
- Only include files that need to change — don't re-commit unchanged files
- Write production-quality code
- JSON only, no other text
</instructions>"""
    else:
        user = f"""\
Someone requested changes on your MR "{mr_title}" (branch: {branch}).

Request: {request}

Current diff:
```diff
{current_diff}
```

Repo files: {repo_tree}

Push a commit. JSON only:
{{"commit_message": "...", "files": [{{"action": "create|update", "file_path": "...", "content": "..."}}]}}"""

    return system, user


def clarify(family: Family, *, context: str, what_is_unclear: str) -> tuple[str, str]:
    system = _system(family)
    user = f"""\
Context: {context}

You need more information. Specifically: {what_is_unclear}

Write a concise GitLab comment asking for the missing information."""

    return system, user


# Registry for easy lookup
TEMPLATES = {
    Task.ISSUE_ANALYSIS: issue_analysis,
    Task.MR_SUMMARY: mr_summary,
    Task.CODE_REVIEW: code_review,
    Task.MENTION_RESPONSE: mention_response,
    Task.CLARIFY: clarify,
    Task.IMPLEMENT: implement,
    Task.TRIAGE: triage,
}
