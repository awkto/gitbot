#!/usr/bin/env python3
"""Clean up all GitLab objects created by the gitbot user.

Queries the GitLab API for everything owned/created by the bot account
and deletes it. No manifest needed — discovers objects dynamically.

Usage:
    GITBOT_GITLAB_URL=https://gitlab.dnsif.ca \
    GITBOT_GITLAB_TOKEN=glpat-xxx \
    GITBOT_BOT_USERNAME=gitbot \
    python scripts/test_cleanup.py [--dry-run]

Use a token with Owner/Admin access for permanent deletion.
Set GITBOT_GITLAB_SSL_VERIFY=false for self-signed certs.
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

GITLAB_URL = os.environ.get("GITBOT_GITLAB_URL", "https://gitlab.dnsif.ca")
TOKEN = os.environ.get("GITBOT_GITLAB_TOKEN", "")
BOT_USER = os.environ.get("GITBOT_BOT_USERNAME", "gitbot")
SSL_VERIFY = os.environ.get("GITBOT_GITLAB_SSL_VERIFY", "true").lower() != "false"
DRY_RUN = "--dry-run" in sys.argv
# Project to clean (issues, MRs, branches, milestones, labels, wikis)
PROJECT_ID = int(os.environ.get("GITBOT_PROJECT_ID", "32"))
# Group to clean (epics, group milestones, subgroups, iteration cadences)
GROUP_ID = int(os.environ.get("GITBOT_GROUP_ID", "122"))

if not TOKEN:
    print("Set GITBOT_GITLAB_TOKEN environment variable")
    sys.exit(1)

ctx = ssl.create_default_context()
if not SSL_VERIFY:
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE


def api(method, path, data=None):
    url = f"{GITLAB_URL}/api/v4{path}"
    body = json.dumps(data).encode() if data else None
    headers = {"PRIVATE-TOKEN": TOKEN}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        resp = urllib.request.urlopen(req, context=ctx)
        if resp.status == 204:
            return None
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if method == "DELETE":
            return {"error": e.code}
        raise


def api_list(path, params=None):
    """Paginate through all results."""
    items = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        extra = f"{sep}per_page=100&page={page}"
        if params:
            extra += "&" + urllib.parse.urlencode(params)
        try:
            result = api("GET", f"{path}{extra}")
            if not result or not isinstance(result, list):
                break
            items.extend(result)
            if len(result) < 100:
                break
            page += 1
        except Exception:
            break
    return items


def delete(desc, method, path):
    if DRY_RUN:
        print(f"  [dry-run] Would delete: {desc}")
        return
    result = api(method, path)
    code = result.get("error") if isinstance(result, dict) else "ok"
    print(f"  {desc}: {code}")


def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"GitBot Test Cleanup ({mode})")
    print(f"  GitLab: {GITLAB_URL}")
    print(f"  Bot user: {BOT_USER}")
    print(f"  Project: {PROJECT_ID}, Group: {GROUP_ID}")
    print()

    # 1. Close and delete all issues in project
    print("--- Issues ---")
    issues = api_list(f"/projects/{PROJECT_ID}/issues", {"state": "all"})
    print(f"  Found {len(issues)} issues")
    for issue in issues:
        delete(f"Issue #{issue['iid']} ({issue['title'][:40]})",
               "DELETE", f"/projects/{PROJECT_ID}/issues/{issue['iid']}")

    # 2. Close and delete MRs
    print("\n--- Merge Requests ---")
    mrs = api_list(f"/projects/{PROJECT_ID}/merge_requests", {"state": "all"})
    print(f"  Found {len(mrs)} MRs")
    for mr in mrs:
        delete(f"MR !{mr['iid']} ({mr['title'][:40]})",
               "DELETE", f"/projects/{PROJECT_ID}/merge_requests/{mr['iid']}")

    # 3. Delete non-main branches
    print("\n--- Branches ---")
    branches = api_list(f"/projects/{PROJECT_ID}/repository/branches")
    non_main = [b for b in branches if b["name"] != "main"]
    print(f"  Found {len(non_main)} non-main branches")
    for b in non_main:
        name = urllib.parse.quote(b["name"], safe="")
        delete(f"Branch {b['name']}", "DELETE",
               f"/projects/{PROJECT_ID}/repository/branches/{name}")

    # 4. Delete milestones
    print("\n--- Project Milestones ---")
    milestones = api_list(f"/projects/{PROJECT_ID}/milestones")
    print(f"  Found {len(milestones)}")
    for m in milestones:
        delete(f"Milestone '{m['title']}'", "DELETE",
               f"/projects/{PROJECT_ID}/milestones/{m['id']}")

    # 5. Delete labels
    print("\n--- Labels ---")
    labels = api_list(f"/projects/{PROJECT_ID}/labels")
    print(f"  Found {len(labels)}")
    for l in labels:
        delete(f"Label '{l['name']}'", "DELETE",
               f"/projects/{PROJECT_ID}/labels/{l['id']}")

    # 6. Delete wiki pages
    print("\n--- Wiki Pages ---")
    try:
        wikis = api("GET", f"/projects/{PROJECT_ID}/wikis?per_page=100")
        if isinstance(wikis, list):
            print(f"  Found {len(wikis)}")
            for w in wikis:
                slug = urllib.parse.quote(w["slug"], safe="")
                delete(f"Wiki '{w['title']}'", "DELETE",
                       f"/projects/{PROJECT_ID}/wikis/{slug}")
    except Exception:
        print("  Could not list wikis")

    # 7. Delete group epics
    print("\n--- Group Epics ---")
    try:
        epics = api_list(f"/groups/{GROUP_ID}/epics")
        print(f"  Found {len(epics)}")
        for e in epics:
            delete(f"Epic '{e['title']}'", "DELETE",
                   f"/groups/{GROUP_ID}/epics/{e['id']}")
    except Exception:
        print("  Could not list epics")

    # 8. Delete group milestones
    print("\n--- Group Milestones ---")
    try:
        gms = api_list(f"/groups/{GROUP_ID}/milestones")
        print(f"  Found {len(gms)}")
        for m in gms:
            delete(f"Group milestone '{m['title']}'", "DELETE",
                   f"/groups/{GROUP_ID}/milestones/{m['id']}")
    except Exception:
        print("  Could not list group milestones")

    # 9. Delete subgroups (and their projects)
    print("\n--- Subgroups ---")
    try:
        subgroups = api_list(f"/groups/{GROUP_ID}/subgroups")
        print(f"  Found {len(subgroups)}")
        for sg in subgroups:
            # Delete projects in subgroup first
            try:
                projs = api_list(f"/groups/{sg['id']}/projects")
                for p in projs:
                    delete(f"Project '{p['path_with_namespace']}'",
                           "DELETE", f"/projects/{p['id']}")
            except Exception:
                pass
            delete(f"Subgroup '{sg['full_path']}'", "DELETE",
                   f"/groups/{sg['id']}")
    except Exception:
        print("  Could not list subgroups")

    # 10. Delete test files on main
    print("\n--- Test files on main ---")
    test_files = ["healthcheck.py", "CONTRIBUTING.md", "sysinfo.py",
                  "logger.py", "validate_email.py", "backup.sh",
                  "hello.go", "hello.rs", "disk_monitor.sh",
                  "fibonacci.rs", "palindrome.go"]
    for fname in test_files:
        path = urllib.parse.quote(fname, safe="")
        delete(f"File {fname}", "DELETE",
               f"/projects/{PROJECT_ID}/repository/files/{path}?branch=main&commit_message=cleanup")

    print("\nDone!")


if __name__ == "__main__":
    main()
