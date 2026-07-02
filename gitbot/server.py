"""FastAPI webhook server + admin panel."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from gitbot.config import settings
from gitbot.router import route_event
from gitbot.todos import process_pending_todos, resume_incomplete_work
from gitbot.activity import tracker

def _read_version() -> str:
    try:
        return Path(__file__).parent.parent.joinpath("version").read_text().strip()
    except Exception:
        return "dev"

APP_VERSION = _read_version()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from gitbot.config import ensure_env_file
    ensure_env_file()

    if settings.setup_needed:
        log.warning("GitBot is not configured. Visit /admin to set up.")
    else:
        log.info("GitLab: %s as @%s | classifier: %s",
                 settings.gitlab_url, settings.bot_username, settings.classifier_model)

    log.info("Checking for pending todos (crash recovery)...")
    try:
        await process_pending_todos()
    except Exception:
        log.exception("Error processing pending todos on startup")

    log.info("Checking for interrupted work to resume...")
    # Run in background so the server starts accepting requests immediately
    async def _resume_in_background():
        try:
            await resume_incomplete_work()
        except Exception:
            log.exception("Error resuming incomplete work on startup")

    asyncio.create_task(_resume_in_background())

    # Periodic reconciliation: picks up orphaned and parked (gitbot::waiting)
    # work — the issue thread is the durable state, this sweep is the resume.
    async def _reconcile_loop():
        from gitbot.todos import reconcile
        while True:
            await asyncio.sleep(settings.reconcile_minutes * 60)
            try:
                await reconcile()
            except Exception:
                log.exception("Reconcile sweep failed")

    if settings.reconcile_minutes > 0:
        asyncio.create_task(_reconcile_loop())
        log.info("Reconciliation sweep every %d min", settings.reconcile_minutes)
    yield


app = FastAPI(title="GitBot", version=APP_VERSION, lifespan=lifespan)


@app.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin")


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION, "admin": settings.admin_enabled}


@app.post("/reconcile")
async def reconcile_now(x_gitlab_token: str | None = Header(None)):
    """External trigger for the reconciliation sweep (e.g. a scheduled CI job
    or cron). Authenticated with the same secret as the webhook."""
    if settings.webhook_secret and x_gitlab_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid token")
    from gitbot.todos import reconcile

    async def _safe():
        try:
            await reconcile()
        except Exception:
            log.exception("Reconcile (external trigger) failed")

    asyncio.create_task(_safe())
    return {"status": "reconciling"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_gitlab_event: str = Header(...),
    x_gitlab_token: str | None = Header(None),
):
    if settings.webhook_secret:
        if x_gitlab_token != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook token")

    payload = await request.json()
    log.info("Received event: %s", x_gitlab_event)
    tracker.webhook_received()

    async def _safe_route(event_type, payload):
        try:
            await route_event(event_type, payload)
        except Exception:
            log.exception("Error processing event: %s", event_type)

    asyncio.create_task(_safe_route(x_gitlab_event, payload))

    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def _check_admin():
    if not settings.admin_enabled:
        raise HTTPException(status_code=404)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    _check_admin()
    html_path = Path(__file__).parent / "admin.html"
    return HTMLResponse(html_path.read_text())


@app.get("/admin/api/stats")
async def admin_stats():
    _check_admin()
    stats = tracker.get_stats()
    stats["version"] = APP_VERSION
    stats["config"] = {
        "gitlab_url": settings.gitlab_url,
        "bot_username": settings.bot_username,
        "gitlab_connected": bool(settings.gitlab_token),
        "llm_configured": bool(settings.anthropic_api_key),
        "setup_needed": settings.setup_needed,
        "question_threshold": settings.question_threshold,
        "workflow_models": {
            "mention": settings.model_mention,
            "implement": settings.model_implement,
            "orchestrate": settings.model_orchestrate,
            "review": settings.model_review,
        },
    }
    return stats


@app.get("/admin/api/events")
async def admin_events(limit: int = 50):
    _check_admin()
    return tracker.get_events(limit)


@app.get("/admin/api/workflows")
async def admin_workflows(limit: int = 20):
    _check_admin()
    return tracker.get_workflows(limit)


@app.get("/admin/api/current")
async def admin_current():
    _check_admin()
    return tracker.get_current()


@app.get("/admin/api/debug/{workflow_id}")
async def admin_debug_log(workflow_id: str):
    _check_admin()
    debug_log = tracker.get_debug_log(workflow_id)
    if debug_log is None:
        raise HTTPException(status_code=404, detail="No debug log for this workflow")
    return {"workflow_id": workflow_id, "debug_log": debug_log}


@app.post("/admin/api/save-config")
async def admin_save_config(request: Request):
    _check_admin()
    data = await request.json()

    # Build env file content from provided fields
    lines = []
    field_map = {
        "gitlab_url": "GITBOT_GITLAB_URL",
        "gitlab_token": "GITBOT_GITLAB_TOKEN",
        "bot_username": "GITBOT_BOT_USERNAME",
        "gitlab_ssl_verify": "GITBOT_GITLAB_SSL_VERIFY",
        "webhook_secret": "GITBOT_WEBHOOK_SECRET",
        "anthropic_api_key": "GITBOT_ANTHROPIC_API_KEY",
        "admin_enabled": "GITBOT_ADMIN_ENABLED",
    }
    for field, env_var in field_map.items():
        if field in data and data[field] != "":
            lines.append(f"{env_var}={data[field]}")

    env_content = "\n".join(lines) + "\n"
    Path(".env").write_text(env_content)

    return {"status": "ok", "message": "Config saved. Restart the container to apply changes."}


@app.post("/admin/api/threshold")
async def admin_set_threshold(request: Request):
    """Live-tune how important a question must be (1-10) before the agent
    asks the user instead of assuming. Takes effect for new sessions."""
    _check_admin()
    data = await request.json()
    try:
        val = int(data.get("question_threshold"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="question_threshold must be an integer")
    if not 1 <= val <= 10:
        raise HTTPException(status_code=400,
                            detail="question_threshold must be 1-10")
    settings.question_threshold = val
    log.info("Question threshold set to %d via admin panel", val)
    return {"status": "ok", "question_threshold": val}


_MODEL_VALUE_RE = r"^(auto|haiku|sonnet|opus|claude-[a-z0-9.-]+)$"


@app.post("/admin/api/models")
async def admin_set_workflow_model(request: Request):
    """Per-workflow model override. "auto" = harness decides from the triage
    complexity score; "haiku"/"sonnet"/"opus" = current model of that tier
    (SDK alias, never goes stale); or a pinned id like "claude-opus-4-8".
    Takes effect for new sessions."""
    import re as _re

    _check_admin()
    data = await request.json()
    workflow = str(data.get("workflow", ""))
    model = str(data.get("model", "")).strip()
    if workflow not in ("mention", "implement", "orchestrate", "review"):
        raise HTTPException(status_code=400, detail="unknown workflow")
    if not _re.match(_MODEL_VALUE_RE, model):
        raise HTTPException(
            status_code=400,
            detail="model must be auto, haiku, sonnet, opus, or a claude-* id")
    setattr(settings, f"model_{workflow}", model)
    log.info("Workflow model set via admin panel: %s -> %s", workflow, model)
    return {"status": "ok", "workflow": workflow, "model": model}


@app.post("/admin/api/test-gitlab")
async def admin_test_gitlab():
    _check_admin()
    try:
        from gitbot import gitlab_client as glc
        gl = glc.get_client()
        user = gl.auth()
        return {"status": "ok", "user": gl.user.username, "url": settings.gitlab_url}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)


@app.post("/admin/api/test-llm")
async def admin_test_llm():
    _check_admin()
    try:
        from gitbot.engine_sdk import _classify_complete
        result = await _classify_complete(system="Say OK", prompt="Test")
        return {"status": "ok", "model": settings.classifier_model, "response": result[:100]}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)


# ---------------------------------------------------------------------------
# Group webhook management (onboarding — issue #35)
#
# Discover the groups a token owns, then turn GitBot on/off per group. GitBot
# creates and owns the group webhook, using the instance webhook secret so
# inbound events validate with no other changes. Group hooks we create cover
# all descendant projects; the overlap guard prevents double-delivery (#17).
# ---------------------------------------------------------------------------

@app.post("/admin/api/groups/list")
async def admin_groups_list(request: Request):
    _check_admin()
    data = await request.json()
    token = (data.get("token") or "").strip() or None
    gitlab_url = (data.get("gitlab_url") or "").strip() or None
    our_url = (data.get("webhook_url") or "").strip()
    from gitbot import gitlab_client as glc

    try:
        groups = glc.list_owned_groups(token, gitlab_url)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)

    # Which groups currently carry our hook (live check).
    hook_ids: dict[int, int | None] = {}
    for g in groups:
        try:
            hooks = glc.list_group_hooks(g["id"], token)
            hook_ids[g["id"]] = next(
                (h["id"] for h in hooks if glc._hook_is_ours(h["url"], our_url)), None
            )
        except Exception:
            hook_ids[g["id"]] = None

    by_id = {g["id"]: g for g in groups}

    def covered_by(g: dict) -> str | None:
        pid = g.get("parent_id")
        while pid and pid in by_id:
            if hook_ids.get(pid):
                return by_id[pid]["full_path"]
            pid = by_id[pid].get("parent_id")
        return None

    result = [
        {**g, "enabled": hook_ids.get(g["id"]) is not None,
         "hook_id": hook_ids.get(g["id"]), "covered_by": covered_by(g)}
        for g in groups
    ]
    result.sort(key=lambda x: x["full_path"])
    return {"status": "ok", "groups": result,
            "webhook_secret_set": bool(settings.webhook_secret)}


_GROUPS_SECRET_MSG = ("Set a webhook secret (Configure) before enabling groups — "
                      "otherwise inbound events would be unauthenticated.")
_GROUPS_COVERED_MSG = ("A parent group already has GitBot enabled — enabling this "
                       "subgroup would double-fire.")
_GROUPS_FORBIDDEN_MSG = ("GitLab denied hook creation (403). Managing a group's "
                         "webhooks requires Owner on that group.")


# python-gitlab is blocking; these sync helpers run via asyncio.to_thread so a
# multi-second scan never stalls the event loop (or the admin page's polling).

def _groups_scan_sync(group_id: int, our_url: str, token: str | None) -> dict:
    """Idempotency + ancestor-coverage + project-hook overlap check."""
    from gitbot import gitlab_client as glc
    existing = [h for h in glc.list_group_hooks(group_id, token)
                if glc._hook_is_ours(h["url"], our_url)]
    if existing:
        return {"status": "already", "hook_id": existing[0]["id"]}
    # Ancestor coverage — tolerate parents we can't read (a hook we can't see
    # isn't one we created, so it can't be covering us).
    try:
        pid = glc.group_parent_id(group_id, token)
    except Exception:
        pid = None
    while pid:
        try:
            if any(glc._hook_is_ours(h["url"], our_url)
                   for h in glc.list_group_hooks(pid, token)):
                return {"status": "covered"}
        except Exception:
            pass
        try:
            pid = glc.group_parent_id(pid, token)
        except Exception:
            break
    scan = glc.scan_project_hook_overlaps(group_id, our_url, token)
    return {"status": "ok", "overlaps": scan["overlaps"],
            "scanned": scan["scanned"], "truncated": scan["truncated"]}


def _groups_create_sync(group_id, our_url, token, cleanup, overlaps) -> dict:
    """Clean any provided overlapping project hooks and create the group hook.

    GitLab I/O only — the DB record is written by the caller on the main thread
    (sqlite connections are thread-bound)."""
    from gitbot import gitlab_client as glc
    removed = 0
    if cleanup and overlaps:
        for o in overlaps:
            try:
                glc.delete_project_hook(o["project_id"], o["hook_id"], token)
                removed += 1
            except Exception:
                log.exception("Failed to remove overlapping hook on %s", o.get("path"))
    try:
        hook_id = glc.create_group_hook(group_id, our_url, settings.webhook_secret, token)
    except Exception as e:
        if "403" in str(e):
            return {"status": "forbidden"}
        raise
    return {"status": "ok", "hook_id": hook_id, "removed": removed}


@app.post("/admin/api/groups/scan")
async def admin_groups_scan(request: Request):
    """Phase 1 of enabling: idempotency + coverage + overlap check. Blocking
    work runs off the event loop; the UI shows this as 'Checking projects…'."""
    _check_admin()
    data = await request.json()
    token = (data.get("token") or "").strip() or None
    our_url = (data.get("webhook_url") or "").strip()
    group_id = data.get("group_id")
    if not our_url or group_id is None:
        raise HTTPException(status_code=400, detail="group_id and webhook_url required")
    if not settings.webhook_secret:
        return JSONResponse({"status": "error", "error": _GROUPS_SECRET_MSG}, status_code=400)
    try:
        result = await asyncio.to_thread(_groups_scan_sync, group_id, our_url, token)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    # Adopting an existing hook keeps our record in sync (drift repair).
    if result.get("status") == "already":
        from gitbot import state
        state.record_managed_hook(group_id, data.get("full_path", ""), result["hook_id"], our_url)
    return result


@app.post("/admin/api/groups/create")
async def admin_groups_create(request: Request):
    """Phase 2 of enabling: create the group hook (optionally cleaning the
    overlaps the scan returned). The UI shows this as 'Creating group trigger…'."""
    _check_admin()
    data = await request.json()
    token = (data.get("token") or "").strip() or None
    our_url = (data.get("webhook_url") or "").strip()
    full_path = data.get("full_path", "")
    group_id = data.get("group_id")
    cleanup = bool(data.get("cleanup"))
    overlaps = data.get("overlaps") or []
    if not our_url or group_id is None:
        raise HTTPException(status_code=400, detail="group_id and webhook_url required")
    if not settings.webhook_secret:
        return JSONResponse({"status": "error", "error": _GROUPS_SECRET_MSG}, status_code=400)
    try:
        r = await asyncio.to_thread(_groups_create_sync, group_id,
                                    our_url, token, cleanup, overlaps)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    if r["status"] == "forbidden":
        return JSONResponse({"status": "error", "error": _GROUPS_FORBIDDEN_MSG}, status_code=400)
    from gitbot import state
    state.record_managed_hook(group_id, full_path, r["hook_id"], our_url)
    log.info("Enabled GitBot on group %s (hook %s, removed %d overlaps)",
             full_path, r["hook_id"], r["removed"])
    msg = ("Group trigger created" + (f" (removed {r['removed']} overlapping hook(s))"
                                      if r["removed"] else ""))
    return {"status": "ok", "hook_id": r["hook_id"], "message": msg}


@app.post("/admin/api/groups/enable")
async def admin_groups_enable(request: Request):
    """All-in-one enable (scan + create). The admin UI drives /scan then /create
    for phased progress; this stays for programmatic callers."""
    _check_admin()
    data = await request.json()
    token = (data.get("token") or "").strip() or None
    our_url = (data.get("webhook_url") or "").strip()
    full_path = data.get("full_path", "")
    group_id = data.get("group_id")
    cleanup = bool(data.get("cleanup"))
    if not our_url or group_id is None:
        raise HTTPException(status_code=400, detail="group_id and webhook_url required")
    if not settings.webhook_secret:
        return JSONResponse({"status": "error", "error": _GROUPS_SECRET_MSG}, status_code=400)
    try:
        from gitbot import state
        scan = await asyncio.to_thread(_groups_scan_sync, group_id, our_url, token)
        if scan["status"] == "already":
            state.record_managed_hook(group_id, full_path, scan["hook_id"], our_url)
            return {"status": "ok", "message": "Already enabled", "hook_id": scan["hook_id"]}
        if scan["status"] == "covered":
            return JSONResponse({"status": "error", "error": _GROUPS_COVERED_MSG}, status_code=400)
        if scan["overlaps"] and not cleanup:
            return {"status": "overlap", "overlaps": scan["overlaps"],
                    "scanned": scan["scanned"], "truncated": scan["truncated"],
                    "message": "Overlapping project hooks would double-fire."}
        r = await asyncio.to_thread(_groups_create_sync, group_id,
                                    our_url, token, cleanup, scan["overlaps"])
        if r["status"] == "forbidden":
            return JSONResponse({"status": "error", "error": _GROUPS_FORBIDDEN_MSG}, status_code=400)
        state.record_managed_hook(group_id, full_path, r["hook_id"], our_url)
        return {"status": "ok", "hook_id": r["hook_id"],
                "message": f"Enabled (removed {r['removed']} overlapping hook(s))"}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)


@app.post("/admin/api/groups/disable")
async def admin_groups_disable(request: Request):
    _check_admin()
    data = await request.json()
    token = (data.get("token") or "").strip() or None
    our_url = (data.get("webhook_url") or "").strip()
    group_id = data.get("group_id")
    if group_id is None:
        raise HTTPException(status_code=400, detail="group_id required")

    def _delete_hooks_sync():
        from gitbot import gitlab_client as glc
        removed = 0
        for h in glc.list_group_hooks(group_id, token):
            if glc._hook_is_ours(h["url"], our_url):
                glc.delete_group_hook(group_id, h["id"], token)
                removed += 1
        return removed

    try:
        removed = await asyncio.to_thread(_delete_hooks_sync)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    from gitbot import state
    state.delete_managed_hook(group_id)
    log.info("Disabled GitBot on group %s (%d hook(s) removed)", group_id, removed)
    return {"status": "ok", "message": f"Disabled ({removed} hook(s) removed)"}
