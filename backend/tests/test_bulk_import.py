"""Bulk Import & Bulk Connect module tests (Phase 2b).

Executes only functionality that does NOT require live Playwright / Instagram:
    * CSV parser edge cases
    * In-memory job registry + state machine
    * OTP submission guards
    * Static-analysis assertions (no publish/password leaks, protected files)
    * FastAPI routes via TestClient (register + preview password-masking)
    * data-testid coverage on the new template
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Make the backend package importable no matter where pytest is invoked from.
BACKEND_DIR = Path("/app/backend")
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

APP_ROOT = Path("/app")


# ---------------------------------------------------------------------------
# Protected files invariant
# ---------------------------------------------------------------------------
class TestProtectedFiles:
    def test_publisher_untouched(self):
        out = subprocess.run(
            ["git", "diff", "--", "backend/app/automation/publisher.py"],
            cwd=APP_ROOT, capture_output=True, text=True,
        )
        assert out.stdout.strip() == "", f"publisher.py modified!\n{out.stdout}"

    def test_publisher_queue_untouched(self):
        out = subprocess.run(
            ["git", "diff", "--", "backend/app/automation/publisher_queue.py"],
            cwd=APP_ROOT, capture_output=True, text=True,
        )
        assert out.stdout.strip() == "", f"publisher_queue.py modified!\n{out.stdout}"

    def test_browser_session_untouched(self):
        out = subprocess.run(
            ["git", "diff", "--", "backend/app/automation/browser_session.py"],
            cwd=APP_ROOT, capture_output=True, text=True,
        )
        assert out.stdout.strip() == "", f"browser_session.py modified!\n{out.stdout}"


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------
class TestCompilation:
    @pytest.mark.parametrize("rel", [
        "backend/server.py",
        "backend/app/api/bulk_import.py",
        "backend/app/api/pages.py",
        "backend/app/automation/bulk_connect.py",
    ])
    def test_py_compile(self, rel):
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", str(APP_ROOT / rel)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# Static-analysis on bulk_connect.py
# ---------------------------------------------------------------------------
BULK_SRC = (APP_ROOT / "backend/app/automation/bulk_connect.py").read_text()


class TestStaticAnalysis:
    def test_import_isolation(self):
        # Must import as `from . import browser_session` — not `from .browser_session import ...`
        assert "from . import browser_session" in BULK_SRC
        assert "from .browser_session import" not in BULK_SRC

    def test_no_publish_primitives(self):
        for forbidden in ("configure_to_clips", "_perform_publish", "set_input_files", "publish_job"):
            assert forbidden not in BULK_SRC, f"Forbidden publish primitive present: {forbidden}"

    def test_password_never_persisted(self):
        # No Setting() / Account() model instantiation storing password fields.
        # (Non-DOTALL so we only catch single-line kwarg patterns like
        # Account(password=..., ...) — not multi-line def _connect_one_account signatures.)
        assert not re.search(r"Setting\([^)\n]*password", BULK_SRC, re.I)
        assert not re.search(r"\bAccount\([^)\n]*password\s*=", BULK_SRC, re.I)
        # No open() writing anything named password.
        assert not re.search(r"open\([^)\n]*password", BULK_SRC, re.I)
        # No log call that formats a password variable inline.
        assert not re.search(r"_log\([^)\n]*\bpassword\b", BULK_SRC)
        assert not re.search(r"db_log\([^)\n]*\bpassword\b", BULK_SRC)

    def test_password_dataclass_and_clearing(self):
        assert "_passwords: Dict[int, str]" in BULK_SRC
        # Driver pops the password after each account.
        assert "job._passwords.pop(state.index" in BULK_SRC

    def test_popup_lists_present(self):
        assert "CLICK_YES_TEXTS" in BULK_SRC
        assert "Save Info" in BULK_SRC
        assert "CLICK_DISMISS_TEXTS" in BULK_SRC
        assert "Not Now" in BULK_SRC and "Skip" in BULK_SRC and "Maybe Later" in BULK_SRC
        assert "FORBIDDEN_TEXTS" in BULK_SRC
        assert "Turn On" in BULK_SRC
        assert "Enable Contacts" in BULK_SRC
        assert "Sync Contacts" in BULK_SRC

    def test_handle_popups_does_not_click_forbidden(self):
        # Extract _handle_popups body.
        m = re.search(r"async def _handle_popups\(.*?\n(?=async def |\Z)", BULK_SRC, re.S)
        assert m, "_handle_popups not found"
        body = m.group(0)
        # Strip out the docstring first (the docstring may name Turn On/etc as
        # part of the explanatory rules — that's not a click).
        body_no_doc = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
        # Also strip inline comments.
        body_no_comments = re.sub(r"#[^\n]*", "", body_no_doc)
        for bad in ("Turn On", "Turn on", "Enable Contacts", "Sync Contacts"):
            assert bad not in body_no_comments, (
                f"_handle_popups references forbidden click text outside docstring: {bad}"
            )

    def test_no_large_fixed_sleeps(self):
        # Flag asyncio.sleep(N) where N > 2
        for m in re.finditer(r"asyncio\.sleep\(([0-9.]+)\)", BULK_SRC):
            val = float(m.group(1))
            assert val <= 2.0, f"Large fixed sleep found: asyncio.sleep({val})"


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------
class TestCsvParser:
    def _parse(self, data: bytes):
        from app.automation import bulk_connect
        return bulk_connect.parse_csv(data)

    def test_valid_two_rows(self):
        data = b"username,password\nalice,pw1\nbob,pw2\n"
        rows = self._parse(data)
        assert len(rows) == 2
        assert rows[0] == {"username": "alice", "password": "pw1"}

    def test_missing_password_column(self):
        from app.automation.bulk_connect import CsvParseError
        with pytest.raises(CsvParseError):
            self._parse(b"username,foo\nalice,x\n")

    def test_blank_rows_skipped(self):
        data = b"username,password\nalice,pw1\n\n,\nbob,pw2\n"
        rows = self._parse(data)
        assert [r["username"] for r in rows] == ["alice", "bob"]

    def test_utf8_bom_header(self):
        data = "\ufeffusername,password\nalice,pw1\n".encode("utf-8")
        rows = self._parse(data)
        assert rows == [{"username": "alice", "password": "pw1"}]

    def test_duplicate_usernames_dedup(self):
        data = b"username,password\nalice,pw1\nAlice,pw2\nalice,pw3\n"
        rows = self._parse(data)
        assert len(rows) == 1
        assert rows[0]["username"] == "alice"

    def test_empty_csv_raises(self):
        from app.automation.bulk_connect import CsvParseError
        with pytest.raises(CsvParseError):
            self._parse(b"")

    def test_case_insensitive_headers(self):
        data = b"Username,Password\nalice,pw1\n"
        rows = self._parse(data)
        assert rows == [{"username": "alice", "password": "pw1"}]


# ---------------------------------------------------------------------------
# Registry / state / OTP
# ---------------------------------------------------------------------------
class TestRegistryAndState:
    def _fresh(self):
        from app.automation import bulk_connect
        # Isolate: create job via REGISTRY.create (does NOT start task)
        rows = [
            {"username": "u1", "password": "SuperSecret111"},
            {"username": "u2", "password": "SuperSecret222"},
            {"username": "u3", "password": "SuperSecret333"},
        ]
        job = bulk_connect.REGISTRY.create(rows)
        return bulk_connect, job

    def test_create_and_initial_state(self):
        bc, job = self._fresh()
        assert len(job.accounts) == 3
        assert job.status == "idle"
        for a in job.accounts:
            assert a.status == "pending"

    def test_status_snapshot_shape(self):
        bc, job = self._fresh()
        snap = job.status_snapshot()
        assert set(snap.keys()) >= {"job_id", "status", "totals", "eta_seconds", "accounts"}
        t = snap["totals"]
        for k in ("total", "connected", "failed", "otp_required", "running", "remaining", "processed", "by_status"):
            assert k in t, f"Missing totals key: {k}"
        assert t["total"] == 3
        assert t["remaining"] == 3

    def test_snapshot_does_not_leak_password(self):
        bc, job = self._fresh()
        import json
        payload = json.dumps(job.status_snapshot())
        assert "SuperSecret" not in payload

    def test_otp_wrong_state_returns_false(self):
        bc, job = self._fresh()
        assert bc.submit_otp(job.job_id, 0, "123456") is False  # status = pending

    def test_otp_correct_state_returns_true(self):
        bc, job = self._fresh()
        job.accounts[1].status = "otp_required"
        assert bc.submit_otp(job.job_id, 1, "123456") is True
        assert job._otp_codes.get(1) == "123456"

    def test_otp_empty_code_returns_false(self):
        bc, job = self._fresh()
        job.accounts[0].status = "otp_required"
        assert bc.submit_otp(job.job_id, 0, "") is False
        assert bc.submit_otp(job.job_id, 0, "   ") is False


# ---------------------------------------------------------------------------
# FastAPI routes via TestClient
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    # Import lazily; sandbox has SQLAlchemy but not necessarily playwright at
    # top level (bulk_connect defers the playwright import).
    from fastapi.testclient import TestClient
    import server  # /app/backend/server.py
    return TestClient(server.app)


class TestRoutesRegistered:
    def test_expected_paths_present(self, client):
        import server
        paths = {getattr(r, "path", "") for r in server.app.routes}
        # Some paths are under mounted routers; also inspect included routers.
        # Use OpenAPI schema for completeness.
        schema = client.get("/api/openapi.json")
        # OpenAPI may be at /api/openapi.json OR /openapi.json depending on mount
        if schema.status_code != 200:
            schema = client.get("/openapi.json")
        assert schema.status_code == 200, "OpenAPI schema not accessible"
        paths_openapi = set(schema.json().get("paths", {}).keys())
        all_paths = paths | paths_openapi
        needed = [
            "/api/bulk-import",
            "/api/v1/bulk-import/preview",
            "/api/v1/bulk-import/start",
            "/api/v1/bulk-import/status",
            "/api/v1/bulk-import/{job_id}/status",
            "/api/v1/bulk-import/{job_id}/accounts/{account_index}/otp",
        ]
        for p in needed:
            assert p in all_paths, f"Missing route: {p}\nHave: {sorted(x for x in all_paths if 'bulk' in x)}"


class TestPreviewEndpoint:
    def test_preview_masks_password(self, client):
        csv_bytes = b"username,password\nalice,SuperSecret123\nbob,AnotherSecret456\n"
        r = client.post(
            "/api/v1/bulk-import/preview",
            files={"csv_file": ("in.csv", csv_bytes, "text/csv")},
        )
        assert r.status_code == 200, r.text
        body_text = r.text
        assert "SuperSecret123" not in body_text
        assert "AnotherSecret456" not in body_text
        data = r.json()
        assert data["count"] == 2
        for a in data["accounts"]:
            assert "password_masked" in a
            assert a["password_masked"] and a["password_masked"] != "SuperSecret123"

    def test_preview_bad_csv_returns_400(self, client):
        r = client.post(
            "/api/v1/bulk-import/preview",
            files={"csv_file": ("in.csv", b"foo,bar\n1,2\n", "text/csv")},
        )
        assert r.status_code == 400

    def test_status_when_no_job(self, client):
        r = client.get("/api/v1/bulk-import/status")
        assert r.status_code == 200
        d = r.json()
        # No job or an idle placeholder
        assert d.get("status") in (None, "idle", "completed", "running")


# ---------------------------------------------------------------------------
# data-testid coverage in template & sidebar
# ---------------------------------------------------------------------------
class TestTemplates:
    def test_bulk_import_testids(self):
        html = (APP_ROOT / "backend/app/templates/bulk_import.html").read_text()
        for tid in [
            "page-bulk-import", "csv-file-input", "btn-preview", "btn-start",
            "bulk-progress-card", "bulk-accounts-card", "bulk-accounts-table",
            "stat-total", "stat-connected", "stat-failed", "stat-otp",
            "stat-running", "stat-remaining", "progress-bar", "progress-percent",
            "progress-eta",
        ]:
            assert f'data-testid="{tid}"' in html, f"Missing testid: {tid}"

    def test_sidebar_link(self):
        html = (APP_ROOT / "backend/app/templates/base.html").read_text()
        assert 'href="/api/bulk-import"' in html
        assert 'data-testid="nav-bulk-import"' in html
