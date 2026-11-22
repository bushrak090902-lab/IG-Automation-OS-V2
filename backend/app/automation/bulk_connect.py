"""Phase 2b: Bulk-import & Bulk-connect engine.

Runs automated Instagram login for a batch of CSV-supplied accounts using
persistent Playwright profiles, then handles the standard post-login popup
sequence, then verifies the session.

Design contract:
    * Does NOT modify or import mutable state from `publisher.py`,
      `publisher_queue.py`, or the login logic of `browser_session.py`.
    * Reuses these READ-ONLY primitives from `browser_session`:
        - `_lock_for(account_id)`     : per-account async lock (never two
                                        browsers on the same profile).
        - `_has_display()`            : Windows display detection.
        - `_update_account(...)`      : safe DB write helper.
        - `_write_log(...)`           : safe DB log helper.
        - `_verify_persistent_profile(profile_path, expected_username)` :
                                        session validation (identity check).
        - `PLAYWRIGHT_AVAILABLE`      : import-safety flag.
    * Passwords live in memory ONLY. Never persisted to disk, never logged.
    * Publishing flow is completely untouched.

Per-account state machine (values used by the API + UI):
    pending          initial
    logging_in       browser open, filling credentials
    popups           login succeeded, handling save-info / notifications / …
    otp_required     Instagram prompted for a 2FA / login code — waiting
                     for the operator to POST /accounts/{i}/otp
    checkpoint_required
                     Instagram suspects activity — account parked, requires
                     manual reconnect in a real browser
    connected        session verified, profile saved, done
    failed           terminal error (wrong password, network, timeout)
    retrying         transient failure, will re-attempt within same job
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..database import SessionLocal, BASE_DIR
from ..models import Account
from ..services.logger import log as db_log
from . import browser_session  # READ-ONLY: only call exported helpers

# Re-export for callers.
PLAYWRIGHT_AVAILABLE = browser_session.PLAYWRIGHT_AVAILABLE


PROFILES_DIR = BASE_DIR / "profiles"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Timings (all Playwright waits; no fixed sleeps in the flow logic)
# ---------------------------------------------------------------------------
LOGIN_NAV_TIMEOUT_MS = 45_000
LOGIN_INPUT_TIMEOUT_MS = 20_000
LOGIN_SUBMIT_TIMEOUT_MS = 20_000
POST_LOGIN_TIMEOUT_MS = 30_000
POPUP_WINDOW_MS = 20_000          # how long we scan for popups after login
OTP_WAIT_TIMEOUT_S = 15 * 60      # 15 min for the operator to submit OTP


# ---------------------------------------------------------------------------
# In-memory job state
# ---------------------------------------------------------------------------
@dataclass
class BulkAccountState:
    index: int
    username: str
    status: str = "pending"      # see state machine above
    message: str = ""
    account_id: Optional[int] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempt_count: int = 0

    def public(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "username": self.username,
            "status": self.status,
            "message": self.message,
            "account_id": self.account_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempt_count": self.attempt_count,
        }


@dataclass
class BulkJob:
    job_id: str
    created_at: str
    accounts: List[BulkAccountState] = field(default_factory=list)
    # Passwords in-memory only, keyed by account index.
    _passwords: Dict[int, str] = field(default_factory=dict)
    # OTP codes supplied by the operator, keyed by account index.
    _otp_codes: Dict[int, str] = field(default_factory=dict)
    task: Optional[asyncio.Task] = None
    status: str = "idle"          # idle | running | completed | cancelled
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    processed_count: int = 0

    def status_snapshot(self) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        for a in self.accounts:
            by_status[a.status] = by_status.get(a.status, 0) + 1

        total = len(self.accounts)
        connected = by_status.get("connected", 0)
        failed = by_status.get("failed", 0) + by_status.get("checkpoint_required", 0)
        otp = by_status.get("otp_required", 0)
        running = by_status.get("logging_in", 0) + by_status.get("popups", 0) + by_status.get("retrying", 0)
        remaining = total - connected - failed - otp - running

        # ETA: simple average of per-account wall time so far.
        eta_seconds: Optional[float] = None
        if self.started_at and self.processed_count > 0:
            elapsed = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(self.started_at)
            ).total_seconds()
            avg = elapsed / max(1, self.processed_count)
            eta_seconds = avg * max(0, total - self.processed_count)

        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "totals": {
                "total": total,
                "connected": connected,
                "failed": failed,
                "otp_required": otp,
                "running": running,
                "remaining": remaining,
                "processed": self.processed_count,
                "by_status": by_status,
            },
            "eta_seconds": eta_seconds,
            "accounts": [a.public() for a in self.accounts],
        }


# ---------------------------------------------------------------------------
# In-memory job registry (single-process)
# ---------------------------------------------------------------------------
class _Registry:
    def __init__(self) -> None:
        self._jobs: Dict[str, BulkJob] = {}
        self._current_job_id: Optional[str] = None

    def create(self, rows: List[Dict[str, str]]) -> BulkJob:
        job_id = uuid.uuid4().hex[:12]
        job = BulkJob(
            job_id=job_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        for i, row in enumerate(rows):
            username = row["username"]
            job.accounts.append(BulkAccountState(index=i, username=username))
            job._passwords[i] = row["password"]
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[BulkJob]:
        return self._jobs.get(job_id)

    def current(self) -> Optional[BulkJob]:
        if self._current_job_id:
            return self._jobs.get(self._current_job_id)
        return None

    def set_current(self, job_id: Optional[str]) -> None:
        self._current_job_id = job_id


REGISTRY = _Registry()


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------
class CsvParseError(ValueError):
    pass


def parse_csv(data_bytes: bytes) -> List[Dict[str, str]]:
    """Parse a `username,password` CSV. Tolerates BOM, whitespace, blank rows."""
    try:
        text = data_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data_bytes.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvParseError("CSV is empty or missing a header row.")

    normalized = {n.strip().lower(): n for n in reader.fieldnames}
    if "username" not in normalized or "password" not in normalized:
        raise CsvParseError("CSV header must contain both 'username' and 'password' columns.")

    u_col = normalized["username"]
    p_col = normalized["password"]

    rows: List[Dict[str, str]] = []
    seen: set = set()
    for i, raw in enumerate(reader, start=2):
        username = (raw.get(u_col) or "").strip().lstrip("@")
        password = (raw.get(p_col) or "")
        if not username or not password:
            continue
        key = username.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"username": username, "password": password})

    if not rows:
        raise CsvParseError("CSV contained no valid (username, password) rows.")
    if len(rows) > 5000:
        raise CsvParseError("Maximum 5000 accounts per bulk import.")
    return rows


# ---------------------------------------------------------------------------
# DB: ensure Account row + profile folder exist for a given username
# ---------------------------------------------------------------------------
def _ensure_account_row(username: str) -> Account:
    db = SessionLocal()
    try:
        acc = db.query(Account).filter(Account.account_name == username).first()
        if acc is None:
            acc = Account(
                account_name=username,
                profile_path="",
                session_status="unknown",
                account_status="enabled",
            )
            db.add(acc)
            db.flush()  # allocate id
            profile_path = PROFILES_DIR / f"account_{acc.id:04d}"
            profile_path.mkdir(parents=True, exist_ok=True)
            acc.profile_path = str(profile_path)
            db_log(
                db,
                "INFO",
                f"Bulk import created account '{username}' — profile {profile_path.name}",
                account_id=acc.id,
            )
            db.commit()
            db.refresh(acc)
        else:
            # Make sure the profile folder exists (e.g. wiped by mistake).
            if acc.profile_path:
                Path(acc.profile_path).mkdir(parents=True, exist_ok=True)
        return acc
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Popup handler (Feature 4). Never touches publisher.py. Uses Playwright
# waits only — no fixed sleeps.
# ---------------------------------------------------------------------------
CLICK_YES_TEXTS = [
    # ONLY the ones we explicitly want to say "yes" to.
    "Save Info",
    "Save info",
]

CLICK_DISMISS_TEXTS = [
    "Not Now",
    "Not now",
    "Skip",
    "Maybe Later",
    "Maybe later",
]

# Never enable these; never click "Turn On", "Enable Contacts", "Sync Contacts".
FORBIDDEN_TEXTS = [
    "Turn On",
    "Turn on",
    "Enable Contacts",
    "Enable contacts",
    "Sync Contacts",
    "Sync contacts",
]


async def _click_visible_text(page, text: str, timeout_ms: int = 1500) -> bool:
    """Try to click the first visible button/link/role=button with EXACT text.
    Returns True on click, False if not visible within timeout_ms.
    """
    selectors = [
        f'button:has-text("{text}")',
        f'div[role="button"]:has-text("{text}")',
        f'a:has-text("{text}")',
        f'span:has-text("{text}")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                # Playwright wait, no fixed sleep.
                if await loc.is_visible():
                    await loc.click(timeout=timeout_ms)
                    return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Resilient login-input finder + diagnostic artifact writer.
#
# Instagram's login form is React-hydrated: the DOM has `#loading` / skeleton
# nodes when `domcontentloaded` fires, and the real `<input name="username">`
# only appears after the client-side bundle mounts. On slower machines this
# means a strict `input[name="username"]` wait can time out even though the
# form is about to appear. Also, IG occasionally A/B-tests different
# attributes (aria-label, autocomplete) across regions and app versions.
#
# The helper below:
#   1. Waits until `document.readyState === "complete"`.
#   2. Cycles the 4 known input variants for up to `total_timeout_ms`.
#   3. Returns the first visible locator + which selector matched.
#   4. On timeout: writes URL / title / HTML / screenshot to
#      backend/logs/bulk_connect_debug/ and raises `LoginInputNotFound`
#      with a descriptive message (never a raw Playwright TimeoutError).
# ---------------------------------------------------------------------------
BULK_DEBUG_DIR = BASE_DIR / "logs" / "bulk_connect_debug"
BULK_DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# Ordered from most specific → most general. First visible match wins.
LOGIN_USERNAME_SELECTORS = (
    'input[aria-label="Phone number, username or email"]',
    'input[autocomplete="username"]',
    'input[name="username"]',
    'input[type="text"]',
)


class LoginInputNotFound(RuntimeError):
    """Raised when the login username input cannot be located within the
    timeout. Carries the paths of the diagnostic artifacts saved to disk.
    """

    def __init__(self, message: str, artifacts: Dict[str, str]):
        super().__init__(message)
        self.artifacts = artifacts


async def _dump_debug_artifacts(page, tag: str) -> Dict[str, str]:
    """Best-effort save of URL / title / HTML / screenshot for diagnostics.
    Never raises; returns a dict of what was actually written.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)[:80] or "account"
    base = BULK_DEBUG_DIR / f"{safe_tag}_{ts}"
    artifacts: Dict[str, str] = {}
    try:
        artifacts["url"] = page.url
    except Exception:
        pass
    try:
        artifacts["title"] = await page.title()
    except Exception:
        pass
    try:
        html_path = base.with_suffix(".html")
        html_path.write_text(await page.content(), encoding="utf-8")
        artifacts["html"] = str(html_path)
    except Exception:
        pass
    try:
        png_path = base.with_suffix(".png")
        await page.screenshot(path=str(png_path), full_page=True)
        artifacts["screenshot"] = str(png_path)
    except Exception:
        pass
    return artifacts


async def _wait_ready_state_complete(page, timeout_ms: int) -> bool:
    """Wait until `document.readyState === "complete"`. Returns False on timeout."""
    try:
        await page.wait_for_function(
            "() => document.readyState === 'complete'",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


async def _find_login_username_input(
    page,
    total_timeout_ms: int,
    tag: str,
):
    """Return (locator, matched_selector) as soon as any of the fallback
    selectors becomes visible. Raises LoginInputNotFound after the timeout
    (never a raw TimeoutError).
    """
    # (a) Let the SPA finish mounting.
    await _wait_ready_state_complete(page, min(total_timeout_ms, 15_000))

    # (b) Poll the fallback list.
    deadline = time.monotonic() + (total_timeout_ms / 1000.0)
    last_seen_but_not_visible = None
    while time.monotonic() < deadline:
        for sel in LOGIN_USERNAME_SELECTORS:
            try:
                loc = page.locator(sel).first
                cnt = await loc.count()
                if cnt <= 0:
                    continue
                try:
                    if await loc.is_visible():
                        return loc, sel
                except Exception:
                    pass
                last_seen_but_not_visible = sel
            except Exception:
                continue
        # No visible match yet — yield and retry.
        await asyncio.sleep(0.4)

    # (c) Timed out — dump diagnostics + raise a descriptive error.
    artifacts = await _dump_debug_artifacts(page, f"login_input_not_found_{tag}")
    detail_bits = [f"tried={list(LOGIN_USERNAME_SELECTORS)}"]
    if last_seen_but_not_visible:
        detail_bits.append(f"present_but_hidden={last_seen_but_not_visible!r}")
    if artifacts.get("url"):
        detail_bits.append(f"url={artifacts['url']}")
    if artifacts.get("title"):
        detail_bits.append(f"title={artifacts['title']!r}")
    for k in ("html", "screenshot"):
        if artifacts.get(k):
            detail_bits.append(f"{k}={artifacts[k]}")
    raise LoginInputNotFound(
        "Instagram login form did not expose a username input within "
        f"{total_timeout_ms / 1000:.0f}s. " + " · ".join(detail_bits),
        artifacts,
    )


async def _handle_popups(page, job_log) -> None:
    """Feature 4: cycle for POPUP_WINDOW_MS looking for known popups.

    Rules:
      * Save Info dialog        → click "Save Info"
      * Notifications dialog    → click "Not Now"
      * Everything else         → click Skip / Not Now / Maybe Later / Close (X)
      * Never click Turn On / Enable / Sync Contacts
    """
    deadline = time.monotonic() + (POPUP_WINDOW_MS / 1000.0)
    last_action = time.monotonic()

    while time.monotonic() < deadline:
        # Fast bail: if home feed is loaded and no dialogs are visible, exit.
        try:
            dialog_count = await page.locator('div[role="dialog"], div[role="alertdialog"]').count()
        except Exception:
            dialog_count = 0

        clicked_something = False

        # (a) Save Info dialog — click SAVE INFO (yes).
        if dialog_count > 0:
            for t in CLICK_YES_TEXTS:
                if await _click_visible_text(page, t):
                    job_log(f"popup: clicked '{t}'")
                    clicked_something = True
                    last_action = time.monotonic()
                    break

        # (b) Any dismissive text: Not Now / Skip / Maybe Later.
        if not clicked_something:
            for t in CLICK_DISMISS_TEXTS:
                if await _click_visible_text(page, t):
                    job_log(f"popup: clicked '{t}'")
                    clicked_something = True
                    last_action = time.monotonic()
                    break

        # (c) Close (X) button on any lingering dialog.
        if not clicked_something and dialog_count > 0:
            try:
                x_loc = page.locator('div[role="dialog"] [aria-label="Close"]').first
                if await x_loc.count() > 0 and await x_loc.is_visible():
                    await x_loc.click(timeout=1500)
                    job_log("popup: clicked (X) close")
                    clicked_something = True
                    last_action = time.monotonic()
            except Exception:
                pass

        # Idle exit: no dialogs and no action for 4s.
        if not clicked_something and (time.monotonic() - last_action) > 4.0 and dialog_count == 0:
            return

        # Yield to the event loop; Playwright resolves DOM updates.
        await asyncio.sleep(0.6)


# ---------------------------------------------------------------------------
# Login state detection
# ---------------------------------------------------------------------------
async def _detect_post_submit_state(page, expected_username: str) -> str:
    """After clicking Log In, what state are we in?
    Returns one of: 'logged_in' | 'otp_required' | 'checkpoint_required' |
                    'bad_password' | 'unknown'
    """
    # Give the SPA a moment to settle without a hard sleep — poll signals.
    deadline = time.monotonic() + (POST_LOGIN_TIMEOUT_MS / 1000.0)
    while time.monotonic() < deadline:
        current = (page.url or "").split("?")[0]

        # Checkpoint / challenge redirect.
        if "/challenge/" in current or "/accounts/suspended/" in current:
            return "checkpoint_required"

        # 2FA / login-code page.
        if "/two_factor/" in current or "/accounts/login/two_factor" in current:
            return "otp_required"

        try:
            body = (await page.locator("body").inner_text(timeout=1500)) or ""
        except Exception:
            body = ""

        low = body.lower()
        if "we suspect this activity" in low or "help us confirm" in low or "unusual login attempt" in low:
            return "checkpoint_required"

        if (
            "enter the code" in low
            or "we sent you a code" in low
            or "we sent a code" in low
            or "security code" in low
        ):
            # But make sure this isn't the password-reset help link.
            try:
                otp_input = page.locator(
                    'input[name="verificationCode"], input[aria-label*="code" i], input[autocomplete="one-time-code"]'
                ).first
                if await otp_input.count() > 0:
                    return "otp_required"
            except Exception:
                pass

        if (
            "your password was incorrect" in low
            or "sorry, your password was incorrect" in low
            or "the username you entered doesn" in low
            or "please wait a few minutes before" in low
        ):
            return "bad_password"

        # Signals that we've reached a logged-in surface:
        try:
            cookies = await page.context.cookies("https://www.instagram.com/")
            has_session = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
        except Exception:
            has_session = False

        on_login = "/accounts/login" in current
        try:
            login_form = await page.locator('input[name="password"]').count() > 0
        except Exception:
            login_form = False

        if has_session and not on_login and not login_form:
            return "logged_in"

        await asyncio.sleep(0.7)

    return "unknown"


# ---------------------------------------------------------------------------
# Per-account worker: automated login + popups + verify
# ---------------------------------------------------------------------------
async def _connect_one_account(
    job: BulkJob,
    state: BulkAccountState,
    password: str,
) -> None:
    """Runs the full connect flow for ONE account. Updates `state` in place.
    Does NOT raise; every terminal outcome sets state.status accordingly.
    """
    from playwright.async_api import async_playwright

    state.attempt_count += 1
    state.started_at = datetime.now(timezone.utc).isoformat()

    def _log(msg: str, level: str = "INFO") -> None:
        # DB log (never contains password)
        try:
            browser_session._write_log(
                state.account_id, level, f"BulkConnect '{state.username}': {msg}"
            )
        except Exception:
            pass
        state.message = msg

    # 1) Ensure Account row + profile path
    try:
        acc = _ensure_account_row(state.username)
        state.account_id = acc.id
        profile_path = acc.profile_path
    except Exception as e:
        state.status = "failed"
        state.message = f"DB error: {type(e).__name__}: {e}"
        state.completed_at = datetime.now(timezone.utc).isoformat()
        return

    lock = browser_session._lock_for(state.account_id)
    if lock.locked():
        state.status = "failed"
        state.message = "Account is already active in another session."
        state.completed_at = datetime.now(timezone.utc).isoformat()
        return

    async with lock:
        state.status = "logging_in"
        _log("launching persistent context")

        try:
            browser_session._update_account(
                state.account_id, session_status="connecting"
            )
        except Exception:
            pass

        try:
            async with async_playwright() as p:
                # Same off-screen headful pattern as the Phase-2 publisher on
                # Windows — matches PUBLISH_HEADLESS=off_screen so the bulk
                # login never disturbs the operator's desktop, but headful
                # is what IG expects for real logins.
                launch_args = [
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-position=-32000,-32000",
                    "--window-size=1280,900",
                ]
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_path,
                    headless=not browser_session._has_display(),
                    viewport={"width": 1280, "height": 900},
                    args=launch_args,
                )

                try:
                    page = context.pages[0] if context.pages else await context.new_page()

                    # 2) Navigate to login. `wait_until="load"` corresponds
                    # to `document.readyState === "complete"`, which gives
                    # React time to hydrate the login form before we try to
                    # find its inputs.
                    await page.goto(
                        "https://www.instagram.com/accounts/login/",
                        wait_until="load",
                        timeout=LOGIN_NAV_TIMEOUT_MS,
                    )

                    # 3) Fill credentials — resilient username finder that
                    # tries 4 selector variants and, on timeout, saves URL /
                    # title / HTML / screenshot to backend/logs/
                    # bulk_connect_debug/ instead of raising a bare
                    # TimeoutError.
                    try:
                        user_input, matched_sel = await _find_login_username_input(
                            page,
                            total_timeout_ms=LOGIN_INPUT_TIMEOUT_MS,
                            tag=state.username,
                        )
                    except LoginInputNotFound as e:
                        state.status = "failed"
                        state.message = str(e)
                        try:
                            browser_session._update_account(
                                state.account_id, session_status="unknown"
                            )
                        except Exception:
                            pass
                        state.completed_at = datetime.now(timezone.utc).isoformat()
                        return
                    _log(f"located username input via {matched_sel!r}")
                    await user_input.fill(state.username)

                    pw_input = page.locator('input[name="password"]').first
                    await pw_input.wait_for(state="visible", timeout=LOGIN_INPUT_TIMEOUT_MS)
                    await pw_input.fill(password)

                    submit = page.locator('button[type="submit"]').first
                    await submit.wait_for(state="visible", timeout=LOGIN_INPUT_TIMEOUT_MS)
                    _log("submitting credentials")
                    await submit.click(timeout=LOGIN_SUBMIT_TIMEOUT_MS)

                    # 4) Detect post-submit state
                    outcome = await _detect_post_submit_state(page, state.username)

                    if outcome == "otp_required":
                        state.status = "otp_required"
                        _log("Instagram asked for a verification code", "WARNING")
                        # Wait for the operator to submit an OTP via API.
                        code = await _await_otp(job, state.index, OTP_WAIT_TIMEOUT_S)
                        if code is None:
                            state.status = "failed"
                            state.message = "Timed out waiting for OTP."
                            state.completed_at = datetime.now(timezone.utc).isoformat()
                            return
                        _log("received OTP, submitting")
                        # Fill + submit
                        otp_input = page.locator(
                            'input[name="verificationCode"], input[autocomplete="one-time-code"], input[aria-label*="code" i]'
                        ).first
                        await otp_input.wait_for(state="visible", timeout=15_000)
                        await otp_input.fill(code)
                        # Try dedicated Confirm / Next button.
                        for t in ("Confirm", "Next", "Submit"):
                            try:
                                btn = page.locator(f'button:has-text("{t}"), div[role="button"]:has-text("{t}")').first
                                if await btn.count() > 0 and await btn.is_visible():
                                    await btn.click(timeout=8000)
                                    break
                            except Exception:
                                continue
                        outcome = await _detect_post_submit_state(page, state.username)

                    if outcome == "checkpoint_required":
                        state.status = "checkpoint_required"
                        state.message = (
                            "Instagram flagged this login as suspicious. "
                            "Reconnect manually in the Accounts page."
                        )
                        try:
                            browser_session._update_account(
                                state.account_id, session_status="login_required"
                            )
                        except Exception:
                            pass
                        state.completed_at = datetime.now(timezone.utc).isoformat()
                        return

                    if outcome == "bad_password":
                        state.status = "failed"
                        state.message = "Incorrect username or password."
                        try:
                            browser_session._update_account(
                                state.account_id, session_status="login_required"
                            )
                        except Exception:
                            pass
                        state.completed_at = datetime.now(timezone.utc).isoformat()
                        return

                    if outcome != "logged_in":
                        state.status = "failed"
                        state.message = f"Unknown login state: {outcome}"
                        state.completed_at = datetime.now(timezone.utc).isoformat()
                        return

                    # 5) Popups
                    state.status = "popups"
                    _log("login OK, handling popups")
                    await _handle_popups(page, lambda m: _log(m))

                    # 6) Verify session via the SAME helper the manual flow uses.
                    #    This proves the persistent context is actually authed
                    #    as `expected_username` (identity lock).
                    _log("verifying session")

                finally:
                    # Close context so the persistent state flushes to disk.
                    try:
                        await context.close()
                    except Exception:
                        pass

            # Verify OUTSIDE the context (uses its own headless launch)
            verify_status = await browser_session._verify_persistent_profile(
                Path(profile_path), state.username
            )

            if verify_status == "connected":
                state.status = "connected"
                state.message = "Session verified."
                try:
                    browser_session._update_account(
                        state.account_id,
                        session_status="connected",
                        last_used_at=datetime.now(timezone.utc),
                    )
                except Exception:
                    pass
            else:
                state.status = "failed"
                state.message = f"Post-login verify returned '{verify_status}'."
                try:
                    browser_session._update_account(
                        state.account_id, session_status="login_required"
                    )
                except Exception:
                    pass

        except Exception as e:
            state.status = "failed"
            state.message = f"{type(e).__name__}: {str(e)[:240]}"
            try:
                browser_session._update_account(
                    state.account_id, session_status="unknown"
                )
            except Exception:
                pass

        state.completed_at = datetime.now(timezone.utc).isoformat()


async def _await_otp(job: BulkJob, account_index: int, timeout_s: int) -> Optional[str]:
    """Poll the job._otp_codes dict until the operator submits a code."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        code = job._otp_codes.pop(account_index, None)
        if code:
            return code.strip()
        await asyncio.sleep(1.0)
    return None


# ---------------------------------------------------------------------------
# Job driver (sequential; skips OTP-blocked accounts, resumes on operator input)
# ---------------------------------------------------------------------------
async def _run_job(job: BulkJob) -> None:
    if not PLAYWRIGHT_AVAILABLE:
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc).isoformat()
        for st in job.accounts:
            if st.status == "pending":
                st.status = "failed"
                st.message = "Playwright not installed on this host."
        return

    job.status = "running"
    job.started_at = datetime.now(timezone.utc).isoformat()

    try:
        for state in job.accounts:
            if state.status not in ("pending", "retrying"):
                continue
            password = job._passwords.get(state.index, "")
            if not password:
                state.status = "failed"
                state.message = "Password missing from CSV input."
                continue
            try:
                await _connect_one_account(job, state, password)
            except Exception as e:
                state.status = "failed"
                state.message = f"Unhandled: {type(e).__name__}: {e}"
            finally:
                # Clear password from memory as soon as this account finishes,
                # regardless of outcome.
                job._passwords.pop(state.index, None)
                if state.status in ("connected", "failed", "checkpoint_required"):
                    job.processed_count += 1
    finally:
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API for the router
# ---------------------------------------------------------------------------
def start_job(rows: List[Dict[str, str]]) -> BulkJob:
    existing = REGISTRY.current()
    if existing and existing.status == "running":
        raise RuntimeError(
            "A bulk-connect job is already running. Wait for it to finish."
        )
    job = REGISTRY.create(rows)
    REGISTRY.set_current(job.job_id)
    job.task = asyncio.create_task(_run_job(job))
    return job


def submit_otp(job_id: str, account_index: int, code: str) -> bool:
    job = REGISTRY.get(job_id)
    if not job:
        return False
    st = next((a for a in job.accounts if a.index == account_index), None)
    if not st:
        return False
    if st.status != "otp_required":
        return False
    if not code or not code.strip():
        return False
    job._otp_codes[account_index] = code.strip()
    return True


def get_status(job_id: str) -> Optional[Dict[str, Any]]:
    job = REGISTRY.get(job_id)
    if not job:
        return None
    return job.status_snapshot()


def current_job() -> Optional[BulkJob]:
    return REGISTRY.current()
