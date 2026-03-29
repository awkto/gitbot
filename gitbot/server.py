"""FastAPI webhook server + admin panel."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from gitbot.config import settings
from gitbot.models import Family
from gitbot.router import route_event
from gitbot.todos import process_pending_todos
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
        log.info("GitLab: %s as @%s | LLM: %s",
                 settings.gitlab_url, settings.bot_username, settings.get_llm_family())

    log.info("Checking for pending todos (crash recovery)...")
    try:
        await process_pending_todos()
    except Exception:
        log.exception("Error processing pending todos on startup")
    yield


app = FastAPI(title="GitBot", version=APP_VERSION, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION, "admin": settings.admin_enabled}


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
        "llm_family": str(settings.get_llm_family()),
        "llm_family_explicit": settings.llm_family is not None,
        "gitlab_connected": bool(settings.gitlab_token),
        "llm_configured": bool(settings.llm_api_key or settings.get_llm_family() == Family.CLAUDE_CODE),
        "setup_needed": settings.setup_needed,
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
        "llm_family": "GITBOT_LLM_FAMILY",
        "llm_api_key": "GITBOT_LLM_API_KEY",
        "admin_enabled": "GITBOT_ADMIN_ENABLED",
    }
    for field, env_var in field_map.items():
        if field in data and data[field] != "":
            lines.append(f"{env_var}={data[field]}")

    env_content = "\n".join(lines) + "\n"
    Path(".env").write_text(env_content)

    return {"status": "ok", "message": "Config saved. Restart the container to apply changes."}


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
        from gitbot import llm
        from gitbot.models import Task
        result = await llm.complete(Task.TRIAGE, system="Say OK", prompt="Test")
        return {"status": "ok", "model": settings.get_llm_family(), "response": result[:100]}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
