"""IG Automation OS — FastAPI entrypoint.

All routes (pages + REST) are mounted under `/api` because the platform
ingress only forwards paths beginning with `/api` to this backend.
"""
import logging
import os
from pathlib import Path

from fastapi import FastAPI, APIRouter
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load env before importing any app modules that read env at import time
# (e.g. automation.browser_session reads PLAYWRIGHT_BROWSERS_PATH).
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from app.database import init_db  # noqa: E402
from app.api.pages import router as pages_router  # noqa: E402
from app.api.campaigns import router as campaigns_router  # noqa: E402
from app.api.accounts import router as accounts_router  # noqa: E402
from app.api.system import router as system_router  # noqa: E402
from app.api.bulk_connect import router as bulk_connect_router  # noqa: E402
from app.api.queue import router as queue_router  # noqa: E402
from app.api.instagram_services import router as instagram_services_router
from app.api.username_mapping import router as username_mapping_router  # noqa: E402
from app.queue.worker_pool import pool as worker_pool  # noqa: E402
from app.queue.instagram_services_test_worker import test_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ig_automation_os")

init_db()

app = FastAPI(title="IG Automation OS", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter(prefix="/api")
api_router.include_router(pages_router)
api_router.include_router(campaigns_router)
api_router.include_router(accounts_router)
api_router.include_router(system_router)
api_router.include_router(bulk_connect_router)
api_router.include_router(queue_router)
api_router.include_router(username_mapping_router)
api_router.include_router(instagram_services_router)


@api_router.get("/health")
async def health():
    return {"status": "ok", "service": "IG Automation OS", "phase": 1}


app.include_router(api_router)

STATIC_DIR = ROOT_DIR / "app" / "static"
app.mount("/api/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def on_startup():
    worker_pool.start()
    test_worker.start()
    logger.info("IG Automation OS ready — SQLite initialised, worker pool online")


@app.on_event("shutdown")
async def on_shutdown():
    await test_worker.stop()
    await worker_pool.stop()
    logger.info("IG Automation OS shutting down — worker pool stopped")
