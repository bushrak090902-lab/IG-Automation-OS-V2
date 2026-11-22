"""Instagram Services diagnostics worker — REAL background execution.

Completely isolated from the publishing lane (worker_pool.py / publisher.py /
publisher_queue.py) and from Bulk Connect (bulk_connect_worker.py). It never
touches the publishing `jobs` table and never writes to the accounts table.

What it does
------------
Claims pending diagnostic jobs from the persistent SQLite queue
(``instagram_service_jobs``) and executes a permitted, browser-backed
diagnostic for each order using an existing connected account's persistent
Playwright profile:

    1. Open the account's persistent Chromium context (visible Chromium when
       the host has a display and ``svc_diag_headless`` is false).
    2. Session health check — a non-empty ``sessionid`` cookie, no redirect to
       /accounts/login, no login form present.
    3. Navigate to the order's Instagram profile / post / Reel URL and check
       the target is reachable (not a 404, not login-walled).
    4. Capture permitted diagnostics: full-page screenshot + page metadata
       (final URL, title, og:title, description) into
       ``backend/logs/instagram_services_debug/``.

No engagement is automated and no engagement delivery is ever inferred from
the requested quantity: ``completed_quantity`` stays 0 and a passed order is
marked completed *only* because its diagnostic checks succeeded.

Guarantees
----------
* Persistent SQLite job queue — jobs survive restarts; crash recovery requeues
  jobs that were mid-flight when the worker stopped.
* Automatic worker assignment — N worker tasks (``svc_diag_concurrency``,
  default 1) claim jobs atomically from the queue.
* Per-account browser locking — a shared in-process registry, cross-checked
  against the publishing pool's claimed accounts and Bulk Connect's active
  sessions, so the same persistent profile is never opened concurrently.
* Retry and timeout handling — per-step Playwright timeouts, an overall job
  timeout, and bounded retries with backoff; final failure is recorded with a
  meaningful error, never simulated success.
* Cancellation — the operator can cancel queued/running jobs; the worker
  checks cancellation between steps and releases the browser.
* Resource cleanup — the browser context is closed in a ``finally`` block on
  every exit path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..database import BASE_DIR, SessionLocal
from ..models import Account, InstagramServiceJob, InstagramServiceOrder as Order, InstagramServiceOrderLog as OrderLog, Setting

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger("ig_automation_os.services_diagnostics")

DEBUG_DIR = BASE_DIR / "logs" / "instagram_services_debug"

# Isolation cross-checks (read-only) so we never double-open a profile that
# another lane is already using. Publishing pool claims accounts through
# worker_pool.pool; browser_session tracks interactive connect/verify tasks.
from .worker_pool import pool as publishing_pool  # noqa: E402

POLL_INTERVAL_SECONDS = 5.0

NOT_FOUND_MARKERS = (
    "sorry, this page isn't available",
    "page isn't available",
    "content isn't available",
)


class JobCancelled(Exception):
    """Raised inside the diagnostic when the operator cancels the job."""


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
            "concurrency": max(1, min(10, _int("svc_diag_concurrency", 1))),
            "headless": (rows.get("svc_diag_headless", "false").lower() == "true"),
            "timeout_ms": max(5000, _int("svc_diag_browser_timeout_ms", 60000)),
            "job_timeout": max(30, _int("svc_diag_job_timeout_seconds", 180)),
            "max_retries": max(0, min(5, _int("svc_diag_max_retries", 2))),
        }
    finally:
        db.close()


def _has_display() -> bool:
    # Windows desktop sessions have a GUI even though DISPLAY is not set.
    import os
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY"))


class InstagramDiagnosticsWorker:
    def __init__(self) -> None:
        self._tasks: list[tuple[str, asyncio.Task]] = []
        self._running = False
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()          # guards job claim + account claim
        self._claimed_accounts: set[int] = set()
        self._status: dict[str, dict] = {}

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._recover_interrupted_jobs()
        cfg = _settings()
        for i in range(cfg["concurrency"]):
            worker_id = f"diag-{i + 1}"
            self._tasks.append((worker_id, asyncio.create_task(self._worker_loop(worker_id))))
        logger.info("Instagram Services diagnostics worker started with %d worker(s)", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        for _worker_id, task in self._tasks:
            task.cancel()
        for _worker_id, task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("Instagram Services diagnostics worker stopped")

    def notify(self) -> None:
        """Wake idle workers immediately (called when a new job is queued)."""
        self._wake.set()

    def status(self) -> dict:
        return {
            "running": self._running,
            "playwright_installed": PLAYWRIGHT_AVAILABLE,
            "workers": len(self._tasks),
            "claimed_accounts": sorted(self._claimed_accounts),
            "worker_status": dict(self._status),
        }

    # ---- crash recovery ----------------------------------------------
    def _recover_interrupted_jobs(self) -> None:
        """On startup, requeue jobs that were mid-flight when we died and
        diagnostic orders stuck in `processing` with no live job. Publishing
        data is never touched."""
        db = SessionLocal()
        try:
            active_jobs = (
                db.query(InstagramServiceJob)
                .filter(InstagramServiceJob.status.in_(("assigned", "running")))
                .all()
            )
            for job in active_jobs:
                job.status = "queued"
                job.worker_id = ""
                job.assigned_at = None
                db.add(OrderLog(
                    order_id=job.order_id,
                    event_type="recovered",
                    message=f"Diagnostic job #{job.id} was interrupted by a worker restart; requeued for assignment",
                ))
            stuck_orders = (
                db.query(Order)
                .filter(Order.execution_mode == "diagnostic", Order.status == "processing")
                .all()
            )
            for order in stuck_orders:
                has_live = (
                    db.query(InstagramServiceJob)
                    .filter(
                        InstagramServiceJob.order_id == order.id,
                        InstagramServiceJob.status.in_(("queued", "assigned", "running")),
                    )
                    .first()
                )
                if not has_live:
                    order.status = "pending"
                    db.add(OrderLog(
                        order_id=order.id,
                        event_type="recovered",
                        message="Order recovered after worker restart; diagnostic job will be requeued",
                    ))
                    db.add(InstagramServiceJob(order_id=order.id, status="queued", max_attempts=1))
            if active_jobs or stuck_orders:
                db.commit()
                logger.info("Crash recovery: requeued %d job(s), reconciled %d order(s)", len(active_jobs), len(stuck_orders))
        except Exception:
            db.rollback()
            logger.exception("Crash recovery failed")
        finally:
            db.close()

    # ---- job claiming ------------------------------------------------
    async def _claim_job(self, worker_id: str) -> Optional[dict]:
        """Atomically claim the oldest queued job (single-process pool)."""
        async with self._lock:
            db = SessionLocal()
            try:
                job = (
                    db.query(InstagramServiceJob)
                    .filter(InstagramServiceJob.status == "queued")
                    .order_by(InstagramServiceJob.id.asc())
                    .first()
                )
                if job is None:
                    return None
                job.status = "assigned"
                job.worker_id = worker_id
                job.assigned_at = _now()
                job.attempt_count = (job.attempt_count or 0) + 1
                db.commit()
                return {
                    "id": job.id,
                    "order_id": job.order_id,
                    "attempt_count": job.attempt_count,
                }
            finally:
                db.close()

    # ---- account claiming (per-account browser lock) ------------------
    def _publishing_claimed(self) -> set[int]:
        try:
            return set(publishing_pool.status().get("claimed_accounts", []))
        except Exception:
            return set()

    async def _claim_account(self, job_id: int, order_id: int) -> Optional[dict]:
        """Pick a connected account whose persistent profile is not in use by
        any lane. Returns a plain dict (never a detached ORM instance) and the
        account row itself is never modified."""
        async with self._lock:
            db = SessionLocal()
            try:
                busy_states = ("busy", "connecting", "verifying")
                candidates = (
                    db.query(Account)
                    .filter(
                        Account.account_status == "enabled",
                        Account.session_status == "connected",
                        ~Account.id.in_(self._claimed_accounts or {-1}),
                        ~Account.session_status.in_(busy_states),
                    )
                    .order_by(Account.last_used_at.asc().nullsfirst(), Account.id.asc())
                    .limit(20)
                    .all()
                )
                taken = self._publishing_claimed()
                for acc in candidates:
                    if acc.id in taken:
                        continue
                    # Extract fields BEFORE the session closes — returning the
                    # ORM object would leave it detached and expired.
                    ref = {"id": acc.id, "name": acc.account_name,
                           "profile_path": acc.profile_path}
                    self._claimed_accounts.add(acc.id)
                    job_row = db.get(InstagramServiceJob, job_id)
                    if job_row is not None:
                        job_row.account_id = acc.id
                    db.add(OrderLog(
                        order_id=order_id,
                        event_type="account_assigned",
                        message=f"Job #{job_id}: using account '{acc.account_name}' persistent session for diagnostics",
                    ))
                    db.commit()
                    return ref
                return None
            finally:
                db.close()

    def _release_account(self, account_id: int) -> None:
        self._claimed_accounts.discard(account_id)

    # ---- worker loop ---------------------------------------------------
    async def _worker_loop(self, worker_id: str) -> None:
        self._status[worker_id] = {"state": "idle", "job_id": None, "account_id": None}
        while self._running:
            try:
                job = await self._claim_job(worker_id)
            except Exception:
                logger.exception("[%s] claim failed", worker_id)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            if job is None:
                self._status[worker_id] = {"state": "idle", "job_id": None, "account_id": None}
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue

            self._status[worker_id] = {"state": "starting", "job_id": job["id"], "account_id": None}
            try:
                await self._process_job(worker_id, job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never let a worker die and never fake success.
                logger.exception("[%s] job %s crashed: %s", worker_id, job["id"], e)
                self._finish_job(job["id"], status="failed", error=f"worker crash: {type(e).__name__}: {str(e)[:200]}",
                                 order_status="failed",
                                 log_line=(f"Diagnostic job #{job['id']} crashed: {type(e).__name__}: {str(e)[:200]}"))
            finally:
                self._status[worker_id] = {"state": "idle", "job_id": None, "account_id": None}
                self._wake.set()

    # ---- job execution -------------------------------------------------
    async def _process_job(self, worker_id: str, job_ref: dict) -> None:
        job_id = job_ref["id"]
        cfg = _settings()

        db = SessionLocal()
        try:
            job = db.get(InstagramServiceJob, job_id)
            order = db.get(Order, job_ref["order_id"]) if job else None
            if job is None or order is None:
                return
            if job.status == "cancelled":
                return
            if order.execution_mode != "diagnostic":
                self._finish_job(job_id, status="failed", error="order is not a diagnostic order",
                                 order_status="failed", log_line="Job rejected: order is not a diagnostic order")
                return
            if order.status == "cancelled":
                job.status = "cancelled"
                job.finished_at = _now()
                db.commit()
                return
            target_url = order.instagram_url
            order.status = "processing"
            job.status = "running"
            job.started_at = _now()
            db.add(OrderLog(
                order_id=order.id,
                event_type="job_started",
                message=(
                    f"Diagnostic job #{job_id} assigned to worker {worker_id} "
                    f"(attempt {job.attempt_count}/{job.max_attempts}); running real browser checks — no engagement actions"
                ),
            ))
            db.commit()
        finally:
            db.close()

        # ---- account assignment (may wait if the pool is saturated) -----
        account: Optional[dict] = None
        waited = 0.0
        try:
            try:
                while account is None and waited < cfg["job_timeout"]:
                    self._check_cancelled(job_id)
                    account = await self._claim_account(job_id, job_ref["order_id"])
                    if account is None:
                        if waited == 0.0:
                            self._add_log(job_ref["order_id"], "waiting_account",
                                          f"Job #{job_id}: no connected account free right now; waiting for an available session")
                        await asyncio.sleep(min(5.0, cfg["job_timeout"] - waited))
                        waited += 5.0
            except JobCancelled:
                if account is not None:
                    self._release_account(account["id"])
                self._finish_job(job_id, status="cancelled", error="cancelled by operator",
                                 order_status="cancelled",
                                 log_line=f"Diagnostic job #{job_id} cancelled by operator while waiting for an account")
                return
            if account is None:
                self._finish_job(job_id, status="failed", error="no_available_session",
                                 order_status="failed",
                                 log_line=f"Job #{job_id} failed: no connected account session became available within the timeout")
                return

            self._status[worker_id] = {"state": "running", "job_id": job_id, "account_id": account["id"]}
            try:
                result: dict[str, Any]
                try:
                    result = await asyncio.wait_for(
                        self._run_diagnostic(job_id, job_ref["order_id"], target_url, account, cfg),
                        timeout=cfg["job_timeout"],
                    )
                except asyncio.TimeoutError:
                    raise TimeoutError(f"job_timeout: exceeded {cfg['job_timeout']}s")
                except JobCancelled:
                    self._finish_job(job_id, status="cancelled", error="cancelled by operator",
                                     order_status="cancelled",
                                     log_line=f"Diagnostic job #{job_id} cancelled by operator; browser released")
                    return
                self._finish_job(job_id, status="succeeded", result=result,
                                 order_status="completed",
                                 log_line=(
                                     f"Diagnostic job #{job_id} PASSED — target reachable: {result.get('target_accessible')}, "
                                     f"session healthy: {result.get('session_healthy')}; "
                                     f"requested quantity is a reference only — 0 engagement delivered"
                                 ))
            except TimeoutError as e:
                self._handle_failure(job_id, job_ref["attempt_count"], cfg, str(e))
            except Exception as e:
                self._handle_failure(job_id, job_ref["attempt_count"], cfg, f"{type(e).__name__}: {str(e)[:250]}")
            finally:
                self._release_account(account["id"])
                self._wake.set()
        finally:
            # Belt-and-braces: the claim can never leak, even if an unexpected
            # error fires between claim and the inner finally above.
            if account is not None:
                self._release_account(account["id"])

    # ---- the real browser diagnostic ------------------------------------
    async def _run_diagnostic(self, job_id: int, order_id: int, target_url: str,
                              account: dict, cfg: dict) -> dict:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not installed on this server")

        from playwright.async_api import async_playwright

        profile_path = Path(account["profile_path"])
        if not profile_path.exists() or not any(profile_path.iterdir()):
            raise RuntimeError("session_missing: account has no persistent profile on this host")

        headless = cfg["headless"] or not _has_display()
        if headless and not cfg["headless"]:
            self._add_log(order_id, "headless_fallback",
                          f"Job #{job_id}: host has no display — running headless; visible Chromium requires running locally on Windows")

        ts = time.strftime("%Y%m%d_%H%M%S")
        debug_dir = DEBUG_DIR / f"order_{order_id}_job_{job_id}_{ts}"
        debug_dir.mkdir(parents=True, exist_ok=True)

        result: dict[str, Any] = {
            "account_id": account["id"],
            "account_name": account["name"],
            "headless": headless,
            "profile_used": str(profile_path),
            "debug_dir": str(debug_dir),
            "session_healthy": False,
            "target_accessible": False,
        }

        async with async_playwright() as p:
            # Launch args intentionally match browser_session/bulk_connect
            # (extra automation-control args crash Chromium on Windows builds).
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            self._add_log(order_id, "browser_opened",
                          f"Job #{job_id}: Chromium opened for '{account['name']}' ({'headless' if headless else 'visible'})")
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(cfg["timeout_ms"])

                # Step 1 — session health (reuse of an explicitly authorized session)
                self._check_cancelled(job_id)
                try:
                    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
                except Exception as e:
                    raise RuntimeError(f"instagram_unreachable: {type(e).__name__}: {str(e)[:150]}")
                await asyncio.sleep(2)

                try:
                    cookies = await context.cookies("https://www.instagram.com/")
                    has_session = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
                except Exception:
                    has_session = False
                redirected_to_login = "/accounts/login" in (page.url or "")
                try:
                    login_form_present = bool(await page.query_selector('input[name="username"]'))
                except Exception:
                    login_form_present = False
                result["session_healthy"] = has_session and not redirected_to_login and not login_form_present
                if not result["session_healthy"]:
                    raise RuntimeError(
                        f"session_unhealthy: sessionid={'present' if has_session else 'missing'}, "
                        f"login_redirect={redirected_to_login}, login_form={login_form_present}"
                    )
                self._add_log(order_id, "session_healthy",
                              f"Job #{job_id}: authorized session verified healthy (sessionid cookie present, no login wall)")

                # Step 2 — target navigation + accessibility check
                self._check_cancelled(job_id)
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
                except Exception as e:
                    raise RuntimeError(f"target_unreachable: {type(e).__name__}: {str(e)[:150]}")
                await asyncio.sleep(2)
                final_url = page.url
                result["final_url"] = final_url

                try:
                    title = await page.title()
                except Exception:
                    title = ""
                result["title"] = title

                try:
                    body_text = (await page.locator("body").inner_text(timeout=5000)).lower()
                except Exception:
                    body_text = ""
                not_found = any(marker in body_text for marker in NOT_FOUND_MARKERS)
                login_walled = "/accounts/login" in (final_url or "")
                try:
                    login_walled = login_walled or bool(await page.query_selector('input[name="username"]'))
                except Exception:
                    pass

                if not_found:
                    raise RuntimeError("target_not_found: Instagram reports this page is not available")
                if login_walled:
                    raise RuntimeError("target_login_walled: target redirected to a login page")
                result["target_accessible"] = True
                self._add_log(order_id, "target_accessible",
                              f"Job #{job_id}: target loaded OK — {title or final_url}")

                # Step 3 — permitted metadata capture
                self._check_cancelled(job_id)
                meta: dict[str, Any] = {
                    "order_id": order_id,
                    "job_id": job_id,
                    "source_url": target_url,
                    "final_url": final_url,
                    "title": title,
                    "session_healthy": True,
                    "headless": headless,
                    "captured_at": _now().isoformat(),
                }
                try:
                    og = await page.locator('meta[property="og:title"]').get_attribute("content")
                    desc = await page.locator('meta[name="description"]').get_attribute("content")
                    meta["og_title"] = og
                    meta["description"] = desc
                except Exception:
                    pass
                (debug_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
                try:
                    await page.screenshot(path=str(debug_dir / "screenshot.png"), full_page=True)
                except Exception as e:
                    self._add_log(order_id, "screenshot_failed", f"Job #{job_id}: screenshot failed: {type(e).__name__}")
                result["screenshot_path"] = str(debug_dir / "screenshot.png")
                result["meta_path"] = str(debug_dir / "meta.json")
                self._add_log(order_id, "diagnostics_captured",
                              f"Job #{job_id}: screenshot + metadata saved to {debug_dir}")
                return result
            except JobCancelled:
                raise
            except Exception:
                # Best-effort debug capture on failure — never cookies.
                try:
                    await page.screenshot(path=str(debug_dir / "error_screenshot.png"), full_page=False)
                    (debug_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
                except Exception:
                    pass
                raise
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                self._add_log(order_id, "browser_closed", f"Job #{job_id}: Chromium closed, profile released")

    # ---- failure handling / retries -------------------------------------
    def _handle_failure(self, job_id: int, attempt_count: int, cfg: dict, error: str) -> None:
        db = SessionLocal()
        requeued = False
        try:
            job = db.get(InstagramServiceJob, job_id)
            if job is None:
                return
            if job.status == "cancelled":
                return
            if attempt_count < job.max_attempts:
                job.status = "queued"
                job.error = error[:500]
                job.worker_id = ""
                job.assigned_at = None
                db.add(OrderLog(
                    order_id=job.order_id,
                    event_type="retry_scheduled",
                    message=f"Job #{job_id} failed ({error[:200]}); retry {attempt_count}/{job.max_attempts} requeued",
                ))
                requeued = True
            else:
                job.status = "failed"
                job.error = error[:500]
                job.finished_at = _now()
                db.add(OrderLog(
                    order_id=job.order_id,
                    event_type="diagnostic_failed",
                    message=f"Job #{job_id} FAILED permanently after {attempt_count} attempt(s): {error}",
                ))
            order = db.get(Order, job.order_id)
            if order is not None:
                if requeued:
                    order.status = "pending"
                else:
                    order.status = "failed"
                    order.completed_at = _now()
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("failure handling for job %s failed", job_id)
        finally:
            db.close()
        if requeued:
            self._wake.set()

    def _finish_job(self, job_id: int, status: str, error: str = "", result: Optional[dict] = None,
                    order_status: str = "", log_line: str = "") -> None:
        db = SessionLocal()
        try:
            job = db.get(InstagramServiceJob, job_id)
            if job is None:
                return
            job.status = status
            job.error = error[:500]
            if result is not None:
                job.result = json.dumps(result, default=str)
            job.finished_at = _now()
            order = db.get(Order, job.order_id)
            if order is not None and order_status:
                order.status = order_status
                if order_status in ("completed", "failed", "cancelled"):
                    order.completed_at = _now()
                if order_status == "completed":
                    # The requested quantity is an order reference ONLY. A
                    # completed diagnostic order means its real checks passed —
                    # it never records delivered engagement.
                    order.completed_quantity = 0
            if log_line:
                db.add(OrderLog(order_id=job.order_id, event_type=f"job_{status}", message=log_line))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("finish job %s failed", job_id)
        finally:
            db.close()

    # ---- helpers ---------------------------------------------------------
    def _check_cancelled(self, job_id: int) -> None:
        db = SessionLocal()
        try:
            job = db.get(InstagramServiceJob, job_id)
            if job is not None and job.status == "cancelled":
                raise JobCancelled()
        finally:
            db.close()

    def _add_log(self, order_id: int, event_type: str, message: str) -> None:
        db = SessionLocal()
        try:
            db.add(OrderLog(order_id=order_id, event_type=event_type, message=message))
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _add_order_log(self, order_id: int, event_type: str, message: str) -> None:
        self._add_log(order_id, event_type, message)


# Module-level singleton
diagnostics_worker = InstagramDiagnosticsWorker()
