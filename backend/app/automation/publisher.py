"""Instagram publishing steps (Playwright) â€” the single publishing implementation.

Given a logged-in persistent context, ``publish_job`` performs the real
Instagram web "Create" flow for a Reel or a Static post. It NEVER logs or
returns cookies/passwords.

Design notes
------------
* The worker owns the browser context and reuses it across jobs; this module
  only drives a page â€” it never launches or closes browsers.
* Every failure raises :class:`PublishError` with a short machine code so the
  worker can decide retry vs terminal. Transient errors are retryable;
  ``login_required`` / ``identity_mismatch`` are terminal.
* Selectors are resilient (multiple fallbacks) because Instagram's DOM changes
  frequently. This code is written to run on the target Windows host against
  real, logged-in persistent profiles.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

try:  # screenshots/diagnostics land next to the rest of the app logs
    from ..database import BASE_DIR
    SCREENSHOTS_DIR = BASE_DIR / "logs" / "screenshots"
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # pragma: no cover
    SCREENSHOTS_DIR = Path("logs/screenshots")

logger = logging.getLogger("ig_automation_os.publisher")

IG_HOME = "https://www.instagram.com/"

# ``publish_unconfirmed`` is terminal on purpose: Share was clicked but IG never
# confirmed â€” retrying could create a duplicate post (mirrors the old engine's
# "ACTION REQUIRED" no-retry behaviour).
TERMINAL_CODES = {"login_required", "identity_mismatch", "file_missing", "unsupported", "publish_unconfirmed"}


def _now():
    return datetime.now(timezone.utc)


def _tag_path(tag: str, marker: str) -> Path:
    ts = _now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", tag)[:40] or "publish"
    return SCREENSHOTS_DIR / f"{safe}_{marker}_{ts}.png"


async def _safe_screenshot(page, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass


class PublishError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)

    @property
    def retryable(self) -> bool:
        return self.code not in TERMINAL_CODES


async def _first(page, selectors, timeout_ms: int = 8000):
    """Return the first selector locator that becomes visible, else None."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


async def _dismiss_popups(page) -> None:
    for sel in [
        'button:has-text("Not Now")', 'button:has-text("Not now")',
        'button:has-text("Allow all cookies")', 'button:has-text("Decline optional cookies")',
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.click()
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def ensure_logged_in(context) -> None:
    """Fast auth check. Raises PublishError('login_required') if not authed.

    The authoritative signal is a non-empty ``sessionid`` cookie on the
    instagram.com domain â€” same contract as onboarding verification.
    """
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto(IG_HOME, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    await asyncio.sleep(1)
    await _dismiss_popups(page)
    try:
        cookies = await context.cookies(IG_HOME)
    except Exception:
        cookies = []
    has_session = any(c.get("name") == "sessionid" and c.get("value") for c in cookies)
    current = (page.url or "").split("?")[0]
    if not has_session or "/accounts/login" in current:
        raise PublishError("login_required", "Account session is not logged in.")


# ---------------------------------------------------------------------------
# Gold-standard automation helpers (restored from the proven publisher).
# Try each candidate selector until one matches, then interact.
# ---------------------------------------------------------------------------
async def _first_visible(page, selectors, timeout_ms: int = 8000):
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
    last_err = None
    while asyncio.get_event_loop().time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    return loc
            except Exception as e:
                last_err = e
        await asyncio.sleep(0.25)
    raise TimeoutError(f"None of {selectors!r} became visible in {timeout_ms}ms: {last_err}")


async def _click_first(page, selectors, *, timeout_ms: int = 8000):
    loc = await _first_visible(page, selectors, timeout_ms=timeout_ms)
    await loc.click()
    return loc


# ---------------------------------------------------------------------------
# Step 1 + 2: open Create and reach the uploader (universal detector).
# ---------------------------------------------------------------------------
async def _open_create_dialog(page, tag: str = "") -> None:
    """Open Instagram Create reliably and reach the media uploader.

    The persistent browser context is reused between jobs, so the page can be
    left in an arbitrary Instagram state by the previous job. Before looking
    for Create, make sure the page is alive and on Instagram, dismiss common
    overlays, and give the UI a few recovery attempts.
    """
    logger.info("[%s] opening Create", tag)

    create_selectors = [
        'a:has(svg[aria-label="New post"])',
        'div[role="button"]:has(svg[aria-label="New post"])',
        '[aria-label="New post"]',
        'a[role="link"]:has-text("Create")',
        'div[role="button"]:has-text("Create")',
        'button:has-text("Create")',
    ]

    post_selectors = [
        '[aria-label="Post"]',
        'div[role="button"]:has-text("Post")',
        'a:has-text("Post")',
        'span:has-text("Post")',
    ]

    reel_selectors = [
        'a[href*="/reels/create"]',
        '[aria-label="Reel"]',
        'div[role="button"]:has-text("Reel")',
        'span:has-text("Reel")',
    ]

    select_from_computer_selectors = [
        'button:has-text("Select from computer")',
        'div[role="button"]:has-text("Select from computer")',
        'span:has-text("Select from computer")',
    ]

    async def uploader_reachable():
        try:
            return await page.locator('input[type="file"]').count() > 0
        except Exception:
            return False

    async def find_visible(selectors):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    async def click_visible(selectors, timeout_ms=3000):
        loc = await find_visible(selectors)
        if loc is None:
            return False
        try:
            await loc.click(timeout=timeout_ms)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------
    # Recover page state before touching Create.
    # ---------------------------------------------------------------
    try:
        if page.is_closed():
            raise PublishError(
                "ui_page_closed",
                "Instagram page was closed before publishing.",
            )
    except PublishError:
        raise
    except Exception:
        pass

    try:
        current_url = (page.url or "").split("?")[0].rstrip("/")
    except Exception:
        current_url = ""

    # Always bring the reused page back to Instagram home. This is important
    # when the previous job left the page inside an editor/dialog.
    if "instagram.com" not in current_url or current_url in (
        "",
        "https://www.instagram.com",
    ):
        try:
            await page.goto(
                IG_HOME,
                wait_until="domcontentloaded",
                timeout=45000,
            )
        except Exception as e:
            logger.warning("[%s] Instagram navigation warning: %s", tag, e)

    try:
        await page.bring_to_front()
    except Exception:
        pass

    await asyncio.sleep(2)
    await _dismiss_popups(page)

    # ---------------------------------------------------------------
    # Try Create several times. A reused persistent page can need a moment
    # for the left navigation/sidebar to hydrate.
    # ---------------------------------------------------------------
    create = None

    for attempt in range(1, 4):
        logger.info("[%s] looking for Create, attempt %d/3", tag, attempt)

        create = await find_visible(create_selectors)

        if create is not None:
            try:
                await create.click(timeout=10000)
                logger.info("[%s] Create clicked on attempt %d", tag, attempt)
                break
            except Exception as e:
                logger.warning(
                    "[%s] Create click failed on attempt %d: %s",
                    tag,
                    attempt,
                    e,
                )
                create = None

        if attempt < 3:
            # Give Instagram's client-side navigation time to hydrate.
            await asyncio.sleep(1.5)

            # One controlled reload recovery.
            if attempt == 2:
                try:
                    await page.reload(
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    await asyncio.sleep(2)
                    await _dismiss_popups(page)
                except Exception as e:
                    logger.warning("[%s] Create recovery reload failed: %s", tag, e)

    if create is None:
        visible_text = ""
        try:
            btns = await page.locator(
                'button, [role="button"], a[role="link"]'
            ).all()
            texts = []
            for b in btns[:60]:
                try:
                    if await b.is_visible():
                        t = (await b.inner_text(timeout=800))[:80].strip()
                        if t and t not in texts:
                            texts.append(t)
                        if len(texts) >= 20:
                            break
                except Exception:
                    continue
            visible_text = " | ".join(texts) if texts else "(no visible buttons)"
        except Exception as e:
            visible_text = f"(diagnostic error: {type(e).__name__})"

        try:
            logger.error(
                "[%s] Create not found. url=%s visible=%s",
                tag,
                page.url,
                visible_text,
            )
        except Exception:
            pass

        await _safe_screenshot(page, _tag_path(tag, "create_missing"))

        raise PublishError(
            "ui_create_missing",
            "Visible Create/New post control not found after page recovery. "
            f"Visible buttons/text: {visible_text}",
        )

    # ---------------------------------------------------------------
    # Create dialog -> Post/Reel -> Select from computer -> file input.
    # ---------------------------------------------------------------
    await asyncio.sleep(1)

    for step in range(8):
        if await uploader_reachable():
            logger.info("[%s] uploader reached", tag)
            return

        if await click_visible(post_selectors):
            logger.info("[%s] advanced Create flow via Post", tag)
            await asyncio.sleep(1)
            continue

        if await click_visible(reel_selectors):
            logger.info("[%s] advanced Create flow via Reel", tag)
            await asyncio.sleep(1)
            continue

        if await click_visible(select_from_computer_selectors):
            logger.info("[%s] advanced Create flow via Select from computer", tag)
            await asyncio.sleep(1)
            continue

        # Sometimes the Create dialog animation/rendering takes longer.
        await asyncio.sleep(1)

    # Final uploader check.
    if await uploader_reachable():
        return

    visible_text = ""
    try:
        btns = await page.locator(
            'button, [role="button"], a[role="link"]'
        ).all()
        texts = []
        for b in btns[:60]:
            try:
                if await b.is_visible():
                    t = (await b.inner_text(timeout=800))[:80].strip()
                    if t and t not in texts:
                        texts.append(t)
                    if len(texts) >= 20:
                        break
            except Exception:
                continue
        visible_text = " | ".join(texts) if texts else "(no visible buttons)"
    except Exception as e:
        visible_text = f"(diagnostic error: {type(e).__name__})"

    await _safe_screenshot(page, _tag_path(tag, "no_uploader"))

    raise PublishError(
        "ui_upload_missing",
        "Could not reach Instagram uploader after Create. "
        f"Visible buttons/text: {visible_text}",
    )

async def _upload_file(page, media_path: str, tag: str = "") -> None:
    logger.info("[%s] uploading media (%s)", tag, Path(media_path).name)
    file_input = None
    for sel in ('input[type="file"][accept*="video"]',
                'input[type="file"][accept*="image"]',
                'input[type="file"]'):
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                file_input = loc
                break
        except Exception:
            continue
    if file_input is None:
        raise PublishError("ui_upload_missing", "Could not locate the Instagram file input")
    await file_input.set_input_files(media_path)

    # Wait for processing â€” "Next" becoming clickable is the reliable signal that
    # IG finished processing the upload (up to 2 min for large videos).
    logger.info("[%s] waiting for media processing", tag)
    next_selectors = [
        'div[role="button"]:has-text("Next")',
        'button:has-text("Next")',
    ]
    try:
        await _first_visible(page, next_selectors, timeout_ms=120000)
    except Exception:
        # Sometimes IG shows an "OK"/aspect-ratio dialog first â€” dismiss it, retry.
        for sel in ['div[role="button"]:has-text("OK")', 'div[role="button"]:has-text("Ok")']:
            try:
                await page.locator(sel).first.click(timeout=1500)
            except Exception:
                pass
        try:
            await _first_visible(page, next_selectors, timeout_ms=45000)
        except Exception as e:
            raise PublishError("ui_next_missing", f"Upload did not finish processing: {e}")


# ---------------------------------------------------------------------------
# Step 5: click Next until the caption screen appears (crop -> filter -> caption).
# ---------------------------------------------------------------------------
_CAPTION_SELECTORS = [
    'div[aria-label="Write a caption..."]',
    'div[aria-label*="caption" i][role="textbox"]',
    'textarea[aria-label*="caption" i]',
]
_NEXT_SELECTORS = [
    'div[role="button"]:has-text("Next")',
    'button:has-text("Next")',
]


async def _click_next(page, tag: str = "") -> None:
    logger.info("[%s] processing complete, moving through Next screens", tag)
    for _step in range(5):
        caption_found = False
        for sel in _CAPTION_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    caption_found = True
                    break
            except Exception:
                pass
        if caption_found:
            break
        try:
            await _click_first(page, _NEXT_SELECTORS, timeout_ms=10000)
            await asyncio.sleep(1.5)
        except Exception:
            break


# ---------------------------------------------------------------------------
# Step 6: enter the caption.
# ---------------------------------------------------------------------------
async def _set_caption(page, caption: str, tag: str = "") -> None:
    logger.info("[%s] entering caption", tag)
    try:
        caption_loc = await _first_visible(page, _CAPTION_SELECTORS, timeout_ms=45000)
    except Exception as e:
        raise PublishError("ui_caption_missing", f"Caption field not found: {e}")
    await caption_loc.click()
    if caption:
        # `type` fires IG's onChange more reliably than `fill` for contenteditable.
        await caption_loc.type(caption, delay=6)


# ---------------------------------------------------------------------------
# Step 7 + 8: click the exact visible+enabled Share and confirm publication.
# ---------------------------------------------------------------------------
async def _share(page, tag: str = "") -> None:
    logger.info("[%s] clicking Share", tag)
    # DEBUG: capture Instagram state immediately before Share.
    try:
        visible_buttons = await page.locator('button:visible, div[role="button"]:visible').all_inner_texts()
        logger.info("[%s] PRE-SHARE url=%s buttons=%s", tag, page.url, visible_buttons[:30])
        await _safe_screenshot(page, _tag_path(tag, "pre_share"))
    except Exception as debug_error:
        logger.warning("[%s] PRE-SHARE debug failed: %s", tag, debug_error)

    # Click the exact visible + enabled Share control (search from the last match).
    share_candidates = page.get_by_text("Share", exact=True)
    share = None
    for i in range(await share_candidates.count() - 1, -1, -1):
        candidate = share_candidates.nth(i)
        try:
            if await candidate.is_visible() and await candidate.is_enabled():
                share = candidate
                break
        except Exception:
            continue
    if share is None:
        raise PublishError("ui_share_missing", "No visible and enabled exact Share control found")
    await share.click(timeout=8000)

    # Wait for Instagram to confirm publication â€” explicit confirmation only.
    # A disappearing dialog alone is NOT proof the post was published.
    logger.info("[%s] waiting for Instagram confirmation", tag)
    confirmation_patterns = [
        re.compile(r"your\s+(post|reel)\s+has\s+been\s+shared", re.I),
        re.compile(r"shared\s+to\s+your\s+profile", re.I),
    ]
    deadline = asyncio.get_event_loop().time() + 180
    while asyncio.get_event_loop().time() < deadline:
        body_text = ""
        try:
            body_text = (await page.locator("body").inner_text(timeout=1500)) or ""
        except Exception:
            pass
        if any(p.search(body_text) for p in confirmation_patterns):
            logger.info("[%s] Instagram confirmed publication", tag)
            return
        await asyncio.sleep(2)

    await _safe_screenshot(page, _tag_path(tag, "unconfirmed"))
    raise PublishError(
        "publish_unconfirmed",
        "Share was clicked but Instagram publication could not be confirmed. "
        "Check the profile before retrying to avoid a duplicate post.",
    )


async def publish_job(
    context,
    *,
    content_type: str,
    file_path: str,
    caption: str = "",
    audio_mode: str = "original",
    music_search: str = "",
) -> None:
    """Publish a single piece of content using an already-logged-in context.

    Raises :class:`PublishError` on any failure. Returns None on success. The
    worker owns the browser context (reused across jobs) â€” this function never
    launches or closes browsers. Signature is fixed: the Worker Pool depends on
    it.
    """
    if content_type not in ("reel", "static"):
        raise PublishError("unsupported", f"Unsupported content_type: {content_type}")

    path = Path(file_path)
    if not path.exists():
        raise PublishError("file_missing", f"Media file not found: {file_path}")

    tag = path.stem[:24] or "publish"
    page = context.pages[0] if context.pages else await context.new_page()
    if "instagram.com" not in (page.url or ""):
        await page.goto(IG_HOME, wait_until="domcontentloaded", timeout=45000)

    try:
        await _open_create_dialog(page, tag)
        await _upload_file(page, str(path), tag)
        await _click_next(page, tag)
        await _set_caption(page, caption, tag)
        await _share(page, tag)
    except PublishError:
        await _safe_screenshot(page, _tag_path(tag, "failure"))
        raise
    except Exception as e:
        await _safe_screenshot(page, _tag_path(tag, "failure"))
        raise PublishError("publish_error", f"{type(e).__name__}: {str(e)[:300]}")

