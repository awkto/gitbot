#!/usr/bin/env python3
"""Run the GitBot integration test suite.

Creates test issues on GitLab, triggers webhooks, waits for completion,
collects results, and writes a manifest for cleanup.

Usage:
    GITBOT_GITLAB_URL=https://gitlab.dnsif.ca \
    GITBOT_GITLAB_TOKEN=glpat-xxx \
    GITBOT_BOT_URL=https://gitbot.nginx.dnsif.ca \
    python scripts/run_test_suite.py

Environment:
    GITBOT_GITLAB_URL     GitLab instance URL
    GITBOT_GITLAB_TOKEN   GitLab API token (bot account)
    GITBOT_BOT_URL        GitBot webhook URL
    GITBOT_GITLAB_SSL_VERIFY  Set to 'false' for self-signed certs
    GITBOT_TRIGGER_USER   Username to use as webhook actor (default: altanc)
    GITBOT_PROJECT_ID     Project ID to create test issues in (default: 32)
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

GITLAB_URL = os.environ.get("GITBOT_GITLAB_URL", "https://gitlab.dnsif.ca")
TOKEN = os.environ.get("GITBOT_GITLAB_TOKEN", "")
BOT_URL = os.environ.get("GITBOT_BOT_URL", "https://gitbot.nginx.dnsif.ca")
SSL_VERIFY = os.environ.get("GITBOT_GITLAB_SSL_VERIFY", "true").lower() != "false"
TRIGGER_USER = os.environ.get("GITBOT_TRIGGER_USER", "altanc")
PROJECT_ID = int(os.environ.get("GITBOT_PROJECT_ID", "32"))

ctx = ssl.create_default_context()
if not SSL_VERIFY:
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

# Manifest of created objects (for cleanup)
manifest = {
    "created_at": datetime.now().isoformat(),
    "gitlab_url": GITLAB_URL,
    "project_id": PROJECT_ID,
    "issues": [],
    "merge_requests": [],
    "branches": [],
    "milestones": [],
    "group_milestones": [],
    "epics": [],
    "labels": [],
    "wiki_pages": [],
    "projects": [],
    "groups": [],
    "files_on_main": [],
    "iteration_cadences": [],
}


def api_get(path):
    url = f"{GITLAB_URL}/api/v4{path}"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": TOKEN})
    resp = urllib.request.urlopen(req, context=ctx)
    return json.loads(resp.read())


def api_post(path, data):
    url = f"{GITLAB_URL}/api/v4{path}"
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, headers={"PRIVATE-TOKEN": TOKEN})
    resp = urllib.request.urlopen(req, context=ctx)
    return json.loads(resp.read())


def create_issue(title, description):
    issue = api_post(f"/projects/{PROJECT_ID}/issues", {
        "title": title,
        "description": description,
    })
    manifest["issues"].append({"project_id": PROJECT_ID, "iid": issue["iid"], "id": issue["id"]})
    return issue["iid"]


def send_webhook(iid, title, description):
    payload = json.dumps({
        "object_kind": "issue",
        "user": {"username": TRIGGER_USER},
        "project": {"id": PROJECT_ID, "name": "test2"},
        "object_attributes": {
            "iid": iid,
            "title": title,
            "description": description,
            "action": "update",
            "state": "opened",
        },
        "assignees": [{"username": "gitbot"}],
    }).encode()
    req = urllib.request.Request(
        f"{BOT_URL}/webhook", data=payload,
        headers={"X-Gitlab-Event": "Issue Hook", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, context=ctx)
    except urllib.error.HTTPError:
        pass


def create_and_trigger(title, description):
    iid = create_issue(title, description)
    send_webhook(iid, title, description)
    print(f"  #{iid}: {title}")
    return iid


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------

TESTS = {
    "A1": ("Create issue with full metadata",
           "Create an issue in this project titled 'Database migration fails on PostgreSQL 16' "
           "with description 'The migration script throws a syntax error on PG16.' "
           "Assign it the labels 'bug' and 'database', and assign it to @altanc."),

    "A4": ("Link related issues",
           "Create two issues in this project: 'Implement user API endpoint' and "
           "'Write user API tests'. Link the second as blocked_by the first."),

    "B1": ("Branch and commit single file",
           "Create a branch called feature/logging and commit a Python file called logger.py "
           "that sets up a basic Python logging configuration with file and console handlers."),

    "B2": ("Commit directly to main",
           "Create a file called CONTRIBUTING.md with standard contribution guidelines "
           "(how to fork, branch naming, PR process) and commit it directly to the main branch. "
           "Do NOT create a merge request or feature branch."),

    "C1": ("Branch commit and open MR",
           "Create a Python script called validate_email.py with a function that validates "
           "email addresses using regex. Put it on a new branch feature/email-validation, "
           "and open a merge request titled 'Add email validation utility' targeting main."),

    "C3": ("MR with reviewer",
           "Create a bash script backup.sh that tars the /etc directory with a timestamp, "
           "commit it to branch feature/backup-script, and open an MR targeting main. "
           "Assign @altanc as the reviewer of the MR."),

    "F1": ("Create project milestone",
           "Create a milestone called 'Sprint 42' in this project with start date 2026-04-01 "
           "and due date 2026-04-14, description 'Two-week sprint focusing on API improvements'."),

    "F3": ("Milestone and issues",
           "Find or create a milestone called 'Sprint 42' in this project. Then create 2 issues: "
           "'Refactor auth middleware' and 'Add rate limiting'. Assign both to the Sprint 42 milestone."),

    "G1": ("Create epic",
           "Create an epic called 'Platform Modernization' in the gbtest group with description "
           "'Multi-quarter initiative to modernize our platform stack'."),

    "J1": ("Create wiki page",
           "Create a wiki page titled 'Architecture Overview' with content describing a 3-tier "
           "architecture: frontend (React), API (Python FastAPI), database (PostgreSQL). "
           "Include a section for each tier."),

    "K1": ("Create labels",
           "Create these exact labels: 'priority::critical' (color #FF0000), "
           "'priority::high' (color #FF6600), 'priority::low' (color #00CC00)."),

    "M1": ("Vague issue - should ask questions",
           "Fix the performance issue."),

    "H1": ("Create iteration cadence",
           "Create an iteration cadence called 'Bi-weekly Sprints' in the gbtest group "
           "with 2-week duration, starting 2026-04-01, auto-creating 3 iterations in advance."),

    "N1": ("Full project setup (complex)",
           "Set up a new project ecosystem:\n"
           "1. Create a group called weatherapp under gbtest (use parent_path 'gbtest')\n"
           "2. Create 2 projects in the weatherapp group (use namespace_path 'gbtest/weatherapp'): "
           "weather-api (description: Backend REST API, initialize with README) and "
           "weather-frontend (description: React frontend, initialize with README)\n"
           "3. In weather-api, create 3 issues: 'Implement /forecast endpoint' (label: feature), "
           "'Add authentication middleware' (label: feature), 'Write API documentation' (label: docs)\n"
           "4. In weather-frontend, create 3 issues: 'Build dashboard layout' (label: feature), "
           "'Implement weather map widget' (label: feature), 'Add unit tests' (label: testing)\n"
           "5. Create a group milestone called 'MVP Release' in the weatherapp group with due date 2026-06-01\n"
           "6. Create an epic called 'Weather App MVP' in the weatherapp group"),

    "N3": ("Cross-project issue management",
           "Create 2 issues: 'API: Add /users endpoint' with labels 'backend' and 'feature', "
           "and 'Frontend: Add user list page' with labels 'frontend' and 'feature'. "
           "Link the second as blocked_by the first. Create milestone 'User Management Phase 1' "
           "with due date 2026-05-15 and assign both issues to it."),
}


def main():
    print(f"GitBot Test Suite Runner")
    print(f"  GitLab: {GITLAB_URL}")
    print(f"  Bot: {BOT_URL}")
    print(f"  Project: {PROJECT_ID}")
    print(f"  Time: {datetime.now().isoformat()}")

    # Check bot health
    try:
        req = urllib.request.Request(f"{BOT_URL}/health")
        resp = urllib.request.urlopen(req, context=ctx)
        health = json.loads(resp.read())
        print(f"  Bot version: {health['version']}")
    except Exception as e:
        print(f"  Bot not reachable: {e}")
        sys.exit(1)

    print(f"\nCreating and triggering {len(TESTS)} tests...\n")

    test_iids = {}
    for test_id, (title, desc) in TESTS.items():
        iid = create_and_trigger(f"TS-{test_id}: {title}", desc)
        test_iids[test_id] = iid
        time.sleep(1)

    # Save manifest
    manifest_path = f"test_manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    manifest["test_iids"] = test_iids
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest saved: {manifest_path}")

    # Wait for completion
    print(f"\nWaiting for workflows to complete...")
    max_wait = 900  # 15 minutes
    start = time.time()
    while time.time() - start < max_wait:
        try:
            req = urllib.request.Request(f"{BOT_URL}/admin/api/current")
            resp = urllib.request.urlopen(req, context=ctx)
            current = json.loads(resp.read())
            running = len(current)
            elapsed = int(time.time() - start)
            if running == 0 and elapsed > 30:
                print(f"  All workflows complete ({elapsed}s)")
                break
            print(f"  {running} running... ({elapsed}s)", end="\r")
        except Exception:
            pass
        time.sleep(10)

    # Collect results
    print(f"\n\nCollecting results...")
    try:
        req = urllib.request.Request(f"{BOT_URL}/admin/api/workflows?limit=50")
        resp = urllib.request.urlopen(req, context=ctx)
        workflows = json.loads(resp.read())
    except Exception:
        workflows = []

    print(f"\n{'Test':<6} {'Status':<12} {'Steps':<8} {'Time':<8} {'Esc':<4} Issue")
    print("-" * 60)
    for test_id, iid in test_iids.items():
        target = f"Issue #{iid}"
        wf = next((w for w in workflows if w["target"] == target), None)
        if wf:
            status = wf["status"]
            steps = f"{wf['completed_steps']}/{wf['plan_steps']}"
            elapsed = f"{wf['elapsed_seconds']}s"
            esc = str(wf.get("escalations", 0))
        else:
            status = "not found"
            steps = "-"
            elapsed = "-"
            esc = "-"
        print(f"{test_id:<6} {status:<12} {steps:<8} {elapsed:<8} {esc:<4} #{iid}")

    print(f"\nDone. Use 'python scripts/test_cleanup.py {manifest_path}' to clean up.")


if __name__ == "__main__":
    main()
