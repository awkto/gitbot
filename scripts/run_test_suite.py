#!/usr/bin/env python3
"""Run the full GitBot integration test suite (36 tests).

Creates test issues on GitLab, triggers webhooks (Issue Hook and Note Hook),
waits for completion, and collects results.

Usage:
    GITBOT_GITLAB_URL=https://gitlab.dnsif.ca \
    GITBOT_GITLAB_TOKEN=glpat-xxx \
    GITBOT_BOT_URL=https://gitbot.nginx.dnsif.ca \
    GITBOT_GITLAB_SSL_VERIFY=false \
    python scripts/run_test_suite.py
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
PROJECT_NAME = os.environ.get("GITBOT_PROJECT_NAME", "test2")
GROUP_ID = int(os.environ.get("GITBOT_GROUP_ID", "122"))

ctx = ssl.create_default_context()
if not SSL_VERIFY:
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE


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
        "title": title, "description": description,
    })
    return issue["iid"]


def send_issue_webhook(iid, title, description):
    """Send an Issue Hook webhook (assigned to bot)."""
    payload = json.dumps({
        "object_kind": "issue",
        "user": {"username": TRIGGER_USER},
        "project": {"id": PROJECT_ID, "name": PROJECT_NAME},
        "object_attributes": {
            "iid": iid, "title": title,
            "description": description,
            "action": "update", "state": "opened",
        },
        "assignees": [{"username": "gitbot"}],
    }).encode()
    req = urllib.request.Request(
        f"{BOT_URL}/webhook", data=payload,
        headers={"X-Gitlab-Event": "Issue Hook", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, context=ctx)
    except Exception:
        pass


def send_note_webhook(iid, note_body, target_type="Issue"):
    """Send a Note Hook webhook (@mention on an issue or MR)."""
    target_key = "issue" if target_type == "Issue" else "merge_request"
    payload = json.dumps({
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": TRIGGER_USER},
        "project": {"id": PROJECT_ID, "name": PROJECT_NAME},
        "object_attributes": {
            "note": note_body,
            "noteable_type": target_type,
            "discussion_id": f"test_{int(time.time())}",
            "author": {"username": TRIGGER_USER},
        },
        target_key: {"iid": iid, "title": "test target"},
    }).encode()
    req = urllib.request.Request(
        f"{BOT_URL}/webhook", data=payload,
        headers={"X-Gitlab-Event": "Note Hook", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, context=ctx)
    except Exception:
        pass


def trigger_issue(title, desc):
    """Create issue and send Issue Hook webhook. Returns IID."""
    iid = create_issue(f"TS: {title}", desc)
    send_issue_webhook(iid, f"TS: {title}", desc)
    print(f"  #{iid}: {title}")
    time.sleep(0.5)
    return iid


def trigger_mention(iid, note_body):
    """Send Note Hook for @mention on existing issue."""
    send_note_webhook(iid, note_body)
    print(f"  #{iid}: @mention sent")
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# All 36 tests
# ---------------------------------------------------------------------------

def run_all():
    results = {}  # test_id -> iid

    print("\n--- A: Issues & Comments ---")

    results["A1"] = trigger_issue("A1: Create issue with metadata",
        "Create an issue in this project titled 'Database migration fails on PostgreSQL 16' "
        "with description 'The migration script throws a syntax error on PG16.' "
        "Assign it the labels 'bug' and 'database', and assign it to @altanc.")

    results["A2"] = trigger_issue("A2: Update existing issue",
        "Create an issue titled 'Test issue for update' in this project. "
        "Then update it: change the title to 'Updated: Test issue', add label 'verified', and close it.")

    results["A3"] = trigger_issue("A3: Search and summarize",
        "Search for all open issues in this project and post a comment here listing up to 10 of them "
        "with their IID, title, and labels.")

    results["A4"] = trigger_issue("A4: Link related issues",
        "Create two issues: 'Implement user API endpoint' and 'Write user API tests'. "
        "Link the second as blocked_by the first.")

    print("\n--- B: Branches & Code ---")

    results["B1"] = trigger_issue("B1: Branch and commit single file",
        "Create a branch called feature/logging and commit a Python file called logger.py "
        "that sets up Python logging with file and console handlers.")

    results["B2"] = trigger_issue("B2: Commit directly to main",
        "Create a file called CONTRIBUTING.md with contribution guidelines "
        "(how to fork, branch naming, PR process) and commit directly to main. No MR.")

    results["B3"] = trigger_issue("B3: Commit to existing branch",
        "Create a Rust file called fibonacci.rs with a function that calculates the nth "
        "Fibonacci number iteratively, and commit it to the dev2026 branch.")

    results["B4"] = trigger_issue("B4: Commit to non-existent branch",
        "Create a Go file called palindrome.go that checks if a string is a palindrome, "
        "and push it to the feature/string-utils branch (create the branch first).")

    results["B5"] = trigger_issue("B5: Read file and answer",
        "Read the file .gitlab-ci.yml in this project and post a comment here explaining "
        "what stages are defined and what each job does. If the file doesn't exist, say so.")

    results["B6"] = trigger_issue("B6: Multi-file commit",
        "Create a branch feature/config and commit these 3 files in a single commit: "
        "config/database.yml (PostgreSQL connection: host localhost, port 5432, db myapp), "
        "config/redis.yml (Redis: host localhost, port 6379), "
        "config/app.yml (app name MyApp, port 3000, log level info).")

    print("\n--- C: Merge Requests ---")

    results["C1"] = trigger_issue("C1: Branch commit and open MR",
        "Create a Python script validate_email.py with a function that validates email "
        "addresses using regex. Branch feature/email-validation, MR titled "
        "'Add email validation utility' targeting main.")

    results["C2"] = trigger_issue("C2: Review MR diff",
        "Find the most recently opened merge request in this project. Post a code review "
        "comment on this issue with findings categorized as bug, suggestion, or nitpick.")

    results["C3"] = trigger_issue("C3: MR with reviewer",
        "Create a bash script backup.sh that tars /etc with a timestamp filename. "
        "Branch feature/backup-script, MR targeting main. Assign @altanc as reviewer.")

    print("\n--- D: Projects & Groups ---")

    results["D1"] = trigger_issue("D1: Create project with README",
        "Create a new project called api-gateway in the gbtest group "
        "(use namespace_path 'gbtest') with description 'API gateway for routing and rate limiting', "
        "visibility private, initialized with a README.")

    results["D2"] = trigger_issue("D2: Create subgroup",
        "Create a new subgroup called platform-team under the gbtest group "
        "(use parent_path 'gbtest') with description 'Platform engineering team projects'.")

    results["D3"] = trigger_issue("D3: Get project info",
        "Get the details of the gbtest/test2 project (default branch, visibility, description, URL) "
        "and post them as a comment on this issue.")

    print("\n--- E: Members ---")

    results["E1"] = trigger_issue("E1: List project members",
        "List all members of this project and their access levels. "
        "Post the results as a comment on this issue.")

    results["E2"] = trigger_issue("E2: Add member",
        "Check if user @gitbot is already a member of this project. "
        "If not, add @gitbot as a Developer (access level 30). "
        "Post the result as a comment.")

    print("\n--- F: Milestones ---")

    results["F1"] = trigger_issue("F1: Create project milestone",
        "Create a milestone called 'Sprint 42' in this project with start date 2026-04-01, "
        "due date 2026-04-14, description 'Two-week sprint for API improvements'.")

    results["F2"] = trigger_issue("F2: Create group milestone",
        "Create a group milestone called 'Q2 2026 Planning' in the gbtest group "
        "with due date 2026-06-30.")

    results["F3"] = trigger_issue("F3: Milestone and issues",
        "Find or create milestone 'Sprint 42' in this project. Create 2 issues: "
        "'Refactor auth middleware' and 'Add rate limiting'. Assign both to Sprint 42.")

    print("\n--- G: Epics ---")

    results["G1"] = trigger_issue("G1: Create epic",
        "Create an epic called 'Platform Modernization' in the gbtest group "
        "with description 'Multi-quarter initiative to modernize our platform stack'.")

    results["G2"] = trigger_issue("G2: Issues and add to epic",
        "Create 3 issues: 'Migrate to Kubernetes', 'Implement service mesh', "
        "'Set up observability stack'. Then find the 'Platform Modernization' epic "
        "in the gbtest group and add all 3 issues to it.")

    print("\n--- H: Iterations ---")

    results["H1"] = trigger_issue("H1: Create iteration cadence",
        "Create an iteration cadence called 'Bi-weekly Sprints' in the gbtest group "
        "with 2-week duration, starting 2026-04-01, auto-creating 3 iterations in advance.")

    results["H2"] = trigger_issue("H2: Assign iterations",
        "List the iterations in the gbtest group. If any exist, create 2 issues: "
        "'Sprint task alpha' and 'Sprint task beta', and assign the first available "
        "iteration to both. Post the results as a comment.")

    print("\n--- I: CI/CD ---")

    results["I1"] = trigger_issue("I1: List pipelines",
        "List the 5 most recent pipelines in this project and post their statuses "
        "as a comment on this issue.")

    results["I2"] = trigger_issue("I2: Pipeline details and job logs",
        "Find the most recent pipeline in this project. List its jobs and their statuses. "
        "If any job failed, show the last 20 lines of its log. Post everything as a comment.")

    results["I3"] = trigger_issue("I3: Trigger pipeline",
        "Trigger a new pipeline on the main branch of this project. "
        "Post the pipeline ID and URL as a comment.")

    print("\n--- J: Wiki ---")

    results["J1"] = trigger_issue("J1: Create wiki page",
        "Create a wiki page titled 'Architecture Overview' with content describing "
        "a 3-tier architecture: frontend (React), API (Python FastAPI), database (PostgreSQL). "
        "Include a section for each tier with 2-3 bullet points.")

    print("\n--- K: Labels ---")

    results["K1"] = trigger_issue("K1: Create labels",
        "Create these exact labels: 'priority::critical' (color #FF0000, description 'Showstopper'), "
        "'priority::high' (color #FF6600, description 'Important'), "
        "'priority::low' (color #00CC00, description 'Nice to have').")

    print("\n--- L: Security ---")

    results["L1"] = trigger_issue("L1: List vulnerabilities",
        "Check for vulnerabilities in this project and post a summary as a comment. "
        "If none are found, say so.")

    print("\n--- M: Clarification & Discussion ---")

    results["M1"] = trigger_issue("M1: Vague issue",
        "Fix the performance issue.")

    results["M2"] = trigger_issue("M2: Architecture discussion",
        "We're debating whether to use Redis or Memcached for our session store. "
        "We need 10K concurrent sessions, 30-minute TTL, and individual session invalidation. "
        "Post your recommendation as a comment with pros/cons.")

    results["M3"] = trigger_issue("M3: Non-actionable issue",
        "Meeting notes 2026-03-29: Discussed roadmap priorities for Q2. "
        "Action items: Alice will draft the API spec, Bob will review the infra budget.")

    print("\n--- N: Complex Multi-Step ---")

    results["N1"] = trigger_issue("N1: Full project setup",
        "Set up a new project ecosystem:\n"
        "1. Create a group called weatherapp under gbtest (use parent_path 'gbtest')\n"
        "2. Create 2 projects in weatherapp (use namespace_path 'gbtest/weatherapp'): "
        "weather-api (description: Backend REST API, initialize with README) and "
        "weather-frontend (description: React frontend, initialize with README)\n"
        "3. In weather-api, create 3 issues: 'Implement /forecast endpoint' (label: feature), "
        "'Add authentication middleware' (label: feature), 'Write API documentation' (label: docs)\n"
        "4. In weather-frontend, create 3 issues: 'Build dashboard layout' (label: feature), "
        "'Implement weather map widget' (label: feature), 'Add unit tests' (label: testing)\n"
        "5. Create group milestone 'MVP Release' in weatherapp with due date 2026-06-01\n"
        "6. Create epic 'Weather App MVP' in weatherapp group")

    results["N2"] = trigger_issue("N2: Multi-file + MR + reviewer",
        "Create branch feature/user-model. Commit 3 files in one commit:\n"
        "- models/user.py: Python dataclass with fields id(int), username(str), email(str), created_at(datetime)\n"
        "- tests/test_user.py: pytest tests for User model (test creation, test email format)\n"
        "- models/__init__.py: exports the User class\n"
        "Open MR titled 'Add User model with tests' and assign @altanc as reviewer.")

    results["N3"] = trigger_issue("N3: Cross-project issue management",
        "Create 2 issues: 'API: Add /users endpoint' (labels: backend, feature) and "
        "'Frontend: Add user list page' (labels: frontend, feature). "
        "Link second as blocked_by first. Create milestone 'User Management Phase 1' "
        "with due date 2026-05-15. Assign both issues to it.")

    return results


def wait_for_completion(max_wait=900):
    print(f"\nWaiting for workflows to complete (max {max_wait}s)...")
    start = time.time()
    last_running = 99
    while time.time() - start < max_wait:
        try:
            req = urllib.request.Request(f"{BOT_URL}/admin/api/current")
            resp = urllib.request.urlopen(req, context=ctx)
            current = json.loads(resp.read())
            running = len(current)
            elapsed = int(time.time() - start)
            if running != last_running:
                print(f"  {running} running... ({elapsed}s)")
                last_running = running
            if running == 0 and elapsed > 60:
                print(f"  All complete ({elapsed}s)")
                return True
        except Exception:
            pass
        time.sleep(10)
    print(f"  Timed out after {max_wait}s")
    return False


def collect_results(test_iids):
    try:
        req = urllib.request.Request(f"{BOT_URL}/admin/api/workflows?limit=50")
        resp = urllib.request.urlopen(req, context=ctx)
        workflows = json.loads(resp.read())
    except Exception:
        workflows = []

    print(f"\n{'Test':<6} {'Status':<12} {'Steps':<8} {'Time':<8} {'Esc':<4} Issue")
    print("-" * 65)
    pass_count = 0
    fail_count = 0
    for test_id, iid in sorted(test_iids.items()):
        target = f"Issue #{iid}"
        wf = next((w for w in workflows if w["target"] == target), None)
        if wf:
            status = wf["status"]
            steps = f"{wf['completed_steps']}/{wf['plan_steps']}"
            elapsed = f"{wf['elapsed_seconds']}s"
            esc = str(wf.get("escalations", 0))
            if status == "completed":
                pass_count += 1
            else:
                fail_count += 1
        else:
            status = "not found"
            steps = "-"
            elapsed = "-"
            esc = "-"
            fail_count += 1
        print(f"{test_id:<6} {status:<12} {steps:<8} {elapsed:<8} {esc:<4} #{iid}")

    print(f"\nTotal: {len(test_iids)} | Completed: {pass_count} | Failed/Missing: {fail_count}")


def main():
    print(f"GitBot Full Test Suite — {datetime.now().isoformat()}")
    print(f"  GitLab: {GITLAB_URL} | Bot: {BOT_URL} | Project: {PROJECT_ID}")

    try:
        req = urllib.request.Request(f"{BOT_URL}/health")
        resp = urllib.request.urlopen(req, context=ctx)
        health = json.loads(resp.read())
        print(f"  Version: {health['version']}")
    except Exception as e:
        print(f"  Bot not reachable: {e}")
        sys.exit(1)

    # Prep: create dev2026 branch if needed
    try:
        api_post(f"/projects/{PROJECT_ID}/repository/branches",
                 {"branch": "dev2026", "ref": "main"})
        print("  Created dev2026 branch")
    except Exception:
        print("  dev2026 branch exists")

    test_iids = run_all()

    wait_for_completion(max_wait=1200)  # 20 min for 36 tests
    collect_results(test_iids)

    print(f"\nDone at {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
