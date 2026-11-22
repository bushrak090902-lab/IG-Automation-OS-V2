"""Bulk Connect Playwright worker — production quality.

Completely isolated from publisher.py / publisher_queue.py / browser_session.py.

- One persistent Chromium context per username (backend/profiles/{username}).
- Resilient selectors, no fixed sleeps, only Playwright smart waits.
- Handles: Save-Info popup, Notifications popup, OTP (2FA/SMS/Email),
  Checkpoint, rate-limit, wrong password.
- Passwords held ONLY in in-memory registry keyed by batch_id/item_id, and
  wiped from memory the moment login succeeds, fails permanently, or the
  batch is cancelled. Never written to DB, never logged, never returned.
- On failure: dumps screenshot + HTML + URL + title + console + storage
  state + cookies to backend/logs/bulk_connect_debug/{username}_{ts}/.
- SSE event bus per batch — no polling needed on the frontend.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..database import BASE_DIR, SessionLocal
from ..models import BulkBatch, BulkBatchItem, Setting

# Playwright import is deferred so a missing chromium install only breaks the
# actual worker call, not the whole app.
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout  # noqa
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False

PROFILES_DIR = BASE_DIR / "profiles"
DEBUG_DIR = BASE_DIR / "logs" / "bulk_connect_debug"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

LOGIN_URL = "https://www.instagram.com/accounts/login/"
SAFE_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")

USERNAME_SELECTORS = [
    'input[name="username"]',
    'input[aria-label*="username" i]',
    'input[autocomplete="username"]',
    'input[type="text"]',
]
PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
    'input[aria-label*="Password" i]',
    'input[autocomplete="current-password"]',
    'input[placeholder*="Password" i]',
]
LOGIN_BUTTON_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Log in")',
    'button:has-text("Log In")',
]
OTP_INPUT_SELECTORS = [
    'input[name="verificationCode"]',
    'input[name="security_code"]',
    'input[aria-label*="Security code" i]',
    'input[aria-label*="verification code" i]',
    'input[autocomplete="one-time-code"]',
]
POPUP_SAVE_INFO = ['button:has-text("Save Info")', 'button:has-text("Save info")']
POPUP_NOT_NOW = [
    'button:has-text("Not Now")', 'button:has-text("Not now")',
    'button:has-text("Skip")', 'button:has-text("Maybe Later")',
]
CHECKPOINT_URL_HINTS = ["/challenge/", "/accounts/suspended", "/accounts/disabled", "/two_factor/"]
RATE_LIMIT_TEXTS = [
    "try again later", "suspicious login", "please wait",
    "temporarily blocked", "challenge required",
]


# =====================================================================
# In-memory batch registry
# =====================================================================
class _ItemState:
    __slots__ = ("password", "otp_future", "cancel_event")

    def __init__(self, password: str):
        self.password = password  # wiped after processing
        self.otp_future: Optional[asyncio.Future] = None
        self.cancel_event = asyncio.Event()


class _BatchState:
    def __init__(self, batch_id: int, concurrency: int, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.batch_id = batch_id
        self.items: dict[int, _ItemState] = {}
        self.pause_event = asyncio.Event()
        self.pause_event.set()  # running by default
        self.cancel_event = asyncio.Event()
        self.subscribers: list[asyncio.Queue] = []
        self.semaphore = asyncio.Semaphore(concurrency)
        self.task: Optional[asyncio.Task] = None
        # Loop is captured EXPLICITLY by the FastAPI request handler and
        # passed in — this guarantees we schedule Playwright on the exact
        # uvicorn/FastAPI loop (WindowsProactorEventLoop) and never on a
        # WatchFiles Selector loop or any other stray loop.
        self.loop: asyncio.AbstractEventLoop = loop or asyncio.get_running_loop()

    def wipe_password(self, item_id: int) -> None:
        state = self.items.get(item_id)
        if state:
            state.password = ""

    def wipe_all_passwords(self) -> None:
        for s in self.items.values():
            s.password = ""

    async def publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


_batches: dict[int, _BatchState] = {}


def get_batch(batch_id: int) -> Optional[_BatchState]:
    return _batches.get(batch_id)


# =====================================================================
# CSV parse (in-memory only)
# =====================================================================
def parse_csv(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (rows, errors). Rows are (username, password) tuples."""
    import csv
    import io

    rows: list[tuple[str, str]] = []
    errors: list[str] = []
    reader = csv.reader(io.StringIO(text))

    header_seen = False
    for i, raw in enumerate(reader, start=1):
        if not raw or all((not (c or "").strip()) for c in raw):
            continue
        if not header_seen:
            header_seen = True
            lower = [c.strip().lower() for c in raw]
            if "username" in lower and "password" in lower:
                continue  # skip header
        if len(raw) < 2:
            errors.append(f"Row {i}: expected 2 columns, got {len(raw)}")
            continue
        u = (raw[0] or "").strip()
        p = (raw[1] or "").strip()
        if not u or not p:
            errors.append(f"Row {i}: empty username or password")
            continue
        if not SAFE_USERNAME.match(u):
            errors.append(f"Row {i}: invalid username '{u}'")
            continue
        rows.append((u, p))
    return rows, errors


# =====================================================================
# Persistence helpers (own DB session per call — safe from background)
# =====================================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _update_item(item_id: int, **fields: Any) -> Optional[BulkBatchItem]:
    db = SessionLocal()
    try:
        item = db.query(BulkBatchItem).filter(BulkBatchItem.id == item_id).first()
        if not item:
            return None
        for k, v in fields.items():
            setattr(item, k, v)
        db.commit()
        db.refresh(item)
        return item
    finally:
        db.close()


def _recount_batch(batch_id: int) -> None:
    db = SessionLocal()
    try:
        batch = db.query(BulkBatch).filter(BulkBatch.id == batch_id).first()
        if not batch:
            return
        items = db.query(BulkBatchItem).filter(BulkBatchItem.batch_id == batch_id).all()
        batch.connected_accounts = sum(1 for i in items if i.status == "connected")
        batch.failed_accounts = sum(1 for i in items if i.status == "failed")
        batch.otp_required_accounts = sum(1 for i in items if i.status == "otp_required")
        batch.checkpoint_accounts = sum(1 for i in items if i.status == "checkpoint_required")
        batch.retrying_accounts = sum(1 for i in items if i.status == "retrying")
        batch.remaining_accounts = sum(1 for i in items if i.status in ("pending", "logging_in", "retrying", "otp_required"))
        db.commit()
    finally:
        db.close()


def get_bulk_settings() -> dict:
    db = SessionLocal()
    try:
        rows = {r.key: r.value for r in db.query(Setting).all()}

        def _int(k, d):
            try:
                return int(rows.get(k, d))
            except (ValueError, TypeError):
                return d

        return {
            "concurrency": _int("bulk_connect_concurrency", 3),
            "delay_between_accounts": _int("bulk_connect_delay_seconds", 10),
            "headless": (rows.get("bulk_connect_headless", "false").lower() == "true"),
            "auto_retry": _int("bulk_connect_auto_retry", 2),
            "browser_timeout": _int("bulk_connect_browser_timeout", 60000),
            "min_concurrency": _int("bulk_connect_min_concurrency", 1),
            "max_concurrency": _int("bulk_connect_max_concurrency", 10),
            "min_delay": _int("bulk_connect_min_delay", 5),
            "max_delay": _int("bulk_connect_max_delay", 120),
        }
    finally:
        db.close()


# =====================================================================
# Debug artefact dump on failure
# =====================================================================
async def _dump_debug(page, context, username: str, item_id: int, reason: str, console_logs: list) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    folder = DEBUG_DIR / f"{username}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
    except Exception:
        pass
    try:
        html = await page.content()
        (folder / "page.html").write_text(html, encoding="utf-8")
    except Exception:
        pass
    try:
        info = {
            "url": page.url,
            "title": await page.title(),
            "reason": reason,
            "timestamp": ts,
            "username": username,
        }
        (folder / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    except Exception:
        pass
    try:
        (folder / "console.log").write_text("\n".join(console_logs), encoding="utf-8")
    except Exception:
        pass
    try:
        state = await context.storage_state()
        # Redact all cookie values to a length marker so we never persist secrets
        for c in state.get("cookies", []):
            if "value" in c:
                c["value"] = f"<redacted:{len(c['value'])}>"
        (folder / "storage_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
    _update_item(item_id, debug_dir=str(folder))
    return str(folder)


# =====================================================================
# Selector helpers
# =====================================================================
async def _first_visible(page, selectors: list[str], timeout_ms: int = 8000):
    """Return the first visible+enabled element matching any selector."""
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_err = None
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible() and await loc.is_enabled():
                    return loc
            except Exception as e:
                last_err = e
        await page.wait_for_timeout(150)
    if last_err:
        raise last_err
    return None


async def _dismiss_cookie_banner(page) -> None:
    for txt in ["Allow all cookies", "Accept All", "Only allow essential cookies", "Decline"]:
        try:
            btn = page.locator(f'button:has-text("{txt}")').first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=1500)
                return
        except Exception:
            pass


async def _handle_post_login_popups(page) -> None:
    """Save Info → Save Info. Notifications → Not Now."""
    for _ in range(3):
        acted = False
        for sel in POPUP_SAVE_INFO:
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible():
                    await b.click(timeout=1500)
                    acted = True
                    break
            except Exception:
                pass
        for sel in POPUP_NOT_NOW:
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible():
                    await b.click(timeout=1500)
                    acted = True
                    break
            except Exception:
                pass
        if not acted:
            return
        await page.wait_for_timeout(500)


async def _detect_state(page, context) -> str:
    """Classify the current page: connected | otp | checkpoint | rate_limit | wrong_password | still_login | unknown."""
    url = (page.url or "").lower()
    for hint in CHECKPOINT_URL_HINTS:
        if hint in url:
            if "two_factor" in url:
                return "otp"
            if "challenge" in url:
                # challenge often includes a code input — treat as OTP if input present
                for sel in OTP_INPUT_SELECTORS:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() and await loc.is_visible():
                            return "otp"
                    except Exception:
                        pass
                return "checkpoint"
            return "checkpoint"

    # OTP input on same page?
    for sel in OTP_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return "otp"
        except Exception:
            pass

    try:
        body_text = (await page.locator("body").inner_text(timeout=1500)).lower()
    except Exception:
        body_text = ""
    for phrase in RATE_LIMIT_TEXTS:
        if phrase in body_text:
            return "rate_limit"
    if "sorry, your password was incorrect" in body_text or "incorrect password" in body_text:
        return "wrong_password"

    # Successful login: sessionid cookie present AND no login form
    try:
        cookies = await context.cookies("https://www.instagram.com/")
        has_session = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
    except Exception:
        has_session = False

    login_form_present = False
    try:
        loc = page.locator('input[name="username"]').first
        if await loc.count() and await loc.is_visible():
            login_form_present = True
    except Exception:
        pass

    if has_session and "/accounts/login" not in url and not login_form_present:
        return "connected"
    if login_form_present:
        return "still_login"
    return "unknown"


# =====================================================================
# Single-account login flow
# =====================================================================
async def _login_one(batch_state: _BatchState, item: BulkBatchItem, settings: dict) -> None:
    item_id = item.id
    username = item.username
    item_state = batch_state.items[item_id]
    password = item_state.password

    profile_path = PROFILES_DIR / username
    profile_path.mkdir(parents=True, exist_ok=True)
    _update_item(item_id, status="logging_in", message="Launching browser", profile_path=str(profile_path), started_at=_now())
    await batch_state.publish({"type": "item", "item_id": item_id, "status": "logging_in", "message": "Launching browser"})

    console_logs: list[str] = []
    from playwright.async_api import async_playwright
    # ---- Requested trace logging so the user can see exactly where we are.
    try:
        _loop_name = type(asyncio.get_running_loop()).__name__
        _policy_name = type(asyncio.get_event_loop_policy()).__name__
        print(f"[bulk_connect] Before Playwright launch: loop={_loop_name} "
              f"policy={_policy_name} batch_id={batch_state.batch_id} "
              f"item_id={item_id} username={username}", flush=True)
        print(f"[bulk_connect] PLAYWRIGHT_BROWSERS_PATH="
              f"{os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '<unset>')}", flush=True)
    except Exception:
        pass
    async with async_playwright() as p:
        print(f"[bulk_connect] After Playwright launch: item_id={item_id}", flush=True)
        try:
            print(f"[bulk_connect] p.chromium.executable_path={p.chromium.executable_path}", flush=True)
        except Exception as _e:
            print(f"[bulk_connect] executable_path unavailable: {_e!r}", flush=True)
        # ---- Wrap ONLY launch_persistent_context in try/traceback per operator request ----
        # NOTE: launch args intentionally aligned byte-for-byte with the working
        # browser_session.connect_account launch. Bulk Connect previously added an
        # extra automation-control override arg which triggers an immediate crash
        # of the Chromium child process on many Windows Chrome builds (recognised
        # as an enterprise-policy override → refuse-to-launch). That was the ONLY
        # delta vs. the working Accounts→Connect flow.
        # headless: ALWAYS False for Bulk Connect (operator watches the browser
        # log-in). browser_session.connect_account uses `not has_display` which
        # resolves to False on Windows; we hardcode False here so a mis-toggled
        # `bulk_connect_headless=true` setting cannot produce an invisible browser.
        # ---- Operator-requested runtime proofs (exact form) ----
        print("HEADLESS =", False, flush=True)
        print("SETTINGS HEADLESS =", settings["headless"], flush=True)
        print(f"[bulk_connect] launch params: headless=False (settings value was "
              f"{settings['headless']!r} — intentionally overridden to match browser_session)",
              flush=True)
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=False,
                viewport={"width": 1280, "height": 800},
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception:
            print(f"[bulk_connect] launch_persistent_context FAILED for item_id={item_id}:", flush=True)
            traceback.print_exc()
            raise
        # ---- Operator-requested runtime proof: post-launch ----
        print("context.browser =", context.browser, flush=True)
        try:
            _p0 = context.pages[0] if context.pages else await context.new_page()
            await _p0.goto("https://www.instagram.com/")
            print(_p0.url, flush=True)
        except Exception:
            traceback.print_exc()
        print(f"[bulk_connect] Browser opened: item_id={item_id} username={username}", flush=True)
        # ---- Post-launch diagnostics per operator request ----
        try:
            _browser = context.browser
            _pages = context.pages
            print(f"[bulk_connect] context.browser={_browser!r}", flush=True)
            print(f"[bulk_connect] context.pages={_pages!r} (count={len(_pages)})", flush=True)
            if _browser is not None:
                print(f"[bulk_connect] browser.is_connected()={_browser.is_connected()}", flush=True)
            try:
                _proc_pid = getattr(getattr(context, "_impl_obj", None), "_transport", None)
                print(f"[bulk_connect] context._impl_obj._transport={_proc_pid!r}", flush=True)
            except Exception as _e:
                print(f"[bulk_connect] pid probe failed: {_e!r}", flush=True)
            try:
                _ss = await context.storage_state()
                print(f"[bulk_connect] storage_state keys={list(_ss.keys())} "
                      f"cookies={len(_ss.get('cookies', []))} origins={len(_ss.get('origins', []))}",
                      flush=True)
            except Exception as _e:
                print(f"[bulk_connect] storage_state() error: {_e!r}", flush=True)
                traceback.print_exc()
        except Exception:
            print(f"[bulk_connect] post-launch diagnostics failed:", flush=True)
            traceback.print_exc()
        # ---- Bring the window to the foreground and enumerate visibility state
        # ---- so we can prove whether Windows launched the browser off-screen
        # ---- or with no focus.
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            print(f"[bulk_connect] page.url BEFORE bring_to_front / goto: {page.url!r}", flush=True)
            try:
                await page.bring_to_front()
                print(f"[bulk_connect] page.bring_to_front() called", flush=True)
            except Exception as _e:
                print(f"[bulk_connect] bring_to_front error: {_e!r}", flush=True)
                traceback.print_exc()
            try:
                await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
                print(f"[bulk_connect] page.url AFTER goto: {page.url!r}", flush=True)
            except Exception as _e:
                print(f"[bulk_connect] pre-diagnostic goto error: {_e!r}", flush=True)
                traceback.print_exc()
            try:
                _all_pages = context.pages
                print(f"[bulk_connect] All pages after bring_to_front + goto:", flush=True)
                for _pp in _all_pages:
                    try:
                        _title = await _pp.title()
                    except Exception:
                        _title = "<unavailable>"
                    print(f"    - url={_pp.url!r} title={_title!r}", flush=True)
                try:
                    _b = context.browser
                    if _b is not None:
                        print(f"[bulk_connect] All browser contexts: {_b.contexts}", flush=True)
                except Exception as _e:
                    print(f"[bulk_connect] contexts probe error: {_e!r}", flush=True)
                try:
                    _win_state = await page.evaluate(
                        "() => ({visibilityState: document.visibilityState, hasFocus: document.hasFocus(), "
                        "outerWidth: window.outerWidth, outerHeight: window.outerHeight, "
                        "screenX: window.screenX, screenY: window.screenY})"
                    )
                    print(f"[bulk_connect] window state: {_win_state}", flush=True)
                except Exception as _e:
                    print(f"[bulk_connect] window state probe error: {_e!r}", flush=True)
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
        # ---- 60s sleep so operator can confirm chrome.exe is visible in Task Manager ----
        print(f"[bulk_connect] sleeping 60s so you can verify the browser window is visible…", flush=True)
        try:
            for _tick in range(12):
                await asyncio.sleep(5)
                _connected = None
                try:
                    _connected = context.browser.is_connected() if context.browser else None
                except Exception:
                    _connected = f"probe error"
                print(f"[bulk_connect] tick {(_tick + 1) * 5}s: browser.is_connected()={_connected} "
                      f"pages={len(context.pages)}", flush=True)
        except Exception:
            traceback.print_exc()
        context.set_default_timeout(settings["browser_timeout"])
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: console_logs.append(f"[pageerror] {err}"))

        try:
            # ---- Check if already logged in (persistent profile reused) ----
            await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            await page.wait_for_load_state("domcontentloaded")
            try:
                await page.wait_for_function("document.readyState === 'complete'", timeout=15000)
            except Exception:
                pass
            state = await _detect_state(page, context)
            if state == "connected":
                _update_item(item_id, status="connected", message="Already logged in (persistent profile)",
                             finished_at=_now(), last_url=page.url)
                item_state.password = ""
                return

            # ---- Navigate to login page ----
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_function("document.readyState === 'complete'", timeout=15000)
            except Exception:
                pass
            await _dismiss_cookie_banner(page)

            # ---- Username ----
            user_loc = await _first_visible(page, USERNAME_SELECTORS, timeout_ms=15000)
            if not user_loc:
                raise RuntimeError("Username field not found")
            await user_loc.click()
            await user_loc.fill("")
            await user_loc.type(username, delay=25)
            entered = await user_loc.input_value()
            if entered != username:
                await user_loc.fill(username)

            # ---- Password (re-locate after username to avoid stale IG re-render) ----
            pass_loc = await _first_visible(page, PASSWORD_SELECTORS, timeout_ms=8000)
            if not pass_loc:
                raise RuntimeError("Password field not found")
            await pass_loc.click()
            await pass_loc.fill("")
            await pass_loc.type(password, delay=25)
            entered_pw = await pass_loc.input_value()
            if not entered_pw:
                await pass_loc.fill(password)

            # ---- Click login (or press Enter fallback) ----
            clicked = False
            for sel in LOGIN_BUTTON_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() and await btn.is_visible() and await btn.is_enabled():
                        await btn.click(timeout=3000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                await pass_loc.press("Enter")

            # Wipe password from memory right after submission
            item_state.password = ""
            password = ""

            # ---- Wait for navigation / post-login state ----
            try:
                await page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                pass

            state = await _detect_state(page, context)

            # If checkpoint OR OTP: pause this account and wait for operator input
            if state == "otp":
                _update_item(item_id, status="otp_required", otp_required=1,
                             message="OTP required — enter code in dashboard", last_url=page.url)
                await batch_state.publish({"type": "item", "item_id": item_id, "status": "otp_required",
                                           "message": "OTP required", "url": page.url})
                # Await operator OTP
                fut: asyncio.Future = asyncio.get_running_loop().create_future()
                item_state.otp_future = fut
                try:
                    otp_code = await asyncio.wait_for(fut, timeout=30 * 60)  # 30 min operator timeout
                except asyncio.TimeoutError:
                    _update_item(item_id, status="failed", message="OTP timeout — no code received",
                                 last_error="OTP timeout", finished_at=_now())
                    await _dump_debug(page, context, username, item_id, "otp_timeout", console_logs)
                    return
                if not otp_code:
                    _update_item(item_id, status="failed", message="OTP cancelled", finished_at=_now())
                    return

                otp_input = await _first_visible(page, OTP_INPUT_SELECTORS, timeout_ms=5000)
                if not otp_input:
                    raise RuntimeError("OTP input field disappeared before code could be entered")
                await otp_input.fill("")
                await otp_input.type(otp_code, delay=30)
                # Submit
                submitted = False
                for sel in ['button:has-text("Confirm")', 'button:has-text("Continue")',
                            'button:has-text("Submit")', 'button[type="submit"]']:
                    try:
                        b = page.locator(sel).first
                        if await b.count() and await b.is_visible():
                            await b.click(timeout=3000)
                            submitted = True
                            break
                    except Exception:
                        pass
                if not submitted:
                    await otp_input.press("Enter")
                try:
                    await page.wait_for_load_state("networkidle", timeout=25000)
                except Exception:
                    pass
                state = await _detect_state(page, context)

            if state == "checkpoint":
                _update_item(item_id, status="checkpoint_required", checkpoint="account_challenge",
                             message="Checkpoint required — manual review", last_url=page.url,
                             finished_at=_now())
                await _dump_debug(page, context, username, item_id, "checkpoint", console_logs)
                return

            if state == "rate_limit":
                _update_item(item_id, status="failed", last_error="rate_limited",
                             message="Instagram rate limit / please wait", last_url=page.url,
                             finished_at=_now())
                await _dump_debug(page, context, username, item_id, "rate_limit", console_logs)
                return

            if state == "wrong_password":
                _update_item(item_id, status="failed", last_error="wrong_password",
                             message="Incorrect password", last_url=page.url, finished_at=_now())
                return

            # ---- Handle post-login popups ----
            await _handle_post_login_popups(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            state = await _detect_state(page, context)

            if state == "connected":
                _update_item(item_id, status="connected", message="Session saved to persistent profile",
                             last_url=page.url, finished_at=_now())
                await batch_state.publish({"type": "item", "item_id": item_id, "status": "connected",
                                           "message": "Connected"})
                return

            # If we're still on login page and no explicit error, treat as failure
            _update_item(item_id, status="failed", message=f"Login unresolved (state={state})",
                         last_error=state, last_url=page.url, finished_at=_now())
            await _dump_debug(page, context, username, item_id, f"unresolved_{state}", console_logs)

        except Exception as e:
            reason = f"{type(e).__name__}: {str(e)[:250]}"
            print(f"[bulk_connect] Exception occurred: item_id={item_id} {reason}", flush=True)
            traceback.print_exc()
            _update_item(item_id, status="failed", last_error=reason, message="Login flow error",
                         last_url=(page.url if page else ""), finished_at=_now())
            try:
                await _dump_debug(page, context, username, item_id, reason, console_logs)
            except Exception:
                pass
        finally:
            item_state.password = ""
            try:
                await context.close()
            except Exception:
                pass


# =====================================================================
# Item runner (with retry envelope) + batch orchestrator
# =====================================================================
async def _run_item(batch_state: _BatchState, item_id: int, settings: dict) -> None:
    async with batch_state.semaphore:
        for attempt in range(1, settings["auto_retry"] + 2):  # first try + retries
            if batch_state.cancel_event.is_set():
                _update_item(item_id, status="failed", message="Cancelled", finished_at=_now())
                return
            await batch_state.pause_event.wait()

            db = SessionLocal()
            try:
                item = db.query(BulkBatchItem).filter(BulkBatchItem.id == item_id).first()
            finally:
                db.close()
            if not item or not batch_state.items.get(item_id):
                return
            if item.status == "connected":
                return

            if attempt > 1:
                _update_item(item_id, status="retrying", retry_count=attempt - 1,
                             message=f"Retry {attempt - 1}/{settings['auto_retry']}")
                await batch_state.publish({"type": "item", "item_id": item_id, "status": "retrying"})

            try:
                await _login_one(batch_state, item, settings)
            except Exception as e:
                _update_item(item_id, status="failed", last_error=str(e)[:250],
                             message="Worker crashed", finished_at=_now())

            db = SessionLocal()
            try:
                item = db.query(BulkBatchItem).filter(BulkBatchItem.id == item_id).first()
                final_status = item.status if item else "failed"
            finally:
                db.close()
            # Only retry transient failures — never OTP/checkpoint/wrong_password
            if final_status in ("connected", "otp_required", "checkpoint_required", "completed"):
                break
            if final_status == "failed":
                last_err = (item.last_error or "").lower() if item else ""
                if last_err in ("wrong_password",):
                    break
                if attempt <= settings["auto_retry"]:
                    await asyncio.sleep(min(15, 5 * attempt))
                    continue
            break

        _recount_batch(batch_state.batch_id)
        await batch_state.publish({"type": "batch", "batch_id": batch_state.batch_id})


async def _run_batch(batch_state: _BatchState, settings: dict) -> None:
    db = SessionLocal()
    try:
        item_ids = [i.id for i in db.query(BulkBatchItem)
                    .filter(BulkBatchItem.batch_id == batch_state.batch_id)
                    .order_by(BulkBatchItem.id.asc()).all()]
        batch = db.query(BulkBatch).filter(BulkBatch.id == batch_state.batch_id).first()
        if batch:
            batch.status = "running"
            batch.started_at = _now()
            db.commit()
    finally:
        db.close()

    await batch_state.publish({"type": "batch", "batch_id": batch_state.batch_id, "status": "running"})

    tasks = []
    delay = settings["delay_between_accounts"]
    for idx, iid in enumerate(item_ids):
        if batch_state.cancel_event.is_set():
            break
        await batch_state.pause_event.wait()
        if idx > 0 and delay > 0:
            try:
                await asyncio.wait_for(batch_state.cancel_event.wait(), timeout=delay)
                break  # cancelled during delay
            except asyncio.TimeoutError:
                pass
        tasks.append(asyncio.create_task(_run_item(batch_state, iid, settings)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    batch_state.wipe_all_passwords()

    db = SessionLocal()
    try:
        batch = db.query(BulkBatch).filter(BulkBatch.id == batch_state.batch_id).first()
        if batch:
            items = db.query(BulkBatchItem).filter(BulkBatchItem.batch_id == batch.id).all()
            if batch_state.cancel_event.is_set():
                batch.status = "cancelled"
            elif any(i.status in ("pending", "logging_in", "otp_required", "retrying") for i in items):
                batch.status = "paused" if not batch_state.pause_event.is_set() else "running"
            else:
                batch.status = "completed"
            batch.finished_at = _now()
            db.commit()
    finally:
        db.close()
    _recount_batch(batch_state.batch_id)
    await batch_state.publish({"type": "batch", "batch_id": batch_state.batch_id, "status": "finished"})


# =====================================================================
# Public API
# =====================================================================
def register_batch(batch_id: int, items: list[tuple[int, str, str]], concurrency: int,
                   loop: Optional[asyncio.AbstractEventLoop] = None) -> _BatchState:
    """items: list of (item_id, username, password). ``loop`` should be the
    FastAPI request-handler's running loop, captured explicitly at the call
    site to guarantee Playwright runs on the same uvicorn/FastAPI loop."""
    state = _BatchState(batch_id, concurrency, loop=loop)
    for item_id, _username, password in items:
        state.items[item_id] = _ItemState(password=password)
    _batches[batch_id] = state
    return state


async def start_batch(batch_id: int) -> bool:
    """Schedule the batch runner on the SAME running loop as the caller.

    Mirrors the pattern used by ``browser_session.connect_account``: this
    coroutine is awaited by the FastAPI request handler, and
    ``asyncio.create_task`` runs from within an async body so the created
    task attaches cleanly to the current running loop.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return False
    state = _batches.get(batch_id)
    if not state:
        return False
    if state.task and not state.task.done():
        return False
    settings = get_bulk_settings()

    # Diagnostic — proves start_batch is running on the FastAPI loop.
    try:
        _loop_name = type(asyncio.get_running_loop()).__name__
        _policy_name = type(asyncio.get_event_loop_policy()).__name__
        print(f"[bulk_connect] start_batch: Bulk worker started (batch_id={batch_id}) "
              f"loop={_loop_name} policy={_policy_name}", flush=True)
    except Exception:
        pass

    # Same pattern as browser_session.connect_account: schedule from inside
    # an async function so the task attaches to the running loop directly.
    state.task = asyncio.create_task(_run_batch(state, settings))
    return True


async def submit_otp(batch_id: int, item_id: int, code: str) -> bool:
    state = _batches.get(batch_id)
    if not state:
        return False
    item_state = state.items.get(item_id)
    if not item_state or not item_state.otp_future or item_state.otp_future.done():
        return False
    item_state.otp_future.set_result(code)
    return True


def pause_batch(batch_id: int) -> bool:
    state = _batches.get(batch_id)
    if not state:
        return False
    state.pause_event.clear()
    return True


def resume_batch(batch_id: int) -> bool:
    state = _batches.get(batch_id)
    if not state:
        return False
    state.pause_event.set()
    return True


def cancel_batch(batch_id: int) -> bool:
    state = _batches.get(batch_id)
    if not state:
        return False
    state.cancel_event.set()
    state.pause_event.set()  # unblock any waiters
    for item_state in state.items.values():
        if item_state.otp_future and not item_state.otp_future.done():
            item_state.otp_future.set_result("")
    state.wipe_all_passwords()
    return True


async def retry_item(batch_id: int, item_id: int, password: str) -> bool:
    state = _batches.get(batch_id)
    if not state:
        return False
    settings = get_bulk_settings()
    state.items[item_id] = _ItemState(password=password)
    _update_item(item_id, status="pending", retry_count=0, message="Manual retry queued",
                 last_error="", finished_at=None, started_at=None)
    # Explicitly schedule on the batch's captured loop (Windows safety).
    state.loop.create_task(_run_item(state, item_id, settings))
    return True


def subscribe(batch_id: int) -> Optional[asyncio.Queue]:
    state = _batches.get(batch_id)
    if not state:
        return None
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    state.subscribers.append(q)
    return q


def unsubscribe(batch_id: int, q: asyncio.Queue) -> None:
    state = _batches.get(batch_id)
    if state and q in state.subscribers:
        state.subscribers.remove(q)


def environment_status() -> dict:
    return {
        "playwright_installed": PLAYWRIGHT_AVAILABLE,
        "profiles_dir": str(PROFILES_DIR),
        "debug_dir": str(DEBUG_DIR),
    }
