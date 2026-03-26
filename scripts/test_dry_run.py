#!/usr/bin/env python3
"""End-to-end dry run: routes webhook payloads through handlers with mocked GitLab + LLM.

No external services needed. Shows what the bot *would* do.

Usage:
    PYTHONPATH=. python scripts/test_dry_run.py
    PYTHONPATH=. python scripts/test_dry_run.py --family anthropic   # see Claude-style prompts
    PYTHONPATH=. python scripts/test_dry_run.py --family ollama      # see open-model prompts
"""

import argparse
import asyncio
import os
from unittest.mock import patch

os.environ.setdefault("GITBOT_GITLAB_TOKEN", "fake")
os.environ.setdefault("GITBOT_BOT_USERNAME", "gitbot")

from gitbot.models import Family


EVENTS = [
    (
        "Issue Hook",
        {
            "object_kind": "issue",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "iid": 42,
                "title": "Add user authentication",
                "description": "We need JWT auth on all API endpoints.",
                "action": "update",
            },
            "assignees": [{"username": "gitbot"}],
        },
    ),
    (
        "Note Hook",
        {
            "object_kind": "note",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "note": "@gitbot what about using bcrypt for password hashing?",
                "noteable_type": "Issue",
                "discussion_id": "disc-001",
            },
            "issue": {"iid": 42, "title": "Add user authentication"},
        },
    ),
    (
        "Merge Request Hook",
        {
            "object_kind": "merge_request",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "iid": 7,
                "title": "Fix SQL injection in login",
                "description": "Parameterized the query.",
                "action": "open",
            },
            "assignees": [],
            "reviewers": [{"username": "gitbot"}],
        },
    ),
]


async def main(family: str):
    # Override family via env before importing config
    os.environ["GITBOT_LLM_FAMILY"] = family

    # Re-import to pick up the env change
    import importlib
    import gitbot.config
    importlib.reload(gitbot.config)
    from gitbot.config import settings
    settings.__init__()  # re-read env

    from gitbot.router import route_event

    call_log = []

    async def fake_complete(task, *, system="", prompt):
        print(f"\n  --- LLM call (task={task}, family={family}) ---")
        print(f"  System: {system[:120]}...")
        print(f"  Prompt: {prompt[:200]}...")
        call_log.append(task)
        return f"[DRY RUN] Response for {task}"

    def make_printer(name):
        def printer(*args, **kwargs):
            print(f"  -> {name}({', '.join(str(a) for a in args)})")
        return printer

    patches = [
        patch("gitbot.handlers.llm.complete", side_effect=fake_complete),
        patch("gitbot.handlers.glc.post_note_on_issue", side_effect=make_printer("post_note_on_issue")),
        patch("gitbot.handlers.glc.post_note_on_mr", side_effect=make_printer("post_note_on_mr")),
        patch("gitbot.handlers.glc.reply_to_discussion", side_effect=make_printer("reply_to_discussion")),
        patch("gitbot.handlers.glc.get_mr_diff", return_value="--- a/login.py\n+++ b/login.py\n@@ -1 +1 @@\n-cursor.execute(f'SELECT * FROM users WHERE name={name}')\n+cursor.execute('SELECT * FROM users WHERE name=%s', (name,))"),
    ]

    for p in patches:
        p.start()

    for event_type, payload in EVENTS:
        print(f"\n{'='*60}")
        print(f"Event: {event_type}")
        print(f"{'='*60}")
        await route_event(event_type, payload)

    for p in patches:
        p.stop()

    print(f"\n{'='*60}")
    print(f"Done! {len(call_log)} LLM calls: {', '.join(call_log)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default="claude-code", choices=[f.value for f in Family])
    args = parser.parse_args()
    asyncio.run(main(args.family))
