"""Iteration-4 tests: bulk_connect.py resilient login-input finder fix.

Static + monkey-patched-mock verification. No real Playwright / no IG.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.app.automation import bulk_connect as bc


REPO_ROOT = Path("/app")
BULK_CONNECT_PATH = REPO_ROOT / "backend/app/automation/bulk_connect.py"
PROTECTED = [
    "backend/app/automation/publisher.py",
    "backend/app/automation/publisher_queue.py",
    "backend/app/automation/browser_session.py",
]


# ---------------------------------------------------------------------------
# Static / structural checks
# ---------------------------------------------------------------------------
class TestStaticInvariants:
    def test_protected_files_unchanged(self):
        out = subprocess.run(
            ["git", "diff", "--name-only"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        for p in PROTECTED:
            assert p not in out, f"Protected file was modified: {p}"
        assert "backend/app/automation/bulk_connect.py" in out

    def test_module_compiles(self):
        r = subprocess.run(
            ["python", "-m", "py_compile", str(BULK_CONNECT_PATH)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr

    def test_selector_order_exact(self):
        assert bc.LOGIN_USERNAME_SELECTORS == (
            'input[aria-label="Phone number, username or email"]',
            'input[autocomplete="username"]',
            'input[name="username"]',
            'input[type="text"]',
        )

    def test_login_input_not_found_exception(self):
        assert issubclass(bc.LoginInputNotFound, RuntimeError)

    def test_debug_dir_created_on_import(self):
        assert bc.BULK_DEBUG_DIR.exists()
        assert bc.BULK_DEBUG_DIR.name == "bulk_connect_debug"
        assert bc.BULK_DEBUG_DIR.parent.name == "logs"

    def test_source_uses_wait_until_load(self):
        src = BULK_CONNECT_PATH.read_text()
        # goto call uses wait_until="load"
        assert 'wait_until="load"' in src
        # readyState check present
        assert "document.readyState === 'complete'" in src or 'document.readyState === "complete"' in src
        # ensures graceful catch of LoginInputNotFound
        assert "except LoginInputNotFound" in src
        # password/submit blocks untouched
        assert "page.locator('input[name=\"password\"]').first" in src
        assert "page.locator('button[type=\"submit\"]').first" in src
        # popup constants
        for name in ("CLICK_YES_TEXTS", "CLICK_DISMISS_TEXTS", "FORBIDDEN_TEXTS",
                     "_handle_popups", "_await_otp", "_detect_post_submit_state"):
            assert name in src

    def test_wait_ready_state_function_exists(self):
        assert callable(bc._wait_ready_state_complete)
        assert callable(bc._find_login_username_input)
        assert callable(bc._dump_debug_artifacts)


# ---------------------------------------------------------------------------
# Fake Playwright page/locator
# ---------------------------------------------------------------------------
class FakeLocator:
    def __init__(self, count_val=0, visible=False):
        self._count = count_val
        self._visible = visible
        self.first = self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible


class FakePage:
    def __init__(self, selector_map, url="https://www.instagram.com/accounts/login/",
                 title="Login • Instagram", content_html="<html></html>"):
        # selector_map: dict[selector] -> (count, visible)
        self.selector_map = selector_map
        self.url = url
        self._title = title
        self._content = content_html
        self.goto_wait_until = None
        self.wait_for_function_expr = None
        self.queried_selectors = []
        self.screenshot_called = False

    def locator(self, sel):
        self.queried_selectors.append(sel)
        cnt, vis = self.selector_map.get(sel, (0, False))
        return FakeLocator(cnt, vis)

    async def title(self):
        return self._title

    async def content(self):
        return self._content

    async def screenshot(self, path, full_page=True):
        self.screenshot_called = True
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_wait_until = wait_until

    async def wait_for_function(self, expr, timeout=None):
        self.wait_for_function_expr = expr
        return True


# ---------------------------------------------------------------------------
# Behaviour tests via monkey-patched Playwright page
# ---------------------------------------------------------------------------
class TestFinderBehaviour:
    def test_failure_raises_login_input_not_found_with_artifacts(self):
        page = FakePage(selector_map={s: (0, False) for s in bc.LOGIN_USERNAME_SELECTORS})

        with pytest.raises(bc.LoginInputNotFound) as ei:
            asyncio.run(bc._find_login_username_input(page, total_timeout_ms=500, tag="testuser"))

        exc = ei.value
        assert "within" in str(exc)
        assert isinstance(exc.artifacts, dict)
        # url/title/html/screenshot should be populated
        assert exc.artifacts.get("url", "").startswith("https://www.instagram.com/")
        assert "html" in exc.artifacts
        assert "screenshot" in exc.artifacts
        # readyState wait was invoked
        assert page.wait_for_function_expr is not None
        assert "readyState" in page.wait_for_function_expr

    def test_success_on_third_selector(self):
        target = 'input[name="username"]'
        sm = {s: (0, False) for s in bc.LOGIN_USERNAME_SELECTORS}
        sm[target] = (1, True)
        page = FakePage(selector_map=sm)

        loc, matched = asyncio.run(
            bc._find_login_username_input(page, total_timeout_ms=2000, tag="user2")
        )
        assert matched == target
        # confirm earlier selectors were queried before matching
        first_two = list(bc.LOGIN_USERNAME_SELECTORS[:2])
        for s in first_two:
            assert s in page.queried_selectors

    def test_success_first_selector_short_circuits(self):
        target = bc.LOGIN_USERNAME_SELECTORS[0]
        sm = {s: (0, False) for s in bc.LOGIN_USERNAME_SELECTORS}
        sm[target] = (1, True)
        page = FakePage(selector_map=sm)

        loc, matched = asyncio.run(
            bc._find_login_username_input(page, total_timeout_ms=2000, tag="user1")
        )
        assert matched == target
        # Only the first selector should have been queried in the first pass
        assert page.queried_selectors[0] == target
        # Others should not appear (found before iterating further)
        for s in bc.LOGIN_USERNAME_SELECTORS[1:]:
            assert s not in page.queried_selectors


# ---------------------------------------------------------------------------
# Regression: preserve iteration_3 behaviours (CSV, registry, snapshot, otp)
# ---------------------------------------------------------------------------
class TestRegressionCore:
    def test_parse_csv_valid(self):
        rows = bc.parse_csv(b"username,password\n alice ,pw1\n@bob,pw2\n")
        assert rows == [
            {"username": "alice", "password": "pw1"},
            {"username": "bob", "password": "pw2"},
        ]

    def test_parse_csv_missing_header(self):
        with pytest.raises(bc.CsvParseError):
            bc.parse_csv(b"user,pass\nalice,pw\n")

    def test_parse_csv_empty(self):
        with pytest.raises(bc.CsvParseError):
            bc.parse_csv(b"")

    def test_parse_csv_dedupe(self):
        rows = bc.parse_csv(b"username,password\nalice,pw1\nAlice,pw2\n")
        assert len(rows) == 1

    def test_registry_create_and_snapshot(self):
        job = bc.REGISTRY.create([{"username": "u1", "password": "p1"}])
        snap = job.status_snapshot()
        assert snap["totals"]["total"] == 1
        # No password leak in snapshot
        import json
        assert "p1" not in json.dumps(snap)

    def test_submit_otp_guard_wrong_state(self):
        job = bc.REGISTRY.create([{"username": "u2", "password": "p2"}])
        # status is 'pending', not 'otp_required' → guard rejects
        assert bc.submit_otp(job.job_id, 0, "123456") is False

    def test_submit_otp_success(self):
        job = bc.REGISTRY.create([{"username": "u3", "password": "p3"}])
        job.accounts[0].status = "otp_required"
        assert bc.submit_otp(job.job_id, 0, "654321") is True
        assert job._otp_codes[0] == "654321"

    def test_submit_otp_empty(self):
        job = bc.REGISTRY.create([{"username": "u4", "password": "p4"}])
        job.accounts[0].status = "otp_required"
        assert bc.submit_otp(job.job_id, 0, "   ") is False

    def test_get_status_missing(self):
        assert bc.get_status("nonexistent") is None
