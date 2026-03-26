"""FastAPI webhook server."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from gitbot.config import settings
from gitbot.router import route_event
from gitbot.todos import process_pending_todos

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: process any pending todos from before crash/restart
    log.info("Checking for pending todos (crash recovery)...")
    try:
        await process_pending_todos()
    except Exception:
        log.exception("Error processing pending todos on startup")
    yield


app = FastAPI(title="GitBot", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_gitlab_event: str = Header(...),
    x_gitlab_token: str | None = Header(None),
):
    # Verify webhook secret if configured
    if settings.webhook_secret:
        if x_gitlab_token != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook token")

    payload = await request.json()
    log.info("Received event: %s", x_gitlab_event)

    async def _safe_route(event_type, payload):
        try:
            await route_event(event_type, payload)
        except Exception:
            log.exception("Error processing event: %s", event_type)

    asyncio.create_task(_safe_route(x_gitlab_event, payload))

    return {"status": "accepted"}
