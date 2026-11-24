"""Instagram Services diagnostics worker: real, paced background verification.

Completely isolated from the publishing lane (worker_pool.py / publisher.py /
publisher_queue.py), from Bulk Connect, and from the shared browser helpers.
It never writes to the publishing `jobs` table or to the `accounts` table.

What it does (permitted verification only — no likes, follows, comments,
shares or reposts are ever performed):

    1. Claim a queued job from the persistent SQLite queue
       (``instagram_service_jobs``) once its ``not_before`` time has passed.
    2. Claim a connected account that is not in use by any lane and is not
       resting / cooling down (``instagram_service_account_state``).
    3. Open that account's persistent profile in a background (headless)
       Chromium unless ``svc_diag_show_browser`` is turned on for debugging.
    4. Wait for real page readiness (load event, network settle, content
       element present) plus a randomized dwell before each next step.
    5. Detect Instagram restriction / challenge / checkpoint pages and back
       off: the account is cooled down, the job is retried later with
       another account, and the whole lane pauses if restrictions cluster.
    6. Verify session health and target accessibility, read the public
       follower / like / comment count where Instagram exposes it, and save
       a screenshot + meta.json.

Order outcome:
    * diagnostic checks pass  -> order status ``verified`` (0 delivered)
    * a follow-up job later re-reads the public count -> observed growth
    * delivered quantity only changes when the operator records it
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..database import BASE_DIR, SessionLocal
from ..models import (Account, InstagramServiceAccountState as AccountState,
                      InstagramServiceJob, InstagramServiceObservation as Observation,
                      InstagramServiceOrder as Order, InstagramServiceOrderLog as OrderLog,
                      Setting)

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger("ig_automation_os.services_diagnostics")

DEBUG_DIR = BASE_DIR / "logs" / "instagram_services_debug"

# Read-only cross-check so a profile in use by the publishing pool is skipped.
from .worker_pool import pool as publishing_pool  # noqa: E402

POLL_INTERVAL_SECONDS = 5.0
ACCOUNT_WAIT_SECONDS = 60.0
ACCOUNT_REQUEUE_SECONDS = 30

NOT_FOUND_MARKERS = (
    "sorry, this page isn't available",
    "page isn't available",
    "content isn't available",
)

RESTRICTION_URL_MARKERS = ("/challenge", "/checkpoint", "/accounts/suspended", "/accounts/disabled")
RESTRICTION_TEXT_MARKERS = (
    "try again later",
    "we restrict certain activity",
    "we limit how often",
    "action blocked",
    "your account has been suspended",
    "we suspended your account",
    "your account has been disabled",
    "confirm it's you",
    "help us confirm it's you",
    "suspicious login attempt",
)

# Which public count each service can be verified against. Views, shares and
# reposts are not exposed in Instagram's public page metadata.
SERVICE_METRIC = {"followers": "followers", "likes": "likes", "comments": "comments"}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def detect_restriction(url: str, body_text: str) -> Optional[str]:
    """Return a short reason if the page is an Instagram restriction,
    challenge, checkpoint or suspension page; otherwise None."""
    u = (url or "").lower()
    for m in RESTRICTION_URL_MARKERS:
        if m in u:
            return f"url:{m.strip('/')}"
    t = (body_text or "").lower()
    for m in RESTRICTION_TEXT_MARKERS:
        if m in t:
            return f"text:{m}"
    return None


def _to_int(raw: str) -> Optional[int]:
    s = raw.strip().replace(" ", "")
    mult = 1
    if s[-1:].lower() in ("k", "m", "b"):
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[s[-1].lower()]
        s = s[:-1]
        s = s.replace(",", ".") if s.count(",") == 1 and "." not in s else s.replace(",", "")
        try:
            return int(round(float(s) * mult))
        except ValueError:
            return None
    s = s.replace(",", "").replace(".", "")
    return int(s) if s.isdigit() else None


_COUNT_RE = r"([\d][\d.,]*\s?[KkMmBb]?)"
_METRIC_PATTERNS = {
    "followers": re.compile(_COUNT_RE + r"\s+Followers", re.I),
    "likes": re.compile(_COUNT_RE + r"\s+likes?\b", re.I),
    "comments": re.compile(_COUNT_RE + r"\s+comments?\b", re.I),
}


def parse_metric(service: str, description: str) -> tuple[Optional[str], Optional[int]]:
    """Read the public count for a service from Instagram's og/meta
    description. Returns (metric, value); value is None when not exposed."""
    metric = SERVICE_METRIC.get(service)
    if not metric:
        return None, None
    m = _METRIC_PATTERNS[metric].search(description or "")
    return metric, (_to_int(m.group(1)) if m else None)


class DiagnosticError(Exception):
    """A real, classified diagnostic failure."""

    def __init__(self, code: str, message: str, *, category: str = "other", retryable: bool = True):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.category = category  # restriction | session | target | other
        self.retryable = retryable


class JobCancelled(Exception):
    """Raised inside the diagnostic when the operator cancels the job."""


def _now() -> datetime:
    # Naive UTC: SQLite DateTime columns round-trip without tzinfo.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _settings() -> dict:
    db = SessionLocal()
    try:
        rows = {r.key: r.value for r in db.query(Setting).all()}
    finally:
        db.close()

    def _int(k, d, lo=None, hi=None):
        try:
            v = int(float(rows.get(k, d)))
        except (ValueError, TypeError):
            v = d
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    try:
        backoff = [max(0, int(x)) for x in rows.get("svc_diag_retry_backoff_seconds", "300,1200").split(",") if x.strip()]
    except ValueError:
        backoff = [300, 1200]
    rest_min = _int("svc_diag_account_rest_min_seconds", 120, 0)
    dwell_min = _int("svc_diag_dwell_min_seconds", 3, 0)
    return {
        "workers": _int("svc_diag_workers", 10, 1, 10),
        "show_browser": rows.get("svc_diag_show_browser", "false").lower() == "true",
        "timeout_ms": _int("svc_diag_browser_timeout_ms", 60000, 5000),
        "job_timeout": _int("svc_diag_job_timeout_seconds", 180, 30),
        "max_retries": _int("svc_diag_max_retries", 2, 0, 5),
        "retry_backoff": backoff or [300, 1200],
        "rest_min": rest_min,
        "rest_max": max(rest_min, _int("svc_diag_account_rest_max_seconds", 300, 0)),
        "dwell_min": dwell_min,
        "dwell_max": max(dwell_min, _int("svc_diag_dwell_max_seconds", 8, 0)),
        "restriction_cooldown_min": _int("svc_diag_restriction_cooldown_minutes", 1440, 0),
        "session_cooldown_min": _int("svc_diag_session_cooldown_minutes", 360, 0),
        "pause_threshold": _int("svc_diag_pause_threshold", 3, 1),
        "pause_window_min": _int("svc_diag_pause_window_minutes", 15, 1),
        "pause_min": _int("svc_diag_pause_minutes", 30, 0),
        "followup_min": _int("svc_diag_followup_minutes", 60, 0),
    }


def _has_display() -> bool:
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
        self._paused_until: Optional[datetime] = None
        self._pause_reason = ""
        self._waiting_logged: set[int] = set()

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._recover_interrupted_jobs()
        cfg = _settings()
        for i in range(cfg["workers"]):
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
        self._wake.set()

    def status(self) -> dict:
        paused = bool(self._paused_until and self._paused_until > _now())
        return {
            "running": self._running,
            "playwright_installed": PLAYWRIGHT_AVAILABLE,
            "workers": len(self._tasks),
            "claimed_accounts": sorted(self._claimed_accounts),
            "worker_status": dict(self._status),
            "paused": paused,
            "paused_until": self._paused_until.isoformat() if paused else None,
            "pause_reason": self._pause_reason if paused else "",
        }

    # ---- crash recovery ----------------------------------------------
    def _recover_interrupted_jobs(self) -> None:
        db = SessionLocal()
        try:
            active_jobs = (db.query(InstagramServiceJob)
                           .filter(InstagramServiceJob.status.in_(("assigned", "running"))).all())
            for job in active_jobs:
                job.status = "queued"
                job.worker_id = ""
                job.assigned_at = None
                db.add(OrderLog(order_id=job.order_id, event_type="recovered",
                                message=f"Job #{job.id} was interrupted by a restart; requeued automatically"))
            stuck = (db.query(Order)
                     .filter(Order.execution_mode == "diagnostic", Order.status == "processing").all())
            for order in stuck:
                live = (db.query(InstagramServiceJob)
                        .filter(InstagramServiceJob.order_id == order.id,
                                InstagramServiceJob.status.in_(("queued", "assigned", "running"))).first())
                if not live:
                    order.status = "pending"
                    db.add(OrderLog(order_id=order.id, event_type="recovered",
                                    message="Order recovered after restart; diagnostic job requeued"))
                    db.add(InstagramServiceJob(order_id=order.id, status="queued", kind="diagnostic", max_attempts=1))
            if active_jobs or stuck:
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("Crash recovery failed")
        finally:
            db.close()

    # ---- job claiming ------------------------------------------------
    def _is_paused(self) -> bool:
        return bool(self._paused_until and self._paused_until > _now())

    async def _claim_job(self, worker_id: str) -> Optional[dict]:
        if self._is_paused():
            return None
        async with self._lock:
            db = SessionLocal()
            try:
                now = _now()
                job = (db.query(InstagramServiceJob)
                       .filter(InstagramServiceJob.status == "queued",
                               (InstagramServiceJob.not_before.is_(None)) | (InstagramServiceJob.not_before <= now))
                       .order_by(InstagramServiceJob.id.asc())
                       .first())
                if job is None:
                    return None
                job.status = "assigned"
                job.worker_id = worker_id
                job.assigned_at = now
                job.attempt_count = (job.attempt_count or 0) + 1
                db.commit()
                return {"id": job.id, "order_id": job.order_id,
                        "attempt_count": job.attempt_count, "kind": job.kind or "diagnostic"}
            finally:
                db.close()

    # ---- account claiming (per-account lock + pacing) ------------------
    def _publishing_claimed(self) -> set[int]:
        try:
            return set(publishing_pool.status().get("claimed_accounts", []))
        except Exception:
            return set()

    async def _claim_account(self, job_id: int, order_id: int):
        """Returns an account dict, None (all busy/resting), or the string
        "none_connected" when no connected+enabled account exists at all."""
        async with self._lock:
            db = SessionLocal()
            try:
                now = _now()
                base = db.query(Account).filter(Account.account_status == "enabled",
                                                Account.session_status == "connected")
                if base.count() == 0:
                    return "none_connected"
                resting = db.query(AccountState.account_id).filter(AccountState.available_after > now)
                candidates = (base.filter(~Account.id.in_(self._claimed_accounts or {-1}),
                                          ~Account.id.in_(resting))
                              .order_by(Account.last_used_at.asc().nullsfirst(), Account.id.asc())
                              .limit(50).all())
                taken = self._publishing_claimed()
                for acc in candidates:
                    if acc.id in taken:
                        continue
                    ref = {"id": acc.id, "name": acc.account_name, "profile_path": acc.profile_path}
                    self._claimed_accounts.add(acc.id)
                    job_row = db.get(InstagramServiceJob, job_id)
                    if job_row is not None:
                        job_row.account_id = acc.id
                    db.add(OrderLog(order_id=order_id, event_type="account_assigned",
                                    message=f"Job #{job_id}: using account '{acc.account_name}' (session reused, profile locked)"))
                    db.commit()
                    return ref
                return None
            finally:
                db.close()

    def _rest_account(self, account_id: int, seconds: float, *, reason: str = "", restricted: bool = False) -> None:
        """Release the in-memory lock and record the account's next allowed
        check time in the Services-only state table."""
        self._claimed_accounts.discard(account_id)
        db = SessionLocal()
        try:
            now = _now()
            st = db.get(AccountState, account_id)
            if st is None:
                st = AccountState(account_id=account_id, restriction_reason="", restriction_count=0)
                db.add(st)
            until = now + timedelta(seconds=seconds)
            if not st.available_after or st.available_after < until:
                st.available_after = until
            st.last_check_at = now
            if restricted:
                st.restricted_at = now
                st.restriction_reason = reason[:500]
                st.restriction_count = (st.restriction_count or 0) + 1
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("account state update failed for %s", account_id)
        finally:
            db.close()

    def _maybe_pause_lane(self, cfg: dict, order_id: int) -> None:
        db = SessionLocal()
        try:
            since = _now() - timedelta(minutes=cfg["pause_window_min"])
            recent = db.query(AccountState).filter(AccountState.restricted_at >= since).count()
        finally:
            db.close()
        if recent >= cfg["pause_threshold"] and cfg["pause_min"] > 0:
            self._paused_until = _now() + timedelta(minutes=cfg["pause_min"])
            self._pause_reason = f"{recent} accounts restricted within {cfg['pause_window_min']} min"
            self._add_log(order_id, "lane_paused",
                          f"All Instagram Services workers paused for {cfg['pause_min']} min: {self._pause_reason}")
            logger.warning("Instagram Services lane paused: %s", self._pause_reason)

    # ---- worker loop ---------------------------------------------------
    async def _worker_loop(self, worker_id: str) -> None:
        self._status[worker_id] = {"state": "idle", "job_id": None, "account_id": None}
        # Stagger worker start-up so 10 browsers never launch at the same instant.
        await asyncio.sleep(random.uniform(0, 3) * (int(worker_id.split("-")[-1]) - 1) / 3)
        while self._running:
            try:
                job = await self._claim_job(worker_id)
            except Exception:
                logger.exception("[%s] claim failed", worker_id)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            if job is None:
                state = "paused" if self._is_paused() else "idle"
                self._status[worker_id] = {"state": state, "job_id": None, "account_id": None}
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
                logger.exception("[%s] job %s crashed", worker_id, job["id"])
                self._finish_job(job["id"], status="failed", error=f"worker crash: {type(e).__name__}: {str(e)[:200]}",
                                 order_status="failed" if job["kind"] == "diagnostic" else "",
                                 log_line=f"Job #{job['id']} crashed: {type(e).__name__}: {str(e)[:200]}")
            finally:
                self._status[worker_id] = {"state": "idle", "job_id": None, "account_id": None}
                self._wake.set()

    # ---- job execution -------------------------------------------------
    async def _process_job(self, worker_id: str, job_ref: dict) -> None:
        job_id, order_id, kind = job_ref["id"], job_ref["order_id"], job_ref["kind"]
        cfg = _settings()

        db = SessionLocal()
        try:
            job = db.get(InstagramServiceJob, job_id)
            order = db.get(Order, order_id) if job else None
            if job is None or order is None or job.status == "cancelled":
                return
            if order.execution_mode != "diagnostic":
                self._finish_job(job_id, status="failed", error="order is not a diagnostic order",
                                 order_status="failed", log_line="Job rejected: order is not a diagnostic order")
                return
            if order.status == "cancelled" or (kind == "diagnostic" and order.status not in ("pending", "processing")):
                job.status = "cancelled"
                job.finished_at = _now()
                db.commit()
                return
            target_url, service = order.instagram_url, order.service_type
            if kind == "diagnostic":
                order.status = "processing"
            job.status = "running"
            job.started_at = _now()
            db.add(OrderLog(order_id=order_id, event_type="job_started",
                            message=(f"{'Follow-up' if kind == 'followup' else 'Diagnostic'} job #{job_id} on worker "
                                     f"{worker_id} (attempt {job.attempt_count}/{job.max_attempts}); "
                                     f"verification only — no engagement actions")))
            db.commit()
        finally:
            db.close()

        # ---- account assignment ---------------------------------------
        account = None
        waited = 0.0
        try:
            while account is None and waited < ACCOUNT_WAIT_SECONDS:
                self._check_cancelled(job_id)
                account = await self._claim_account(job_id, order_id)
                if account == "none_connected":
                    self._finish_job(job_id, status="failed", error="no_connected_accounts",
                                     order_status="failed" if kind == "diagnostic" else "",
                                     log_line=f"Job #{job_id} failed: no connected, enabled accounts exist")
                    return
                if account is None:
                    await asyncio.sleep(5.0)
                    waited += 5.0
        except JobCancelled:
            self._finish_job(job_id, status="cancelled", error="cancelled by operator", order_status="cancelled",
                             log_line=f"Job #{job_id} cancelled by operator while waiting for an account")
            return
        if account is None:
            self._requeue_waiting(job_id, order_id, kind)
            return
        self._waiting_logged.discard(job_id)

        self._status[worker_id] = {"state": "running", "job_id": job_id, "account_id": account["id"]}
        rest_seconds = random.uniform(cfg["rest_min"], cfg["rest_max"])
        rest_reason, restricted = "", False
        try:
            try:
                result = await asyncio.wait_for(
                    self._run_diagnostic(job_id, order_id, target_url, service, account, cfg),
                    timeout=cfg["job_timeout"])
            except asyncio.TimeoutError:
                raise DiagnosticError("job_timeout", f"exceeded {cfg['job_timeout']}s", category="other")
            except JobCancelled:
                self._finish_job(job_id, status="cancelled", error="cancelled by operator", order_status="cancelled",
                                 log_line=f"Job #{job_id} cancelled by operator; browser released")
                return
            self._on_success(job_id, order_id, kind, result, cfg)
        except DiagnosticError as e:
            if e.category == "restriction":
                rest_seconds = cfg["restriction_cooldown_min"] * 60
                rest_reason, restricted = str(e), True
                self._add_log(order_id, "account_restricted",
                              f"Job #{job_id}: Instagram showed a restriction for '{account['name']}' ({e.code}); "
                              f"account cooled down {cfg['restriction_cooldown_min']} min — no bypass attempted")
            elif e.category == "session":
                rest_seconds = max(rest_seconds, cfg["session_cooldown_min"] * 60)
                self._add_log(order_id, "account_cooldown",
                              f"Job #{job_id}: session for '{account['name']}' is not healthy; account skipped for "
                              f"{cfg['session_cooldown_min']} min (reconnect it from Bulk Connect / Accounts)")
            self._handle_failure(job_id, job_ref["attempt_count"], kind, cfg, str(e), retryable=e.retryable)
        except Exception as e:
            self._handle_failure(job_id, job_ref["attempt_count"], kind, cfg, f"{type(e).__name__}: {str(e)[:250]}")
        finally:
            self._rest_account(account["id"], rest_seconds, reason=rest_reason, restricted=restricted)
            if restricted:
                self._maybe_pause_lane(cfg, order_id)
            self._wake.set()

    def _requeue_waiting(self, job_id: int, order_id: int, kind: str) -> None:
        """All eligible accounts are busy or resting: put the job back without
        consuming an attempt. The order stays pending — never a fake failure."""
        db = SessionLocal()
        try:
            job = db.get(InstagramServiceJob, job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "queued"
            job.worker_id = ""
            job.assigned_at = None
            job.attempt_count = max(0, (job.attempt_count or 1) - 1)
            job.not_before = _now() + timedelta(seconds=ACCOUNT_REQUEUE_SECONDS)
            order = db.get(Order, order_id)
            if order is not None and kind == "diagnostic" and order.status == "processing":
                order.status = "pending"
            if job_id not in self._waiting_logged:
                db.add(OrderLog(order_id=order_id, event_type="waiting_account",
                                message=f"Job #{job_id}: all eligible accounts are busy or resting; will retry automatically"))
                self._waiting_logged.add(job_id)
            db.commit()
        finally:
            db.close()

    # ---- page helpers --------------------------------------------------
    async def _dwell(self, cfg: dict) -> None:
        await asyncio.sleep(random.uniform(cfg["dwell_min"], cfg["dwell_max"]))

    async def _wait_ready(self, page, cfg: dict, selectors: tuple[str, ...]) -> Optional[str]:
        """Wait for the page to settle and a content element to exist.
        Returns the selector that matched, or None."""
        try:
            await page.wait_for_load_state("networkidle", timeout=min(15000, cfg["timeout_ms"]))
        except Exception:
            pass  # Instagram keeps long-lived connections open; content check below decides
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, state="attached", timeout=min(20000, cfg["timeout_ms"]))
                return sel
            except Exception:
                continue
        return None

    async def _body_text(self, page) -> str:
        try:
            return (await page.locator("body").inner_text(timeout=5000)).lower()
        except Exception:
            return ""

    async def inspect_target(self, page, target_url: str, service: str, cfg: dict) -> dict:
        """Navigate to the order target, wait for readiness, classify the
        page and read public metadata. Raises DiagnosticError."""
        try:
            await page.goto(target_url, wait_until="load", timeout=cfg["timeout_ms"])
        except Exception as e:
            raise DiagnosticError("target_unreachable", f"{type(e).__name__}: {str(e)[:150]}", category="target")
        ready = await self._wait_ready(page, cfg, ("main article, main video, main header, main",))
        await self._dwell(cfg)
        final_url = page.url
        body = await self._body_text(page)
        reason = detect_restriction(final_url, body)
        if reason:
            raise DiagnosticError("restricted", reason, category="restriction")
        if any(m in body for m in NOT_FOUND_MARKERS):
            raise DiagnosticError("target_not_found", "Instagram reports this page is not available",
                                  category="target", retryable=False)
        login_walled = "/accounts/login" in (final_url or "")
        if not login_walled:
            try:
                login_walled = bool(await page.query_selector('input[name="username"]'))
            except Exception:
                pass
        if login_walled:
            raise DiagnosticError("target_login_walled", "target redirected to a login page", category="session")
        if not ready:
            raise DiagnosticError("target_not_loaded", "page content did not appear in time", category="target")
        meta: dict[str, Any] = {"final_url": final_url, "ready_selector": ready}
        for key, sel in (("title", None), ("og_title", 'meta[property="og:title"]'),
                         ("og_description", 'meta[property="og:description"]'),
                         ("description", 'meta[name="description"]')):
            try:
                meta[key] = await page.title() if sel is None else await page.locator(sel).first.get_attribute("content", timeout=3000)
            except Exception:
                meta[key] = None
        metric, value = parse_metric(service, meta.get("og_description") or meta.get("description") or "")
        meta["metric"], meta["metric_value"] = metric, value
        return meta

    # ---- the real browser diagnostic ------------------------------------
    async def _run_diagnostic(self, job_id: int, order_id: int, target_url: str, service: str,
                              account: dict, cfg: dict) -> dict:
        if not PLAYWRIGHT_AVAILABLE:
            raise DiagnosticError("playwright_missing", "Playwright is not installed on this server", retryable=False)
        profile_path = Path(account["profile_path"])
        if not profile_path.exists() or not any(profile_path.iterdir()):
            raise DiagnosticError("session_missing", "account has no persistent profile on this host", category="session")

        headless = (not cfg["show_browser"]) or not _has_display()
        ts = time.strftime("%Y%m%d_%H%M%S")
        debug_dir = DEBUG_DIR / f"order_{order_id}_job_{job_id}_{ts}"
        debug_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, Any] = {"account_id": account["id"], "account_name": account["name"],
                                  "headless": headless, "debug_dir": str(debug_dir),
                                  "session_healthy": False, "target_accessible": False}

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path), headless=headless,
                viewport={"width": 1280, "height": 800},
                args=["--no-first-run", "--no-default-browser-check"])
            self._add_log(order_id, "browser_opened",
                          f"Job #{job_id}: Chromium started for '{account['name']}' "
                          f"({'background / headless' if headless else 'visible (debug setting on)'})")
            page = None
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(cfg["timeout_ms"])

                # Step 1 — session health on the home feed
                self._check_cancelled(job_id)
                try:
                    await page.goto("https://www.instagram.com/", wait_until="load", timeout=cfg["timeout_ms"])
                except Exception as e:
                    raise DiagnosticError("instagram_unreachable", f"{type(e).__name__}: {str(e)[:150]}")
                await self._wait_ready(page, cfg, ("main, nav, form",))
                await self._dwell(cfg)
                reason = detect_restriction(page.url, await self._body_text(page))
                if reason:
                    raise DiagnosticError("restricted", reason, category="restriction")
                try:
                    cookies = await context.cookies("https://www.instagram.com/")
                    has_session = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
                except Exception:
                    has_session = False
                login_redirect = "/accounts/login" in (page.url or "")
                try:
                    login_form = bool(await page.query_selector('input[name="username"]'))
                except Exception:
                    login_form = False
                if not (has_session and not login_redirect and not login_form):
                    raise DiagnosticError("session_unhealthy",
                                          f"sessionid={'present' if has_session else 'missing'}, "
                                          f"login_redirect={login_redirect}, login_form={login_form}",
                                          category="session")
                result["session_healthy"] = True
                self._add_log(order_id, "session_healthy", f"Job #{job_id}: session verified (page fully loaded)")

                # Step 2 — target navigation, readiness and metadata
                self._check_cancelled(job_id)
                meta = await self.inspect_target(page, target_url, service, cfg)
                result.update({"target_accessible": True, **{k: meta.get(k) for k in
                               ("final_url", "title", "og_title", "metric", "metric_value", "ready_selector")}})
                self._add_log(order_id, "target_accessible",
                              f"Job #{job_id}: target loaded ({meta.get('ready_selector')}) — {meta.get('title') or meta.get('final_url')}")

                # Step 3 — permitted capture
                self._check_cancelled(job_id)
                meta.update({"order_id": order_id, "job_id": job_id, "source_url": target_url,
                             "headless": headless, "captured_at": _now().isoformat()})
                (debug_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
                try:
                    await page.screenshot(path=str(debug_dir / "screenshot.png"), full_page=True)
                    result["screenshot_path"] = str(debug_dir / "screenshot.png")
                except Exception as e:
                    self._add_log(order_id, "screenshot_failed", f"Job #{job_id}: screenshot failed: {type(e).__name__}")
                result["meta_path"] = str(debug_dir / "meta.json")
                return result
            except JobCancelled:
                raise
            except Exception:
                try:
                    if page is not None:
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

    # ---- outcomes --------------------------------------------------------
    def _on_success(self, job_id: int, order_id: int, kind: str, result: dict, cfg: dict) -> None:
        metric, value = result.get("metric"), result.get("metric_value")
        db = SessionLocal()
        try:
            job = db.get(InstagramServiceJob, job_id)
            order = db.get(Order, order_id)
            if job is None or order is None:
                return
            job.status = "succeeded"
            job.error = ""
            job.result = json.dumps(result, default=str)
            job.finished_at = _now()
            if metric and value is not None:
                db.add(Observation(order_id=order_id, kind="baseline" if kind == "diagnostic" else "followup",
                                   metric=metric, value=value, source="public page metadata"))
            if kind == "diagnostic":
                if order.status in ("pending", "processing"):
                    order.status = "verified"
                    order.completed_at = _now()
                count_txt = (f"public {metric}: {value}" if value is not None
                             else f"public {metric or 'count'} not exposed for this service")
                db.add(OrderLog(order_id=order_id, event_type="verified",
                                message=(f"Job #{job_id} VERIFIED — session healthy, target accessible; {count_txt}. "
                                         f"Delivered: 0 (requested {order.requested_quantity} is a reference only)")))
                if metric and value is not None:
                    fu = InstagramServiceJob(order_id=order_id, status="queued", kind="followup",
                                             max_attempts=1 + cfg["max_retries"],
                                             not_before=_now() + timedelta(minutes=cfg["followup_min"]))
                    db.add(fu)
                    db.flush()
                    db.add(OrderLog(order_id=order_id, event_type="followup_scheduled",
                                    message=f"Follow-up job #{fu.id} will re-read the public {metric} count in {cfg['followup_min']} min"))
            else:
                base = (db.query(Observation).filter(Observation.order_id == order_id, Observation.kind == "baseline")
                        .order_by(Observation.id.asc()).first())
                delta = (value - base.value) if (base and value is not None) else None
                db.add(OrderLog(order_id=order_id, event_type="observed_growth",
                                message=(f"Follow-up job #{job_id}: public {metric} now {value}"
                                         + (f" ({delta:+d} since baseline; observed, not delivered by this system)" if delta is not None else ""))))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("success handling for job %s failed", job_id)
        finally:
            db.close()

    def _handle_failure(self, job_id: int, attempt_count: int, kind: str, cfg: dict, error: str,
                        retryable: bool = True) -> None:
        db = SessionLocal()
        requeued = False
        try:
            job = db.get(InstagramServiceJob, job_id)
            if job is None or job.status == "cancelled":
                return
            if retryable and attempt_count < job.max_attempts:
                backoff = cfg["retry_backoff"][min(attempt_count - 1, len(cfg["retry_backoff"]) - 1)]
                job.status = "queued"
                job.error = error[:500]
                job.worker_id = ""
                job.assigned_at = None
                job.not_before = _now() + timedelta(seconds=backoff)
                db.add(OrderLog(order_id=job.order_id, event_type="retry_scheduled",
                                message=f"Job #{job_id} failed ({error[:200]}); retry {attempt_count}/{job.max_attempts - 1} in {backoff}s"))
                requeued = True
            else:
                job.status = "failed"
                job.error = error[:500]
                job.finished_at = _now()
                db.add(OrderLog(order_id=job.order_id, event_type="diagnostic_failed",
                                message=f"Job #{job_id} FAILED after {attempt_count} attempt(s): {error}"))
            order = db.get(Order, job.order_id)
            if order is not None and kind == "diagnostic":
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

    def _finish_job(self, job_id: int, status: str, error: str = "", order_status: str = "",
                    log_line: str = "") -> None:
        db = SessionLocal()
        try:
            job = db.get(InstagramServiceJob, job_id)
            if job is None:
                return
            job.status = status
            job.error = error[:500]
            job.finished_at = _now()
            order = db.get(Order, job.order_id)
            if order is not None and order_status and order.status in ("pending", "processing"):
                order.status = order_status
                order.completed_at = _now()
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


# Module-level singleton
diagnostics_worker = InstagramDiagnosticsWorker()
