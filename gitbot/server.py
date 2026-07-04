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


@app.middleware("http")
async def admin_basic_auth(request: Request, call_next):
    """HTTP Basic auth for the admin panel + API when a password is set.

    Any username is accepted; only the password matters. /webhook and
    /reconcile keep their own secret-header auth."""
    if request.url.path.startswith("/admin") and settings.admin_password:
        import base64
        import secrets as _secrets

        from fastapi.responses import Response

        ok = False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                creds = base64.b64decode(auth[6:]).decode()
                _, _, password = creds.partition(":")
                ok = _secrets.compare_digest(password, settings.admin_password)
            except Exception:
                ok = False
        if not ok:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="GitBot Admin"'},
            )
    return await call_next(request)


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
            # Count it: a nonzero rejected counter is the unambiguous signal
            # that a GitLab hook has a wrong/cleared secret and events are
            # being lost (surfaced as a red badge in the admin panel).
            tracker.webhook_rejected()
            log.warning("Webhook rejected: bad or missing secret (event: %s)",
                        x_gitlab_event)
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
    from gitbot import config as cfg
    stats["config"]["sources"] = cfg.config_sources()
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


# Keys the Configure form may write. Everything else has a dedicated endpoint
# (threshold, models) or is deployment-level (host/port/db path).
_SAVEABLE_KEYS = {
    "gitlab_url", "gitlab_token", "bot_username", "gitlab_ssl_verify",
    "webhook_secret", "anthropic_api_key", "admin_password",
}
_GITLAB_KEYS = {"gitlab_url", "gitlab_token", "bot_username", "gitlab_ssl_verify"}


@app.post("/admin/api/save-config")
async def admin_save_config(request: Request):
    """Persist config to the store (data volume) and apply it live.

    Env-owned keys are refused — real environment variables always win, so a
    store write there would be a silent no-op on restart."""
    _check_admin()
    data = await request.json()
    from gitbot import config as cfg

    updates = {k: v for k, v in data.items() if k in _SAVEABLE_KEYS and v != ""}
    applied, locked = cfg.save_config(updates)
    if _GITLAB_KEYS & set(applied):
        from gitbot import gitlab_client as glc
        glc._gl = None  # drop cached client so the new connection details apply
    msg = []
    if applied:
        msg.append(f"Saved & applied live: {', '.join(sorted(applied))}.")
    if locked:
        msg.append(f"Skipped (owned by environment): {', '.join(sorted(locked))} — "
                   "unset the GITBOT_* env var to manage here.")
    if not msg:
        msg.append("Nothing to save.")
    return {"status": "ok", "applied": applied, "locked": locked, "message": " ".join(msg)}


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
    from gitbot import config as cfg
    applied, locked = cfg.save_config({"question_threshold": val})
    if locked:
        raise HTTPException(status_code=400,
                            detail="question_threshold is set via environment")
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
    from gitbot import config as cfg
    applied, locked = cfg.save_config({f"model_{workflow}": model})
    if locked:
        raise HTTPException(status_code=400,
                            detail=f"model_{workflow} is set via environment")
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


# ---------------------------------------------------------------------------
# Service-account onboarding (issue #36, self-hosted Tier A)
#
# Onboard with ONE admin token: discover groups, then provision GitBot's single
# service account — create/adopt the bot user, mint its day-to-day token, grant
# membership + create the webhook on each selected group. The admin token is
# used transiently and never stored.
# ---------------------------------------------------------------------------

@app.post("/admin/api/onboard/discover")
async def admin_onboard_discover(request: Request):
    _check_admin()
    data = await request.json()
    admin_token = (data.get("admin_token") or "").strip() or None
    our_url = (data.get("webhook_url") or "").strip()
    if not admin_token:
        return JSONResponse({"status": "error", "error": "admin token required"}, status_code=400)
    from gitbot import gitlab_client as glc

    def _discover():
        if not glc.token_is_admin(admin_token):
            return {"error": "not_admin"}
        groups = glc.list_all_groups(admin_token)
        for g in groups:
            try:
                hooks = glc.list_group_hooks(g["id"], admin_token)
                g["enabled"] = any(glc._hook_is_ours(h["url"], our_url) for h in hooks)
            except Exception:
                g["enabled"] = False
        groups.sort(key=lambda x: x["full_path"])
        return {"groups": groups}

    try:
        r = await asyncio.to_thread(_discover)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    if r.get("error") == "not_admin":
        return JSONResponse(
            {"status": "error", "error": "This token is not an instance admin. "
             "Creating a service account needs an admin token — or create the bot "
             "user manually and enter its token instead."}, status_code=400)
    return {"status": "ok", "groups": r["groups"],
            "webhook_secret_set": bool(settings.webhook_secret)}


@app.post("/admin/api/onboard/provision")
async def admin_onboard_provision(request: Request):
    _check_admin()
    data = await request.json()
    admin_token = (data.get("admin_token") or "").strip() or None
    sa_name = (data.get("sa_name") or "").strip()            # desired username
    display_name = (data.get("display_name") or sa_name or "GitBot").strip()
    group_ids = data.get("group_ids") or []
    role = int(data.get("access_level") or 40)               # 40 = Maintainer
    our_url = (data.get("webhook_url") or "").strip()
    apply_identity = bool(data.get("apply_identity"))
    if not admin_token or not sa_name:
        return JSONResponse({"status": "error", "error": "admin_token and sa_name required"},
                            status_code=400)
    if group_ids and our_url and not settings.webhook_secret:
        return JSONResponse({"status": "error", "error": _GROUPS_SECRET_MSG}, status_code=400)
    from gitbot import gitlab_client as glc

    def _provision():
        if not glc.token_is_admin(admin_token):
            return {"error": "This token is not an instance admin."}
        # Resolve the bot user: adopt only if it already exists AND is a service account.
        existing = glc.find_user_by_username(sa_name, admin_token)
        if existing:
            if not glc.is_service_account(existing["id"], admin_token):
                return {"error": f"User '{sa_name}' exists but is not a service account — "
                                 "choose another name."}
            user_id = existing["id"]
            adopted = True
        else:
            sa = glc.create_service_account(display_name, sa_name, admin_token)
            user_id = sa["id"]
            adopted = False
        # Mint the day-to-day token.
        tok = glc.create_user_token(user_id, "gitbot", admin_token)
        # Membership + webhook per selected group.
        groups_out = []
        for gid in group_ids:
            gr = {"group_id": gid}
            try:
                gr["membership"] = glc.ensure_group_membership(gid, user_id, role, admin_token)
            except Exception as e:
                gr["membership_error"] = str(e)[:140]
            if our_url and settings.webhook_secret:
                try:
                    hooks = glc.list_group_hooks(gid, admin_token)
                    ex = [h for h in hooks if glc._hook_is_ours(h["url"], our_url)]
                    if ex:
                        gr["hook"] = "exists"; gr["hook_id"] = ex[0]["id"]
                    else:
                        gr["hook_id"] = glc.create_group_hook(
                            gid, our_url, settings.webhook_secret, admin_token)
                        gr["hook"] = "created"
                except Exception as e:
                    gr["hook_error"] = str(e)[:140]
            groups_out.append(gr)
        return {"user_id": user_id, "username": sa_name, "adopted": adopted,
                "token": tok["token"], "expires_at": tok.get("expires_at"),
                "groups": groups_out}

    try:
        r = await asyncio.to_thread(_provision)
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    if r.get("error"):
        return JSONResponse({"status": "error", "error": r["error"]}, status_code=400)

    # Record managed hooks (main thread — sqlite is thread-bound).
    from gitbot import state
    for gr in r["groups"]:
        if gr.get("hook_id"):
            state.record_managed_hook(gr["group_id"], "", gr["hook_id"], our_url)

    # Optionally switch the running GitBot to act as the new identity now.
    # Persisted to the config store when env doesn't own the keys; env-owned
    # keys apply live but revert on restart (reported via identity_persisted).
    if apply_identity:
        from gitbot import config as cfg
        from gitbot import gitlab_client as glc
        applied, locked = cfg.save_config(
            {"gitlab_token": r["token"], "bot_username": r["username"]})
        settings.gitlab_token = r["token"]
        settings.bot_username = r["username"]
        glc._gl = None  # drop cached client so the new token is used immediately
        r["applied"] = True
        r["identity_persisted"] = not locked
        r["identity_locked_keys"] = locked
        log.info("GitBot identity switched to @%s (id %s) via onboarding%s",
                 r["username"], r["user_id"],
                 "" if not locked else f" (env-owned, not persisted: {locked})")
    r["status"] = "ok"
    return r
