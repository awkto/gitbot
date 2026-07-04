"""Live integration tests (github/gitbot#11): drive a running GitBot through
real GitLab events and assert on the artifacts it produces.

These run against a REAL GitLab instance and a REAL GitBot (LLM spend: roughly
$0.2–0.8 per full run) — they are skipped unless explicitly armed:

    GITBOT_IT_TOKEN=<token>   # a HUMAN/admin token, NOT the bot's own token
                              # (the bot drops its own events as self-triggered)
    GITBOT_IT_GITLAB_URL=https://gitlab.dnsif.ca     (default)
    GITBOT_IT_PROJECT=185                            (default: gbtest/gitbot-dev-test)
    GITBOT_IT_BOT_ID=14        # bot user id to assign issues to (default)
    GITBOT_IT_BOT=gitbot       # bot username (default)

Run:  pytest tests/integration -v

The target project must have a webhook pointed at the GitBot under test with
Issues/MR/Notes events and the right secret. Fixtures clean up after
themselves (branches, MRs, issues closed).
"""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("GITBOT_IT_TOKEN"),
    reason="live integration tests need GITBOT_IT_TOKEN (and cost real money)",
)

GITLAB_URL = os.environ.get("GITBOT_IT_GITLAB_URL", "https://gitlab.dnsif.ca")
PROJECT_ID = int(os.environ.get("GITBOT_IT_PROJECT", "185"))
BOT_ID = int(os.environ.get("GITBOT_IT_BOT_ID", "14"))
BOT = os.environ.get("GITBOT_IT_BOT", "gitbot")

IMPLEMENT_TIMEOUT = 360   # clone + SDK loop + MR
MENTION_TIMEOUT = 180


@pytest.fixture(scope="module")
def project():
    import gitlab

    gl = gitlab.Gitlab(GITLAB_URL, private_token=os.environ["GITBOT_IT_TOKEN"])
    return gl.projects.get(PROJECT_ID)


def _wait(what, timeout, poll, interval=10):
    """Poll `poll()` until it returns truthy or timeout; returns the value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = poll()
        if v:
            return v
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def _bot_notes(target):
    target = target.manager.get(target.get_id())  # refresh
    return [n for n in target.notes.list(get_all=True)
            if n.author.get("username") == BOT and not n.system]


# ---------------------------------------------------------------------------
# Scenario 1: issue assignment → implement → branch + MR with the change
# ---------------------------------------------------------------------------

def test_implement_issue_produces_mr(project):
    marker = f"it-{int(time.time())}"
    issue = project.issues.create({
        "title": f"Add {marker}.md ({marker})",
        "description": (
            f"Create a new file named exactly `{marker}.md` at the repository "
            f"root containing the single line `integration test {marker}`. "
            "No other changes."),
        "assignee_ids": [BOT_ID],
    })
    branch = f"gitbot/issue-{issue.iid}"
    try:
        def find_mr():
            mrs = project.mergerequests.list(
                source_branch=branch, state="opened", get_all=True)
            return mrs[0] if mrs else None

        mr = _wait(f"MR from {branch}", IMPLEMENT_TIMEOUT, find_mr)
        commits = list(mr.commits())
        assert commits, "MR exists but has no commits"
        diff = "\n".join(c.get("new_path", "") for c in mr.changes()["changes"])
        assert f"{marker}.md" in diff, f"MR does not touch {marker}.md: {diff}"
        # The finish gate should have reported success on the issue
        _wait("bot success note", 60,
              lambda: any("white_check_mark" in n.body or "Implemented in MR"
                          in n.body for n in _bot_notes(issue)))
    finally:
        for mr in project.mergerequests.list(source_branch=branch, get_all=True):
            mr.state_event = "close"
            mr.save()
        try:
            project.branches.delete(branch)
        except Exception:
            pass
        issue.state_event = "close"
        issue.save()


# ---------------------------------------------------------------------------
# Scenario 2: @mention question → threaded answer, no labels touched
# ---------------------------------------------------------------------------

def test_mention_gets_answer(project):
    marker = f"it-{int(time.time())}"
    issue = project.issues.create({
        "title": f"Question thread ({marker})",
        "description": "Host issue for a mention integration test.",
    })
    try:
        before = len(_bot_notes(issue))
        issue.notes.create({
            "body": f"@{BOT} what is the default branch of this project? "
                    "One-line answer please."})

        def answered():
            # The session's anchor note starts as a "thinking..." placeholder
            # and is edited in place — wait for the real content, not the shell.
            notes = _bot_notes(issue)
            if len(notes) > before and "thinking" not in notes[-1].body:
                return notes[-1].body
            return None

        answer = _wait("bot answer", MENTION_TIMEOUT, answered).lower()
        assert "main" in answer, f"expected the default branch in: {answer[:200]}"
        issue = project.issues.get(issue.iid)
        assert not [l for l in (issue.labels or []) if l.startswith("gitbot::")], \
            "answer session must not leave gitbot:: labels"
    finally:
        issue.state_event = "close"
        issue.save()


# ---------------------------------------------------------------------------
# Scenario 3 (#26): task comment on an MR → commits pushed to THAT MR
# ---------------------------------------------------------------------------

def test_mr_comment_pushes_to_mr_branch(project):
    marker = f"it-{int(time.time())}"
    branch = f"it-mrfix-{marker}"
    project.branches.create({"branch": branch, "ref": "main"})
    project.commits.create({
        "branch": branch,
        "commit_message": f"seed {marker}",
        "actions": [{"action": "create", "file_path": f"{marker}.py",
                     "content": "def f():\n    return 'velue'\n"}],
    })
    mr = project.mergerequests.create({
        "source_branch": branch, "target_branch": "main",
        "title": f"IT seed MR ({marker})"})
    pre_sha = project.branches.get(branch).commit["id"]
    open_before = {m.iid for m in project.mergerequests.list(
        state="opened", get_all=True)}
    try:
        mr.notes.create({"body": f"@{BOT} `{marker}.py` returns the typo "
                                 "'velue' — change it to 'value' in this MR."})

        def new_commit():
            head = project.branches.get(branch).commit["id"]
            return head if head != pre_sha else None

        _wait("push to MR source branch", IMPLEMENT_TIMEOUT, new_commit)
        content = project.files.get(f"{marker}.py", ref=branch).decode().decode()
        assert "'value'" in content, f"typo not fixed: {content}"
        open_after = {m.iid for m in project.mergerequests.list(
            state="opened", get_all=True)}
        assert open_after - open_before == set(), \
            f"MR-update must not open a new MR (new: {open_after - open_before})"
    finally:
        mr = project.mergerequests.get(mr.iid)
        mr.state_event = "close"
        mr.save()
        try:
            project.branches.delete(branch)
        except Exception:
            pass
