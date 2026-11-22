"""Diagnostics worker + Instagram Services API tests.

Hit the LIVE backend (same pattern as backend_test.py / the pod convention):
the server under test is the one the browser sees. Point it at a running
instance with IG_BASE_URL (default http://localhost:8001/api).

The full browser-backed end-to-end test (order -> queue -> real Chromium
diagnostic -> verified outcome) is gated behind RUN_BROWSER_DIAG=1 because it
needs a real Chromium install; it seeds its own TEST_ account with an empty
persistent profile and NEVER touches existing accounts, profiles or orders.
Without a real Instagram session the browser test is expected to fail the
session-health check honestly — that is correct behaviour, not a bug.
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

BASE = os.environ.get("IG_BASE_URL", "http://localhost:8001") + "/api/v1/instagram-services"
TIMEOUT = 30


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=TIMEOUT)


def create_order(service="views", url="https://www.instagram.com/reel/CxTestPost123/", qty=150):
    with client() as c:
        r = c.post("/orders", json={"service_type": service, "instagram_url": url,
                                    "requested_quantity": qty, "notes": "pytest diagnostics"})
        assert r.status_code == 201, r.text
        return r.json()


class TestOrderCreation:
    def test_create_order_saves_and_enqueues_diagnostic_job(self):
        o = create_order()
        assert o["execution_mode"] == "diagnostic"
        assert o["status"] == "pending"
        assert o["completed_quantity"] == 0  # requested quantity is a reference only
        assert isinstance(o["job_id"], int)
        assert o["job_status"] in ("queued", "assigned", "running")

    def test_order_logs_record_queueing(self):
        o = create_order()
        with client() as c:
            logs = c.get(f"/orders/{o['id']}/logs").json()
        kinds = [l["event_type"] for l in logs]
        assert "created" in kinds and "job_queued" in kinds
        assert any("reference only" in l["message"] or "reference" in l["message"] for l in logs)

    def test_followers_requires_profile_url(self):
        with client() as c:
            r = c.post("/orders", json={"service_type": "followers",
                                        "instagram_url": "https://www.instagram.com/p/CxTestPost123/",
                                        "requested_quantity": 10, "notes": ""})
        assert r.status_code == 422

    def test_invalid_host_rejected(self):
        with client() as c:
            r = c.post("/orders", json={"service_type": "likes",
                                        "instagram_url": "https://example.com/p/xyz/",
                                        "requested_quantity": 10, "notes": ""})
        assert r.status_code == 422


class TestOrderDetail:
    def test_get_order_includes_job_summary(self):
        o = create_order()
        with client() as c:
            d = c.get(f"/orders/{o['id']}").json()
        assert d["id"] == o["id"]
        assert d["job_id"] == o["job_id"]

    def test_job_endpoint_returns_full_job(self):
        o = create_order()
        with client() as c:
            r = c.get(f"/orders/{o['id']}/job")
        assert r.status_code == 200
        j = r.json()
        assert j["id"] == o["job_id"]
        assert j["status"] in ("queued", "assigned", "running", "succeeded", "failed", "cancelled")
        assert "attempt_count" in j and "max_attempts" in j

    def test_worker_status_endpoint(self):
        with client() as c:
            r = c.get("/worker/status")
        assert r.status_code == 200
        s = r.json()
        for key in ("running", "workers", "worker_status", "settings"):
            assert key in s
        assert s["settings"]["concurrency"] >= 1


class TestGuards:
    def test_patch_blocked_for_diagnostic_orders(self):
        o = create_order()
        with client() as c:
            r = c.patch(f"/orders/{o['id']}", json={"status": "completed",
                                                    "completed_quantity": 150, "notes": "x"})
        assert r.status_code == 409

    def test_cancel_cancels_job_too(self):
        o = create_order()
        with client() as c:
            r = c.post(f"/orders/{o['id']}/cancel")
            assert r.status_code == 200
            assert r.json()["status"] == "cancelled"
            j = c.get(f"/orders/{o['id']}/job").json()
        assert j["status"] == "cancelled"

    def test_cancel_completed_order_rejected(self):
        # A cancelled order is terminal; cancelling again must 409.
        o = create_order()
        with client() as c:
            c.post(f"/orders/{o['id']}/cancel")
            r = c.post(f"/orders/{o['id']}/cancel")
        assert r.status_code == 409


@pytest.fixture(scope="module")
def seeded_account():
    """Insert a dedicated TEST_ account with an empty persistent profile —
    never reuses or modifies existing accounts, profiles or orders."""
    from pathlib import Path
    import sys
    backend_dir = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(backend_dir))
    from app.database import SessionLocal
    from app.models import Account
    profile = backend_dir / "profiles" / "TEST_diag_profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".profile_placeholder").write_text("test profile dir", encoding="utf-8")
    db = SessionLocal()
    try:
        acc = Account(account_name="TEST_diag_account", profile_path=str(profile),
                      session_status="connected", account_status="enabled")
        db.add(acc)
        db.commit()
        db.refresh(acc)
        yield {"id": acc.id, "name": acc.account_name}
    finally:
        db.query(Account).filter(Account.account_name == "TEST_diag_account").delete()
        db.commit()
        db.close()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_DIAG") != "1",
                    reason="Real Chromium diagnostic — set RUN_BROWSER_DIAG=1 to run")
class TestBrowserDiagnosticEndToEnd:
    """Real background execution proof: order -> queued job -> worker claim ->
    real Chromium launch -> real navigation -> accurate logs/outcome.

    With no real Instagram session on disk the diagnostic is expected to fail
    the session-health check (or session_missing) — a meaningful, honest
    failure, never simulated success. With a real connected account profile
    present on the host (local Windows), the same flow demonstrates a PASS.
    """

    def test_order_runs_real_diagnostic(self, seeded_account):
        o = create_order()
        deadline = time.time() + 240
        final = None
        with client() as c:
            while time.time() < deadline:
                d = c.get(f"/orders/{o['id']}").json()
                if d["status"] in ("completed", "failed", "cancelled"):
                    final = d
                    break
                time.sleep(3)
            logs = c.get(f"/orders/{o['id']}/logs").json()
            job = c.get(f"/orders/{o['id']}/job").json()
        assert final is not None, "diagnostic did not settle within 240s"
        assert final["status"] in ("completed", "failed")
        # Real execution evidence must exist in the logs regardless of outcome.
        kinds = [l["event_type"] for l in logs]
        assert "job_started" in kinds, kinds
        assert "browser_opened" in kinds or "session_missing" in str([l["message"] for l in logs]), kinds
        assert final["completed_quantity"] == 0
        if final["status"] == "failed":
            assert job["error"], "failed job must record a meaningful error"
        else:
            r = job["result"]
            assert r["session_healthy"] and r["target_accessible"]
