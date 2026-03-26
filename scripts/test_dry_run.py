#!/usr/bin/env python3
"""End-to-end dry run: routes webhook payloads through the brain with mocked LLM + GitLab.

No external services needed. Shows the iterative context gathering and decision flow.

Usage:
    PYTHONPATH=. python scripts/test_dry_run.py
"""

import asyncio
import os
from unittest.mock import patch, MagicMock

os.environ.setdefault("GITBOT_GITLAB_TOKEN", "fake")
os.environ.setdefault("GITBOT_BOT_USERNAME", "gitbot")
os.environ.setdefault("GITBOT_LLM_FAMILY", "anthropic")

import json


EVENTS = [
    (
        "Issue Hook — clear implementation request",
        "Issue Hook",
        {
            "object_kind": "issue",
            "user": {"username": "alice"},
            "project": {"id": 1, "name": "test-project"},
            "object_attributes": {
                "iid": 42,
                "title": "Add user authentication",
                "description": "Add JWT auth to the API. Use python-jose for tokens.",
                "action": "update",
                "state": "opened",
            },
            "assignees": [{"username": "gitbot"}],
        },
    ),
    (
        "Issue Hook — vague request (should ask)",
        "Issue Hook",
        {
            "object_kind": "issue",
            "user": {"username": "bob"},
            "project": {"id": 1, "name": "test-project"},
            "object_attributes": {
                "iid": 43,
                "title": "Fix the bug",
                "description": "It's broken, please fix",
                "action": "update",
                "state": "opened",
            },
            "assignees": [{"username": "gitbot"}],
        },
    ),
    (
        "Note Hook — @mention question",
        "Note Hook",
        {
            "object_kind": "note",
            "user": {"username": "alice"},
            "project": {"id": 1, "name": "test-project"},
            "object_attributes": {
                "note": "@gitbot should we use Redis or in-memory for sessions?",
                "noteable_type": "Issue",
                "discussion_id": "abc123",
                "author": {"username": "alice"},
            },
            "issue": {"iid": 42, "title": "Add user authentication"},
        },
    ),
]

# Responses the mock LLM will return for each call
MOCK_RESPONSES = [
    # Issue 42: triage round 1 — fetch
    json.dumps({"status": "fetch", "fetch_sources": ["repo_tree"]}),
    # Issue 42: triage round 2 — ready to implement
    json.dumps({"status": "ready", "action": "create_mr", "reasoning": "Clear requirements, I have repo context", "implementation_notes": "Use python-jose"}),
    # Issue 42: implement
    json.dumps({"branch_name": "feature/auth", "commit_message": "Add JWT auth", "mr_title": "Add authentication", "mr_description": "JWT auth", "files": [{"action": "create", "file_path": "auth.py", "content": "# auth code"}]}),
    # Issue 43: triage round 1 — ask (vague)
    json.dumps({"status": "ready", "action": "ask", "reasoning": "Too vague", "content": "What bug? Can you share error logs or steps to reproduce?", "mention": "@bob"}),
    # Note mention: triage round 1 — comment
    json.dumps({"status": "ready", "action": "comment", "reasoning": "Answering a design question", "content": "For a single-instance deployment, in-memory is simpler. Redis if you need horizontal scaling."}),
]


async def main():
    call_count = [0]

    async def fake_llm_complete(task, *, system="", prompt):
        idx = call_count[0]
        call_count[0] += 1
        resp = MOCK_RESPONSES[idx] if idx < len(MOCK_RESPONSES) else '{"status":"ready","action":"nothing"}'
        print(f"    LLM call #{idx} (task={task}): {resp[:100]}...")
        return resp

    def make_noop(name):
        def noop(*args, **kwargs):
            print(f"    → {name}({', '.join(str(a) for a in args[:3])})")
            return 1  # fake note ID
        return noop

    mock_tree = [{"path": "app.py", "type": "blob"}, {"path": "requirements.txt", "type": "blob"}]

    patches = [
        patch("gitbot.brain.llm.complete", side_effect=fake_llm_complete),
        patch("gitbot.brain.glc.post_note_on_issue", side_effect=make_noop("post_note_on_issue")),
        patch("gitbot.brain.glc.update_note_on_issue", side_effect=make_noop("update_note_on_issue")),
        patch("gitbot.brain.glc.post_note_on_mr", side_effect=make_noop("post_note_on_mr")),
        patch("gitbot.brain.glc.update_note_on_mr", side_effect=make_noop("update_note_on_mr")),
        patch("gitbot.brain.glc.set_issue_labels", side_effect=make_noop("set_issue_labels")),
        patch("gitbot.brain.glc.remove_issue_labels", side_effect=make_noop("remove_issue_labels")),
        patch("gitbot.brain.glc.create_branch", side_effect=make_noop("create_branch")),
        patch("gitbot.brain.glc.commit_files", side_effect=make_noop("commit_files")),
        patch("gitbot.brain.glc.create_merge_request", return_value={"iid": 99, "web_url": "https://example.com/mr/99"}),
        patch("gitbot.brain.glc.assign_mr", side_effect=make_noop("assign_mr")),
        patch("gitbot.brain.glc.get_bot_user_id", return_value=1),
        patch("gitbot.brain.glc.get_client"),
        patch("gitbot.context.glc.get_client"),
        patch("gitbot.context.glc.list_repo_tree", return_value=mock_tree),
        patch("gitbot.context.glc.get_mr_details", return_value={"author": "gitbot", "assignees": ["gitbot"], "source_branch": "feature/x", "target_branch": "main", "state": "opened"}),
        patch("gitbot.context.state.get_pending_question", return_value=None),
    ]

    for p in patches:
        p.start()

    from gitbot.router import route_event

    for label, event_type, payload in EVENTS:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        await route_event(event_type, payload)

    for p in patches:
        p.stop()

    print(f"\n{'='*60}")
    print(f"Done! {call_count[0]} LLM calls total.")


if __name__ == "__main__":
    asyncio.run(main())
