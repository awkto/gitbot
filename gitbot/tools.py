"""GitLab tool definitions for LLM tool_use.

Each tool has:
- A schema (for the LLM to understand parameters)
- An execute function (maps tool call to GitLab API)

Tools are provider-agnostic — litellm normalizes tool_use across
Anthropic, OpenAI, and Ollama.
"""

import logging
from gitbot import gitlab_client as glc

log = logging.getLogger(__name__)


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

    # --- Issues ---
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": "Create a new issue in the current project.",
            "parameters": {
                "type": "object",
                "properties": {
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
            "description": "Create a project milestone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
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


# ---------------------------------------------------------------------------
# Tool executor — maps tool calls to GitLab API
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, args: dict, project_id: int) -> str:
    """Execute a tool call and return the result as a string for the LLM."""
    log.info("Executing tool: %s(%s)", tool_name, {k: str(v)[:80] for k, v in args.items()})

    try:
        gl = glc.get_client()
        project = gl.projects.get(project_id)

        if tool_name == "post_comment":
            # Caller handles this — return instruction
            return f"Comment posted: {args['body'][:100]}..."

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
            return f"Created issue #{issue.iid}: {issue.title}\nURL: {issue.web_url}"

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
            ms = project.milestones.create(data)
            return f"Created milestone: {ms.title} (id={ms.id})"

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
        return f"Error: {e}"
