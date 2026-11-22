"""Persistent-context lifecycle helper for publishing workers.

This is the ONE standard way publishing workers obtain a Playwright browser
context. It intentionally mirrors the persistent-profile convention used by
``browser_session`` (onboarding) so there is a single, shared contract:

    * One persistent profile directory per Instagram account.
    * The profile directory on disk is the source of truth for the session.

The difference in lifecycle is deliberate and documented:

    * ``browser_session``  -> short-lived, INTERACTIVE (manual login / verify).
    * ``browser_pool``     -> long-lived, WORKER-OWNED. A worker opens ONE
                             context for an account and reuses it across many
                             publish jobs, so Chromium startup cost is paid
                             once per account-cycle, never once per upload.

Nothing here launches unnecessary browsers: a context is opened only when a
worker has claimed an account that has queued jobs, and closed as soon as that
account's jobs are drained.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False


def _has_display() -> bool:
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY"))


@asynccontextmanager
async def open_account_context(
    profile_path: str,
    headless: bool = True,
    timeout_ms: int = 60000,
) -> AsyncIterator:
    """Open a persistent Chromium context for one account and yield it.

    The context is guaranteed to be closed on exit even if a job raises. The
    caller (a worker) keeps this context open for the full lifetime of an
    account's job batch and issues multiple publishes against it.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright is not installed on this server.")

    p = Path(profile_path)
    p.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(p),
            headless=headless or not _has_display(),
            viewport={"width": 1280, "height": 900},
            args=["--no-first-run", "--no-default-browser-check"],
        )
        context.set_default_timeout(timeout_ms)
        try:
            yield context
        finally:
            try:
                await context.close()
            except Exception:
                pass
