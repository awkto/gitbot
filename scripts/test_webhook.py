#!/usr/bin/env python3
"""Send fake GitLab webhook payloads to the local server for testing.

Usage:
    python scripts/test_webhook.py issue_assigned
    python scripts/test_webhook.py mr_assigned
    python scripts/test_webhook.py mr_review
    python scripts/test_webhook.py mention_issue
    python scripts/test_webhook.py mention_mr
"""

import argparse
import httpx
import json
import sys

BASE_URL = "http://localhost:8042"

PAYLOADS = {
    "issue_assigned": {
        "event": "Issue Hook",
        "body": {
            "object_kind": "issue",
            "event_type": "issue",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "iid": 42,
                "title": "Add user authentication to the API",
                "description": "We need to add JWT-based authentication to our REST API endpoints.\n\n- Login endpoint\n- Token refresh\n- Protected route middleware\n\nPlease propose an implementation plan.",
                "action": "update",
                "state": "opened",
            },
            "assignees": [{"username": "gitbot", "name": "GitBot"}],
        },
    },
    "mr_assigned": {
        "event": "Merge Request Hook",
        "body": {
            "object_kind": "merge_request",
            "event_type": "merge_request",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "iid": 17,
                "title": "Add rate limiting middleware",
                "description": "Adds rate limiting to API endpoints using a token bucket algorithm.",
                "action": "open",
                "state": "opened",
                "source_branch": "feature/rate-limiting",
                "target_branch": "main",
            },
            "assignees": [{"username": "gitbot", "name": "GitBot"}],
            "reviewers": [],
        },
    },
    "mr_review": {
        "event": "Merge Request Hook",
        "body": {
            "object_kind": "merge_request",
            "event_type": "merge_request",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "iid": 17,
                "title": "Add rate limiting middleware",
                "description": "Adds rate limiting to API endpoints using a token bucket algorithm.",
                "action": "update",
                "state": "opened",
                "source_branch": "feature/rate-limiting",
                "target_branch": "main",
            },
            "assignees": [],
            "reviewers": [{"username": "gitbot", "name": "GitBot"}],
        },
    },
    "mention_issue": {
        "event": "Note Hook",
        "body": {
            "object_kind": "note",
            "event_type": "note",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "note": "@gitbot what do you think about using Redis for the session store instead of in-memory?",
                "noteable_type": "Issue",
                "discussion_id": "abc123",
            },
            "issue": {
                "iid": 42,
                "title": "Add user authentication to the API",
            },
        },
    },
    "mention_mr": {
        "event": "Note Hook",
        "body": {
            "object_kind": "note",
            "event_type": "note",
            "project": {"id": 1, "name": "test-project", "web_url": "https://gitlab.com/test/test-project"},
            "object_attributes": {
                "note": "@gitbot can you check if this handles concurrent requests correctly?",
                "noteable_type": "MergeRequest",
                "discussion_id": "def456",
            },
            "merge_request": {
                "iid": 17,
                "title": "Add rate limiting middleware",
            },
        },
    },
}


def send(name: str, base_url: str, secret: str | None = None):
    if name not in PAYLOADS:
        print(f"Unknown payload: {name}")
        print(f"Available: {', '.join(PAYLOADS)}")
        sys.exit(1)

    spec = PAYLOADS[name]
    headers = {"X-Gitlab-Event": spec["event"]}
    if secret:
        headers["X-Gitlab-Token"] = secret

    print(f"Sending {name} event to {base_url}/webhook ...")
    resp = httpx.post(f"{base_url}/webhook", json=spec["body"], headers=headers)
    print(f"Response: {resp.status_code} {resp.json()}")


def main():
    parser = argparse.ArgumentParser(description="Send test webhook payloads")
    parser.add_argument("event", choices=list(PAYLOADS.keys()), help="Event type to send")
    parser.add_argument("--url", default=BASE_URL, help="Server URL")
    parser.add_argument("--secret", default=None, help="Webhook secret token")
    args = parser.parse_args()
    send(args.event, args.url, args.secret)


if __name__ == "__main__":
    main()
