"""GitLab tool definitions for LLM tool_use.

Each tool has:
- A schema (for the LLM to understand parameters)
- An execute function (maps tool call to GitLab API)

Tools are provider-agnostic — litellm normalizes tool_use across
Anthropic, OpenAI, and Ollama.
"""

import json
import logging
from gitbot import gitlab_client as glc
from gitbot.config import settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool categories — used to filter tools per step
# ---------------------------------------------------------------------------

TOOL_CATEGORIES: dict[str, list[str]] = {
    "comments": ["post_comment", "update_comment"],
    "issues": ["create_issue", "update_issue", "search_issues", "link_issues"],
    "code": ["create_branch", "commit_files", "read_file", "list_files",
             "create_merge_request", "get_mr_diff"],
    "planning": ["create_milestone", "list_milestones", "assign_milestone",
                  "create_label", "create_wiki_page"],
    "ci": ["list_pipelines", "get_pipeline", "list_pipeline_jobs",
            "get_job_log", "retry_pipeline", "run_pipeline"],
    "epics": ["create_epic", "list_epics", "add_issue_to_epic"],
    "iterations": ["create_iteration_cadence", "create_iteration",
                    "list_iterations", "assign_iteration"],
    "admin": ["create_project", "get_project_info", "create_group",
              "list_groups", "list_members", "add_member",
              "list_vulnerabilities"],
}

# Reverse lookup: tool_name → category
_TOOL_TO_CATEGORY: dict[str, str] = {}
for _cat, _names in TOOL_CATEGORIES.items():
    for _name in _names:
        _TOOL_TO_CATEGORY[_name] = _cat


def get_tools_for_step(tools_needed: list[str] | None) -> list[dict]:
    """Filter TOOL_SCHEMAS to only include tools relevant to a step.

    tools_needed can contain tool names or category names.
    Always includes 'comments' category (every step can post comments).
    Returns all tools if tools_needed is None/empty.
    """
    if not tools_needed:
        return TOOL_SCHEMAS

    # Expand category names to tool names
    allowed_tools: set[str] = set()
    for name in tools_needed:
        if name in TOOL_CATEGORIES:
            allowed_tools.update(TOOL_CATEGORIES[name])
        else:
            allowed_tools.add(name)

    # Always include comments
    allowed_tools.update(TOOL_CATEGORIES["comments"])

    filtered = [t for t in TOOL_SCHEMAS if t["function"]["name"] in allowed_tools]
    # If we only matched the always-included comments, the plan's tool names
    # didn't match anything real — fall back to all tools
    comment_names = set(TOOL_CATEGORIES["comments"])
    matched_non_comment = any(
        t["function"]["name"] not in comment_names for t in filtered
    )
    return filtered if matched_non_comment else TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Tool schemas — sent to the LLM with each request
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    # --- Comments ---
    {
        "type": "function",
        "function": {
            "name": "post_comment",
            "description": "Post a comment on the current issue or merge request. Use for responses, updates, questions, or status messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {"type": "string", "description": "The comment body (markdown supported)"},
                },
                "required": ["body"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "update_comment",
            "description": "Edit an existing comment by note ID. Useful for updating progress checklists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer", "description": "The ID of the note to edit"},
                    "target_type": {"type": "string", "enum": ["issue", "merge_request"]},
                    "target_iid": {"type": "integer"},
                    "body": {"type": "string", "description": "New comment body (replaces entire content)"},
                },
                "required": ["note_id", "target_type", "target_iid", "body"],
            },
        },
    },

    # --- Issues ---
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": "Create a new issue in a project. Defaults to the current project if project_id is omitted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Target project ID. Omit to use the current project."},
                    "title": {"type": "string"},
                    "description": {"type": "string", "description": "Issue description (markdown)"},
                    "labels": {"type": "string", "description": "Comma-separated label names"},
                    "assignee_username": {"type": "string", "description": "Username to assign to"},
                    "milestone_id": {"type": "integer", "description": "Milestone ID to assign"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_issue",
            "description": "Update an existing issue (title, description, labels, state, assignee, milestone).",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_iid": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "labels": {"type": "string", "description": "Comma-separated labels (replaces all)"},
                    "state_event": {"type": "string", "enum": ["close", "reopen"]},
                    "assignee_username": {"type": "string"},
                    "milestone_id": {"type": "integer"},
                },
                "required": ["issue_iid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_issues",
            "description": "Search for issues in the current project by keyword, label, state, or milestone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search keyword"},
                    "labels": {"type": "string", "description": "Comma-separated label filter"},
                    "state": {"type": "string", "enum": ["opened", "closed", "all"]},
                    "milestone": {"type": "string", "description": "Milestone title"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_issues",
            "description": "Create a link between two issues (relates, blocks, or is_blocked_by).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_issue_iid": {"type": "integer"},
                    "target_issue_iid": {"type": "integer"},
                    "link_type": {"type": "string", "enum": ["relates_to", "blocks", "is_blocked_by"]},
                },
                "required": ["source_issue_iid", "target_issue_iid"],
            },
        },
    },

    # --- Branches & Files ---
    {
        "type": "function",
        "function": {
            "name": "create_branch",
            "description": "Create a new git branch from the default branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string"},
                    "ref": {"type": "string", "description": "Source branch (defaults to project default)"},
                },
                "required": ["branch_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_files",
            "description": "Create a commit with one or more file changes on a branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "description": "Target branch"},
                    "commit_message": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["create", "update", "delete"]},
                                "file_path": {"type": "string"},
                                "content": {"type": "string", "description": "File content (for create/update)"},
                            },
                            "required": ["action", "file_path"],
                        },
                    },
                },
                "required": ["branch", "commit_message", "files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file from the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "ref": {"type": "string", "description": "Branch or tag (defaults to project default)"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (empty for root)"},
                    "ref": {"type": "string", "description": "Branch or tag"},
                    "recursive": {"type": "boolean", "description": "List recursively"},
                },
            },
        },
    },

    # --- Merge Requests ---
    {
        "type": "function",
        "function": {
            "name": "create_merge_request",
            "description": "Create a new merge request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_branch": {"type": "string"},
                    "target_branch": {"type": "string", "description": "Defaults to project default branch"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["source_branch", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_mr_diff",
            "description": "Get the diff of a merge request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mr_iid": {"type": "integer"},
                },
                "required": ["mr_iid"],
            },
        },
    },

    # --- Planning ---
    {
        "type": "function",
        "function": {
            "name": "create_milestone",
            "description": "Create a milestone. Can be project-level (default) or group-level if group_path is provided.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "group_path": {"type": "string", "description": "Create as a group milestone instead of project milestone"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_label",
            "description": "Create a project label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "color": {"type": "string", "description": "Hex color like #FF0000"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },

    # --- Wiki ---
    {
        "type": "function",
        "function": {
            "name": "create_wiki_page",
            "description": "Create a wiki page in the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "Page content (markdown)"},
                },
                "required": ["title", "content"],
            },
        },
    },

    # --- Pipelines ---
    {
        "type": "function",
        "function": {
            "name": "list_pipelines",
            "description": "List recent CI/CD pipelines for the project, optionally filtered by status, ref, or source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["running", "pending", "success", "failed", "canceled", "skipped", "created", "manual"]},
                    "ref": {"type": "string", "description": "Branch or tag name"},
                    "per_page": {"type": "integer", "description": "Number of results (default 10, max 100)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pipeline",
            "description": "Get details of a specific pipeline by ID, including its status and duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "integer"},
                },
                "required": ["pipeline_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pipeline_jobs",
            "description": "List jobs in a pipeline. Shows job name, stage, status, and duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "integer"},
                },
                "required": ["pipeline_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_log",
            "description": "Get the log/trace output of a specific CI/CD job. Useful for diagnosing failures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_pipeline",
            "description": "Retry a failed pipeline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_id": {"type": "integer"},
                },
                "required": ["pipeline_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_pipeline",
            "description": "Trigger a new pipeline on a specific branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Branch or tag to run the pipeline on"},
                    "variables": {
                        "type": "array",
                        "description": "Pipeline variables",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["key", "value"],
                        },
                    },
                },
                "required": ["ref"],
            },
        },
    },

    # --- Epics (Group-level) ---
    {
        "type": "function",
        "function": {
            "name": "create_epic",
            "description": "Create an epic in the project's parent group. Epics are group-level and contain issues across projects.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_path": {"type": "string", "description": "Group path (e.g. 'gbtest'). Required."},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "labels": {"type": "string", "description": "Comma-separated labels"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["group_path", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_epics",
            "description": "List epics in a group.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_path": {"type": "string"},
                    "state": {"type": "string", "enum": ["opened", "closed", "all"]},
                    "search": {"type": "string"},
                },
                "required": ["group_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_issue_to_epic",
            "description": "Add an issue to an epic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_path": {"type": "string"},
                    "epic_iid": {"type": "integer"},
                    "issue_id": {"type": "integer", "description": "The global issue ID (not iid). Use search_issues to find it."},
                },
                "required": ["group_path", "epic_iid", "issue_id"],
            },
        },
    },

    # --- Milestones (expanded) ---
    {
        "type": "function",
        "function": {
            "name": "list_milestones",
            "description": "List project milestones.",
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": ["active", "closed"]},
                    "search": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_milestone",
            "description": "Assign a milestone to an issue or merge request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_type": {"type": "string", "enum": ["issue", "merge_request"]},
                    "target_iid": {"type": "integer"},
                    "milestone_id": {"type": "integer"},
                },
                "required": ["target_type", "target_iid", "milestone_id"],
            },
        },
    },

    # --- Iterations ---
    {
        "type": "function",
        "function": {
            "name": "create_iteration_cadence",
            "description": "Create an iteration cadence (sprint schedule) in a group. This is required before iterations can be created.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_path": {"type": "string"},
                    "title": {"type": "string", "description": "Cadence name, e.g. 'Development Sprints'"},
                    "duration_in_weeks": {"type": "integer", "description": "Sprint duration: 1, 2, 3, or 4 weeks"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD — when first iteration starts"},
                    "iterations_in_advance": {"type": "integer", "description": "How many future iterations to auto-create (default 2)"},
                    "automatic": {"type": "boolean", "description": "Auto-create iterations (default true)"},
                },
                "required": ["group_path", "title", "start_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_iteration",
            "description": "Create a single iteration (sprint) within a group. Requires an iteration cadence to exist first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_path": {"type": "string"},
                    "title": {"type": "string"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["group_path", "title", "start_date", "due_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_iterations",
            "description": "List iterations (sprints) for a group.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_path": {"type": "string"},
                    "state": {"type": "string", "enum": ["opened", "upcoming", "current", "closed"]},
                },
                "required": ["group_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_iteration",
            "description": "Assign an iteration to an issue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_iid": {"type": "integer"},
                    "iteration_id": {"type": "integer"},
                },
                "required": ["issue_iid", "iteration_id"],
            },
        },
    },

    # --- Projects ---
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Create a new GitLab project (repository).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string", "description": "URL slug (defaults to slugified name)"},
                    "namespace_id": {"type": "integer", "description": "Group/namespace ID to create in (omit for user namespace)"},
                    "description": {"type": "string"},
                    "visibility": {"type": "string", "enum": ["private", "internal", "public"]},
                    "initialize_with_readme": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_info",
            "description": "Get project details including description, default branch, visibility, and web URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id_or_path": {"type": "string", "description": "Project ID or path (e.g. 'gbtest/test2')"},
                },
                "required": ["project_id_or_path"],
            },
        },
    },

    # --- Groups ---
    {
        "type": "function",
        "function": {
            "name": "create_group",
            "description": "Create a new GitLab group or subgroup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "parent_id": {"type": "integer", "description": "Parent group ID for subgroups (omit for top-level)"},
                    "description": {"type": "string"},
                    "visibility": {"type": "string", "enum": ["private", "internal", "public"]},
                },
                "required": ["name", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_groups",
            "description": "List groups the bot has access to.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                },
            },
        },
    },

    # --- Members ---
    {
        "type": "function",
        "function": {
            "name": "list_members",
            "description": "List members of a project or group.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["project", "group"], "description": "Whether to list project or group members"},
                    "scope_id_or_path": {"type": "string", "description": "Project ID or group path"},
                },
                "required": ["scope", "scope_id_or_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_member",
            "description": "Add a member to a project or group.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["project", "group"]},
                    "scope_id_or_path": {"type": "string"},
                    "username": {"type": "string"},
                    "access_level": {"type": "integer", "description": "10=Guest, 20=Reporter, 30=Developer, 40=Maintainer, 50=Owner"},
                },
                "required": ["scope", "scope_id_or_path", "username", "access_level"],
            },
        },
    },

    # --- Security ---
    {
        "type": "function",
        "function": {
            "name": "list_vulnerabilities",
            "description": "List project vulnerabilities, optionally filtered by severity or state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info", "unknown"]},
                    "state": {"type": "string", "enum": ["detected", "confirmed", "resolved", "dismissed"]},
                },
            },
        },
    },
]

# Inject optional project_id into all project-scoped tools
_PROJECT_SCOPED = {
    "create_issue", "update_issue", "search_issues", "link_issues",
    "create_branch", "commit_files", "read_file", "list_files",
    "create_merge_request", "get_mr_diff", "create_milestone", "list_milestones",
    "assign_milestone", "assign_iteration", "create_label", "create_wiki_page",
    "list_pipelines", "get_pipeline", "list_pipeline_jobs", "get_job_log",
    "retry_pipeline", "run_pipeline", "list_vulnerabilities",
}

_PID_PROP = {"type": "integer", "description": "Target project ID. Omit to use the current project."}

for _tool in TOOL_SCHEMAS:
    _name = _tool["function"]["name"]
    if _name in _PROJECT_SCOPED:
        props = _tool["function"]["parameters"].get("properties", {})
        if "project_id" not in props:
            props["project_id"] = _PID_PROP


# ---------------------------------------------------------------------------
# Tool executor — maps tool calls to GitLab API
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, args: dict, project_id: int) -> str:
    """Execute a tool call and return the result as a string for the LLM."""
    # Fix args that some models (Gemini) return as strings instead of proper types
    for key, val in list(args.items()):
        if isinstance(val, str):
            if val.startswith("[") or val.startswith("{"):
                # Try JSON first (most reliable)
                try:
                    args[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    # Gemini sometimes uses Python-style dicts with single quotes
                    # Convert to valid JSON: single quotes → double quotes
                    try:
                        fixed = val.replace("'", '"')
                        args[key] = json.loads(fixed)
                    except (json.JSONDecodeError, ValueError):
                        pass
            elif val.lower() in ("true", "false"):
                args[key] = val.lower() == "true"
            elif val.isdigit():
                args[key] = int(val)

    # Allow tools to specify a different project
    effective_pid = args.pop("project_id", None) or project_id
    log.info("Executing tool: %s (project=%s) %s", tool_name, effective_pid,
             {k: str(v)[:60] for k, v in args.items()})

    try:
        gl = glc.get_client()
        project = gl.projects.get(effective_pid)

        if tool_name == "post_comment":
            # Caller handles this — return instruction
            return f"Comment posted: {args['body'][:100]}..."

        elif tool_name == "update_comment":
            if args["target_type"] == "issue":
                glc.update_note_on_issue(project_id, args["target_iid"], args["note_id"], args["body"])
            else:
                glc.update_note_on_mr(project_id, args["target_iid"], args["note_id"], args["body"])
            return f"Updated comment {args['note_id']}"

        elif tool_name == "create_issue":
            data = {"title": args["title"]}
            if args.get("description"):
                data["description"] = args["description"]
            if args.get("labels"):
                data["labels"] = args["labels"]
            if args.get("milestone_id"):
                data["milestone_id"] = args["milestone_id"]
            issue = project.issues.create(data)
            if args.get("assignee_username"):
                # Look up user ID
                users = gl.users.list(username=args["assignee_username"])
                if users:
                    issue.assignee_ids = [users[0].id]
                    issue.save()
            return f"Created issue #{issue.iid} (global_id={issue.id}, project_id={effective_pid}): {issue.title}\nURL: {issue.web_url}"

        elif tool_name == "update_issue":
            issue = project.issues.get(args["issue_iid"])
            for field in ["title", "description", "labels", "state_event", "milestone_id"]:
                if field in args:
                    setattr(issue, field, args[field])
            if args.get("assignee_username"):
                users = gl.users.list(username=args["assignee_username"])
                if users:
                    issue.assignee_ids = [users[0].id]
            issue.save()
            return f"Updated issue #{args['issue_iid']}"

        elif tool_name == "search_issues":
            kwargs = {"per_page": 20}
            if args.get("search"):
                kwargs["search"] = args["search"]
            if args.get("labels"):
                kwargs["labels"] = args["labels"].split(",")
            if args.get("state"):
                kwargs["state"] = args["state"]
            if args.get("milestone"):
                kwargs["milestone"] = args["milestone"]
            issues = project.issues.list(**kwargs)
            if not issues:
                return "No issues found matching the criteria."
            lines = [f"Found {len(issues)} issue(s):"]
            for i in issues[:10]:
                lines.append(f"  #{i.iid}: {i.title} (state={i.state}, labels={i.labels})")
            return "\n".join(lines)

        elif tool_name == "link_issues":
            source = project.issues.get(args["source_issue_iid"])
            link_type = args.get("link_type", "relates_to")
            source.links.create({
                "target_project_id": project_id,
                "target_issue_iid": args["target_issue_iid"],
                "link_type": link_type,
            })
            return f"Linked #{args['source_issue_iid']} → #{args['target_issue_iid']} ({link_type})"

        elif tool_name == "create_branch":
            ref = args.get("ref", project.default_branch or "main")
            project.branches.create({"branch": args["branch_name"], "ref": ref})
            return f"Created branch: {args['branch_name']} from {ref}"

        elif tool_name == "commit_files":
            project.commits.create({
                "branch": args["branch"],
                "commit_message": args["commit_message"],
                "actions": args["files"],
            })
            file_paths = [f["file_path"] for f in args["files"]]
            return f"Committed {len(args['files'])} file(s) to {args['branch']}: {', '.join(file_paths)}"

        elif tool_name == "read_file":
            ref = args.get("ref", project.default_branch or "main")
            f = project.files.get(file_path=args["file_path"], ref=ref)
            content = f.decode().decode("utf-8")
            if len(content) > 5000:
                content = content[:5000] + "\n... (truncated)"
            return content

        elif tool_name == "list_files":
            ref = args.get("ref", project.default_branch or "main")
            path = args.get("path", "")
            recursive = args.get("recursive", False)
            tree = project.repository_tree(path=path, ref=ref, recursive=recursive, all=True)
            lines = []
            for item in tree:
                prefix = "[dir] " if item["type"] == "tree" else ""
                lines.append(f"{prefix}{item['path']}")
            return "\n".join(lines) if lines else "(empty)"

        elif tool_name == "create_merge_request":
            target = args.get("target_branch", project.default_branch or "main")
            mr = project.mergerequests.create({
                "source_branch": args["source_branch"],
                "target_branch": target,
                "title": args["title"],
                "description": args.get("description", ""),
            })
            # Self-assign
            try:
                mr.assignee_ids = [gl.user.id]
                mr.save()
            except Exception:
                pass
            return f"Created MR !{mr.iid}: {mr.title}\nURL: {mr.web_url}"

        elif tool_name == "get_mr_diff":
            mr = project.mergerequests.get(args["mr_iid"])
            changes = mr.changes()
            parts = []
            for change in changes.get("changes", []):
                parts.append(f"--- a/{change['old_path']}")
                parts.append(f"+++ b/{change['new_path']}")
                parts.append(change.get("diff", ""))
            diff = "\n".join(parts)
            if len(diff) > 8000:
                diff = diff[:8000] + "\n... (truncated)"
            return diff

        elif tool_name == "create_milestone":
            data = {"title": args["title"]}
            for field in ["description", "due_date", "start_date"]:
                if args.get(field):
                    data[field] = args[field]
            if args.get("group_path"):
                group = gl.groups.get(args["group_path"])
                ms = group.milestones.create(data)
                return f"Created group milestone: {ms.title} (id={ms.id})"
            else:
                ms = project.milestones.create(data)
                return f"Created project milestone: {ms.title} (id={ms.id})"

        elif tool_name == "create_label":
            data = {"name": args["name"], "color": args.get("color", "#428BCA")}
            if args.get("description"):
                data["description"] = args["description"]
            label = project.labels.create(data)
            return f"Created label: {label.name}"

        elif tool_name == "create_wiki_page":
            page = project.wikis.create({
                "title": args["title"],
                "content": args["content"],
            })
            return f"Created wiki page: {page.title}"

        elif tool_name == "list_pipelines":
            kwargs = {"per_page": args.get("per_page", 10)}
            if args.get("status"):
                kwargs["status"] = args["status"]
            if args.get("ref"):
                kwargs["ref"] = args["ref"]
            pipelines = project.pipelines.list(**kwargs)
            if not pipelines:
                return "No pipelines found."
            lines = [f"Found {len(pipelines)} pipeline(s):"]
            for p in pipelines:
                duration = f", {p.duration}s" if hasattr(p, "duration") and p.duration else ""
                lines.append(f"  #{p.id}: {p.status} (ref={p.ref}{duration}) {p.web_url}")
            return "\n".join(lines)

        elif tool_name == "get_pipeline":
            p = project.pipelines.get(args["pipeline_id"])
            return (
                f"Pipeline #{p.id}\n"
                f"Status: {p.status}\n"
                f"Ref: {p.ref}\n"
                f"Duration: {p.duration}s\n"
                f"Created: {p.created_at}\n"
                f"Finished: {p.finished_at}\n"
                f"URL: {p.web_url}"
            )

        elif tool_name == "list_pipeline_jobs":
            pipeline = project.pipelines.get(args["pipeline_id"])
            jobs = pipeline.jobs.list(per_page=50)
            if not jobs:
                return "No jobs in this pipeline."
            lines = [f"Jobs in pipeline #{args['pipeline_id']}:"]
            for j in jobs:
                duration = f", {j.duration}s" if hasattr(j, "duration") and j.duration else ""
                lines.append(f"  [{j.stage}] {j.name}: {j.status}{duration}")
            return "\n".join(lines)

        elif tool_name == "get_job_log":
            job = project.jobs.get(args["job_id"])
            trace = job.trace().decode("utf-8", errors="replace")
            if len(trace) > 5000:
                # Keep last 5000 chars — the end usually has the error
                trace = "... (truncated)\n" + trace[-5000:]
            return trace

        elif tool_name == "retry_pipeline":
            pipeline = project.pipelines.get(args["pipeline_id"])
            pipeline.retry()
            return f"Retried pipeline #{args['pipeline_id']}"

        elif tool_name == "run_pipeline":
            data = {"ref": args["ref"]}
            if args.get("variables"):
                data["variables"] = args["variables"]
            p = project.pipelines.create(data)
            return f"Triggered pipeline #{p.id} on {args['ref']}\nURL: {p.web_url}"

        elif tool_name == "create_epic":
            group = gl.groups.get(args["group_path"])
            data = {"title": args["title"]}
            for field in ["description", "labels", "start_date", "due_date"]:
                if args.get(field):
                    data[field] = args[field]
            epic = group.epics.create(data)
            return f"Created epic &{epic.iid}: {epic.title}\nURL: {epic.web_url}"

        elif tool_name == "list_epics":
            group = gl.groups.get(args["group_path"])
            kwargs = {"per_page": 20}
            if args.get("state"):
                kwargs["state"] = args["state"]
            if args.get("search"):
                kwargs["search"] = args["search"]
            epics = group.epics.list(**kwargs)
            if not epics:
                return "No epics found."
            lines = [f"Found {len(epics)} epic(s):"]
            for e in epics:
                lines.append(f"  &{e.iid}: {e.title} (state={e.state})")
            return "\n".join(lines)

        elif tool_name == "add_issue_to_epic":
            group = gl.groups.get(args["group_path"])
            epic = group.epics.get(args["epic_iid"])
            epic.issues.create({"issue_id": args["issue_id"]})
            return f"Added issue to epic &{args['epic_iid']}"

        elif tool_name == "list_milestones":
            kwargs = {"per_page": 20}
            if args.get("state"):
                kwargs["state"] = args["state"]
            if args.get("search"):
                kwargs["search"] = args["search"]
            milestones = project.milestones.list(**kwargs)
            if not milestones:
                return "No milestones found."
            lines = [f"Found {len(milestones)} milestone(s):"]
            for m in milestones:
                due = f", due {m.due_date}" if m.due_date else ""
                lines.append(f"  id={m.id}: {m.title} (state={m.state}{due})")
            return "\n".join(lines)

        elif tool_name == "assign_milestone":
            if args["target_type"] == "issue":
                target = project.issues.get(args["target_iid"])
            else:
                target = project.mergerequests.get(args["target_iid"])
            target.milestone_id = args["milestone_id"]
            target.save()
            return f"Assigned milestone {args['milestone_id']} to {args['target_type']} #{args['target_iid']}"

        elif tool_name == "create_iteration_cadence":
            import requests
            query = """
            mutation($input: IterationCadenceCreateInput!) {
              iterationCadenceCreate(input: $input) {
                iterationCadence { id title }
                errors
              }
            }
            """
            variables = {
                "input": {
                    "groupPath": args["group_path"],
                    "title": args["title"],
                    "startDate": args["start_date"],
                    "durationInWeeks": args.get("duration_in_weeks", 2),
                    "iterationsInAdvance": args.get("iterations_in_advance", 2),
                    "automatic": args.get("automatic", True),
                    "active": True,
                }
            }
            r = requests.post(
                f"{settings.gitlab_url}/api/graphql",
                headers={"PRIVATE-TOKEN": settings.gitlab_token},
                json={"query": query, "variables": variables},
                verify=settings.gitlab_ssl_verify,
            )
            data = r.json()
            if data.get("errors"):
                return f"Error: {data['errors']}"
            result = data.get("data", {}).get("iterationCadenceCreate", {})
            if result.get("errors"):
                return f"Error: {result['errors']}"
            cadence = result.get("iterationCadence", {})
            return f"Created iteration cadence: {cadence.get('title')} (id={cadence.get('id')})"

        elif tool_name == "create_iteration":
            import requests
            # First find the cadence for this group
            list_query = """
            query($groupPath: ID!) {
              group(fullPath: $groupPath) {
                iterationCadences(first: 1) {
                  nodes { id title }
                }
              }
            }
            """
            r = requests.post(
                f"{settings.gitlab_url}/api/graphql",
                headers={"PRIVATE-TOKEN": settings.gitlab_token},
                json={"query": list_query, "variables": {"groupPath": args["group_path"]}},
                verify=settings.gitlab_ssl_verify,
            )
            cadences = r.json().get("data", {}).get("group", {}).get("iterationCadences", {}).get("nodes", [])
            if not cadences:
                return "Error: No iteration cadence found. Create one first with create_iteration_cadence."
            cadence_id = cadences[0]["id"]

            query = """
            mutation($input: iterationCreateInput!) {
              iterationCreate(input: $input) {
                iteration { id title startDate dueDate }
                errors
              }
            }
            """
            variables = {
                "input": {
                    "groupPath": args["group_path"],
                    "iterationCadenceId": cadence_id,
                    "title": args["title"],
                    "startDate": args["start_date"],
                    "dueDate": args["due_date"],
                }
            }
            r = requests.post(
                f"{settings.gitlab_url}/api/graphql",
                headers={"PRIVATE-TOKEN": settings.gitlab_token},
                json={"query": query, "variables": variables},
                verify=settings.gitlab_ssl_verify,
            )
            data = r.json()
            if data.get("errors"):
                return f"Error: {data['errors']}"
            result = data.get("data", {}).get("iterationCreate", {})
            if result.get("errors"):
                return f"Error: {result['errors']}"
            it = result.get("iteration", {})
            return f"Created iteration: {it.get('title')} (id={it.get('id')}, {it.get('startDate')} to {it.get('dueDate')})"

        elif tool_name == "list_iterations":
            group = gl.groups.get(args["group_path"])
            kwargs = {"per_page": 20}
            if args.get("state"):
                kwargs["state"] = args["state"]
            try:
                iterations = group.iterations.list(**kwargs)
                if not iterations:
                    return "No iterations found."
                lines = [f"Found {len(iterations)} iteration(s):"]
                for it in iterations:
                    lines.append(f"  id={it.id}: {it.title} ({it.start_date} to {it.due_date}, state={it.state})")
                return "\n".join(lines)
            except Exception as e:
                return f"Could not list iterations: {e}"

        elif tool_name == "assign_iteration":
            issue = project.issues.get(args["issue_iid"])
            issue.iteration_id = args["iteration_id"]
            issue.save()
            return f"Assigned iteration {args['iteration_id']} to issue #{args['issue_iid']}"

        elif tool_name == "create_project":
            data = {"name": args["name"]}
            for field in ["path", "namespace_id", "description", "visibility", "initialize_with_readme"]:
                if args.get(field) is not None:
                    data[field] = args[field]
            new_project = gl.projects.create(data)
            return f"Created project: {new_project.path_with_namespace} (id={new_project.id})\nURL: {new_project.web_url}"

        elif tool_name == "get_project_info":
            p = gl.projects.get(args["project_id_or_path"])
            return (
                f"Project: {p.path_with_namespace}\n"
                f"Description: {p.description or '(none)'}\n"
                f"Default branch: {p.default_branch}\n"
                f"Visibility: {p.visibility}\n"
                f"URL: {p.web_url}\n"
                f"Created: {p.created_at}"
            )

        elif tool_name == "create_group":
            data = {"name": args["name"], "path": args["path"]}
            for field in ["parent_id", "description", "visibility"]:
                if args.get(field) is not None:
                    data[field] = args[field]
            group = gl.groups.create(data)
            return f"Created group: {group.full_path} (id={group.id})\nURL: {group.web_url}"

        elif tool_name == "list_groups":
            kwargs = {"per_page": 20}
            if args.get("search"):
                kwargs["search"] = args["search"]
            groups = gl.groups.list(**kwargs)
            if not groups:
                return "No groups found."
            lines = [f"Found {len(groups)} group(s):"]
            for g in groups:
                lines.append(f"  {g.full_path} (id={g.id}, visibility={g.visibility})")
            return "\n".join(lines)

        elif tool_name == "list_members":
            if args["scope"] == "project":
                target = gl.projects.get(args["scope_id_or_path"])
            else:
                target = gl.groups.get(args["scope_id_or_path"])
            members = target.members.list(per_page=50)
            if not members:
                return "No members found."
            lines = [f"Found {len(members)} member(s):"]
            level_names = {10: "Guest", 20: "Reporter", 30: "Developer", 40: "Maintainer", 50: "Owner"}
            for m in members:
                role = level_names.get(m.access_level, str(m.access_level))
                lines.append(f"  @{m.username} ({role})")
            return "\n".join(lines)

        elif tool_name == "add_member":
            if args["scope"] == "project":
                target = gl.projects.get(args["scope_id_or_path"])
            else:
                target = gl.groups.get(args["scope_id_or_path"])
            users = gl.users.list(username=args["username"])
            if not users:
                return f"User '{args['username']}' not found."
            target.members.create({"user_id": users[0].id, "access_level": args["access_level"]})
            return f"Added @{args['username']} as {args['access_level']} to {args['scope']} {args['scope_id_or_path']}"

        elif tool_name == "list_vulnerabilities":
            kwargs = {"per_page": 20}
            if args.get("severity"):
                kwargs["severity"] = args["severity"]
            if args.get("state"):
                kwargs["state"] = args["state"]
            try:
                vulns = project.vulnerabilities.list(**kwargs)
                if not vulns:
                    return "No vulnerabilities found matching the criteria."
                lines = [f"Found {len(vulns)} vulnerability/ies:"]
                for v in vulns[:10]:
                    lines.append(f"  [{v.severity}] {v.name} (state={v.state})")
                    if hasattr(v, "description") and v.description:
                        lines.append(f"    {v.description[:100]}")
                return "\n".join(lines)
            except Exception as e:
                return f"Could not fetch vulnerabilities: {e}"

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        log.warning("Tool %s failed: %s", tool_name, e)
        return f"TOOL_ERROR: {tool_name} failed — {e}\nYou should try a different approach or skip this action."
