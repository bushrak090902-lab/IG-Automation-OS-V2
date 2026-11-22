"""Publishing worker pool.

Architecture
------------
    Campaigns -> Jobs (queue) -> WorkerPool -> N Workers
                                                 |
                                    each Worker claims an ACCOUNT
                                                 |
                                    opens ONE persistent browser
                                                 |
                                    publishes ALL that account's jobs
                                                 |
                                    closes the browser, releases account

Key guarantees
--------------
* Browser startup cost is paid once per account-cycle, never per upload.
* Two workers never touch the same account (shared ``_claimed`` set + lock).
* No worker blocks another: claiming and DB writes use short-lived sessions.
* Retries: a job is retried in-place (same warm browser) up to ``max_retries``
  for transient errors; terminal errors (login_required, etc.) fail fast.
* Pause/stop is honoured between jobs by re-reading campaign status.

The pool runs inside the FastAPI event loop (started in ``server.on_startup``)
so Playwright shares the same loop as the rest of the app.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from ..database import SessionLocal
from ..models import Account, Job, Setting
from ..services.logger import log
from ..automation import browser_pool, publisher
from ..automation.publisher import PublishError
from . import queue_manager as qm

logger = logging.getLogger("ig_automation_os.workers")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings() -> dict:
    db = SessionLocal()
    try:
        rows = {r.key: r.value for r in db.query(Setting).all()}

        def _int(k, d):
            try:
                return int(rows.get(k, d))
            except (ValueError, TypeError):
                return d

        return {
            "workers": max(1, min(50, _int("concurrent_workers", 2))),
            "delay": max(0, _int("delay_between_jobs_seconds", 30)),
            "max_retries": max(0, _int("max_retries", 3)),
            "headless": (rows.get("browser_headless", "true").lower() == "true"),
            "timeout_ms": _int("browser_timeout_seconds", 60) * 1000,
        }
    finally:
        db.close()


class WorkerPool:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._claimed: set[int] = set()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._running = False
        self._status: dict[int, dict] = {}

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        n = _settings()["workers"]
        for i in range(n):
            self._tasks.append(asyncio.create_task(self._worker_loop(i)))
        logger.info("WorkerPool started with %d worker(s)", n)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    def notify(self) -> None:
        """Wake idle workers immediately (called when new jobs are queued)."""
        self._wake.set()

    def status(self) -> dict:
        return {
            "running": self._running,
            "workers": len(self._tasks),
            "claimed_accounts": sorted(self._claimed),
            "worker_status": self._status,
        }

    # ---- account claiming -------------------------------------------
    async def _claim(self) -> Optional[int]:
        async with self._lock:
            db = SessionLocal()
            try:
                account_id = qm.claim_account(db, self._claimed)
            finally:
                db.close()
            if account_id is not None:
                self._claimed.add(account_id)
            return account_id

    async def _release(self, account_id: int) -> None:
        async with self._lock:
            self._claimed.discard(account_id)

    # ---- worker loop -------------------------------------------------
    async def _worker_loop(self, worker_id: int) -> None:
        self._status[worker_id] = {"state": "idle", "account_id": None, "job_id": None}
        while self._running:
            account_id = await self._claim()
            if account_id is None:
                self._status[worker_id] = {"state": "idle", "account_id": None, "job_id": None}
                # sleep until woken or a short poll interval
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._process_account(worker_id, account_id)
            except Exception as e:  # never let a worker die
                logger.exception("worker %d crashed on account %d: %s", worker_id, account_id, e)
            finally:
                await self._release(account_id)

    async def _process_account(self, worker_id: int, account_id: int) -> None:
        cfg = _settings()
        db = SessionLocal()
        try:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                return
            profile_path = account.profile_path
            account_name = account.account_name
            account.session_status = "busy"
            db.commit()
        finally:
            db.close()

        self._status[worker_id] = {"state": "opening_browser", "account_id": account_id, "job_id": None}
        published_any = False
        touched_campaigns: set[int] = set()
        try:
            async with browser_pool.open_account_context(
                profile_path, headless=cfg["headless"], timeout_ms=cfg["timeout_ms"],
            ) as context:
                # One auth check per account-cycle (fail fast, warm browser reused).
                try:
                    await publisher.ensure_logged_in(context)
                    session_ok = True
                except PublishError as e:
                    session_ok = False
                    failed_campaigns = self._fail_all_queued(account_id, e.code)
                    touched_campaigns.update(failed_campaigns)
                    self._set_session(account_id, "login_required")

                while session_ok and self._running:
                    job = self._next_job(account_id)
                    if not job:
                        break
                    self._status[worker_id] = {
                        "state": "publishing", "account_id": account_id, "job_id": job["id"],
                    }
                    touched_campaigns.add(job["campaign_id"])
                    ok = await self._run_job_with_retries(context, job, cfg)
                    published_any = published_any or ok
                    if cfg["delay"] > 0:
                        await asyncio.sleep(cfg["delay"])
        except Exception as e:
            logger.exception("browser lifecycle error for account %d: %s", account_id, e)
            self._write_log(account_id, "ERROR", f"Browser error for '{account_name}': {type(e).__name__}")
        finally:
            # Return the account to a sensible session state.
            self._set_session(account_id, "connected" if published_any else None, restore=True)
            for cid in touched_campaigns:
                self._finalize_campaign(cid)
            self._status[worker_id] = {"state": "idle", "account_id": None, "job_id": None}

    # ---- per-job execution ------------------------------------------
    async def _run_job_with_retries(self, context, job: dict, cfg: dict) -> bool:
        job_id = job["id"]
        max_retries = cfg["max_retries"]
        attempt = job["attempt_count"]
        while True:
            attempt += 1
            self._update_job(job_id, status="processing", attempt_count=attempt,
                              started_at=_now(), error_message="")
            try:
                await publisher.publish_job(
                    context,
                    content_type=job["content_type"],
                    file_path=job["file_path"],
                    caption=job["caption"],
                    audio_mode=job["audio_mode"],
                    music_search=job["music_search"],
                )
                self._update_job(job_id, status="successful", completed_at=_now(), error_message="")
                self._mark_account_success(job["account_id"])
                self._write_log(job["account_id"], "SUCCESS",
                                f"Published job #{job_id} (campaign #{job['campaign_id']})",
                                campaign_id=job["campaign_id"])
                return True
            except PublishError as e:
                if e.retryable and attempt <= max_retries:
                    self._update_job(job_id, status="queued", error_message=f"{e.code}: retry {attempt}/{max_retries}")
                    self._write_log(job["account_id"], "WARNING",
                                    f"Job #{job_id} failed ({e.code}); retry {attempt}/{max_retries}",
                                    campaign_id=job["campaign_id"])
                    await asyncio.sleep(min(15, 3 * attempt))
                    continue
                self._update_job(job_id, status="failed", completed_at=_now(), error_message=e.code)
                self._write_log(job["account_id"], "ERROR",
                                f"Job #{job_id} failed permanently: {e.code}",
                                campaign_id=job["campaign_id"])
                return False
            except Exception as e:
                if attempt <= max_retries:
                    self._update_job(job_id, status="queued", error_message=f"error: retry {attempt}/{max_retries}")
                    await asyncio.sleep(min(15, 3 * attempt))
                    continue
                self._update_job(job_id, status="failed", completed_at=_now(),
                                 error_message=str(e)[:200])
                return False

    # ---- DB helpers (own session per call) --------------------------
    def _next_job(self, account_id: int) -> Optional[dict]:
        db = SessionLocal()
        try:
            job = qm.next_job_for_account(db, account_id)
            if not job:
                return None
            campaign = job.campaign
            from ..models import CampaignFile
            f = db.query(CampaignFile).filter(CampaignFile.id == job.file_id).first()
            return {
                "id": job.id,
                "account_id": account_id,
                "campaign_id": job.campaign_id,
                "attempt_count": job.attempt_count,
                "content_type": campaign.content_type,
                "caption": campaign.caption,
                "audio_mode": campaign.audio_mode,
                "music_search": campaign.music_search,
                "file_path": f.stored_path if f else "",
            }
        finally:
            db.close()

    def _update_job(self, job_id: int, **fields) -> None:
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                for k, v in fields.items():
                    setattr(job, k, v)
                db.commit()
        finally:
            db.close()

    def _fail_all_queued(self, account_id: int, code: str) -> set[int]:
        db = SessionLocal()
        affected: set[int] = set()
        try:
            jobs = (
                db.query(Job)
                .filter(Job.account_id == account_id, Job.status == "queued")
                .all()
            )
            for j in jobs:
                j.status = "failed"
                j.error_message = code
                j.completed_at = _now()
                affected.add(j.campaign_id)
            if jobs:
                db.commit()
        finally:
            db.close()
        return affected

    def _mark_account_success(self, account_id: int) -> None:
        db = SessionLocal()
        try:
            acc = db.query(Account).filter(Account.id == account_id).first()
            if acc:
                acc.last_used_at = _now()
                acc.last_successful_post_at = _now()
                db.commit()
        finally:
            db.close()

    def _set_session(self, account_id: int, status: Optional[str], restore: bool = False) -> None:
        db = SessionLocal()
        try:
            acc = db.query(Account).filter(Account.id == account_id).first()
            if not acc:
                return
            if restore and acc.session_status == "busy":
                acc.session_status = status or "connected"
            elif status is not None:
                acc.session_status = status
            acc.last_used_at = _now()
            db.commit()
        finally:
            db.close()

    def _finalize_campaign(self, campaign_id: int) -> None:
        db = SessionLocal()
        try:
            qm.finalize_campaign_if_done(db, campaign_id)
        finally:
            db.close()

    def _write_log(self, account_id, level, message, campaign_id=None) -> None:
        db = SessionLocal()
        try:
            log(db, level, message, campaign_id=campaign_id, account_id=account_id)
        finally:
            db.close()


# Module-level singleton
pool = WorkerPool()
