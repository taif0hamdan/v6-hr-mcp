"""
HR Backend FastAPI app - independent of the MCP server in main.py. Run with:
    uvicorn hr_backend.app:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

import init_hr_backend_db
from hr_backend.background import roster_refresh_loop
from hr_backend.config import get_settings
from hr_backend.routers import admin, auth, committees, divisions, evaluations, reports, results, sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_hr_backend_db.init_db()

    stop_event = asyncio.Event()
    background_task = None
    if get_settings().cache.background_refresh_enabled:
        background_task = asyncio.create_task(roster_refresh_loop(stop_event))
        logger.info("Roster background refresh loop started.")
    else:
        logger.info("Roster background refresh loop disabled (CACHE_BACKGROUND_REFRESH_ENABLED=false).")

    try:
        yield
    finally:
        if background_task is not None:
            stop_event.set()
            background_task.cancel()
            try:
                await background_task
            except asyncio.CancelledError:
                pass
            logger.info("Roster background refresh loop stopped.")


app = FastAPI(title="HR Backend", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(divisions.router)
app.include_router(admin.router)
app.include_router(committees.router)
app.include_router(sessions.router)
app.include_router(evaluations.router)
app.include_router(results.router)
app.include_router(reports.router)


@app.get("/")
def root() -> dict:
    return {"status": "ok", "service": "hr-backend"}
