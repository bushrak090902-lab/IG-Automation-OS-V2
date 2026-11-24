"""Instagram Services diagnostics: worker helpers, API and real-browser tests.

API tests hit a LIVE backend: IG_BASE_URL (default http://localhost:8001).
Browser tests are gated behind RUN_BROWSER_DIAG=1. They seed a dedicated
TEST_ account with an empty profile and temporarily speed up pacing settings,
then restore them. Existing accounts, profiles and orders are never touched.
Without a real Instagram session on the host, the end-to-end order is
expected to FAIL the session-health check — an honest result, never faked.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

BASE = os.environ.get("IG_BASE_URL", "http://localhost:8001") + "/api/v1/instagram-services"
REEL = "https://www.instagram.com/reels/Df6_NPAMPGc/"


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=30)


_created: list[int] = []


def create_order(service="likes", url="https://www.instagram.com/reel/CxTestPost123/", qty=150):
    with client() as c:
        r = c.post("/orders", json={"service_type": service, "instagram_url": url,
                                    "requested_quantity": qty, "notes": "pytest diagnostics"})
        assert r.status_code == 201, r.text
        _created.append(r.json()["id"])
        return r.json()


@pytest.fixture(autouse=True)
def _cancel_test_orders():
    """Cancel every order a test created so queued test jobs never keep
    running against accounts after the test ends."""
    yield
    with client() as c:
        while _created:
            c.post(f"/orders/{_created.pop()}/cancel")


def _db():
    from app.database import SessionLocal
    return SessionLocal()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_detect_restriction_urls_and_text(self):
        from app.queue.instagram_services_worker import detect_restriction
        assert detect_restriction("https://www.instagram.com/challenge/abc/", "") == "url:challenge"
        assert detect_restriction("https://www.instagram.com/accounts/suspended/", "")
        assert detect_restriction("https://www.instagram.com/", "Try Again Later. We restrict certain activity")
        assert detect_restriction("https://www.instagram.com/reel/x/", "0 likes, 0 comments") is None

    def test_parse_metric(self):
        from app.queue.instagram_services_worker import parse_metric
        assert parse_metric("likes", "1,234 likes, 56 comments - user on May 1") == ("likes", 1234)
        assert parse_metric("comments", "1,234 likes, 56 comments - user") == ("comments", 56)
        assert parse_metric("followers", "12.5K Followers, 10 Following, 500 Posts") == ("followers", 12500)
        assert parse_metric("followers", "3M Followers, 1 Following") == ("followers", 3000000)
        assert parse_metric("views", "1,234 likes") == (None, None)
        assert parse_metric("likes", "no counts here") == ("likes", None)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class TestOrderApi:
    def test_create_order_enqueues_job_and_reports_zero_delivered(self):
        o = create_order()
        assert o["execution_mode"] == "diagnostic"
        assert o["status"] in ("pending", "processing")
        assert o["completed_quantity"] == 0
        assert isinstance(o["job_id"], int)
        assert o["observed_delta"] is None and o["operator_delivered"] == 0

    def test_logs_record_queueing(self):
        o = create_order()
        with client() as c:
            kinds = [l["event_type"] for l in c.get(f"/orders/{o['id']}/logs").json()]
        assert "created" in kinds and "job_queued" in kinds

    def test_validation(self):
        with client() as c:
            r1 = c.post("/orders", json={"service_type": "followers", "instagram_url": "https://www.instagram.com/p/Cx1/",
                                         "requested_quantity": 10})
            r2 = c.post("/orders", json={"service_type": "likes", "instagram_url": "https://example.com/p/x/",
                                         "requested_quantity": 10})
        assert r1.status_code == 422 and r2.status_code == 422

    def test_worker_status_ten_background_workers(self):
        with client() as c:
            s = c.get("/worker/status").json()
        assert s["workers"] == 10
        assert s["settings"]["show_browser"] is False
        assert len(s["worker_status"]) == 10
        assert "paused" in s

    def test_stats_has_verified_bucket(self):
        with client() as c:
            s = c.get("/stats").json()
        assert "verified" in s and "completed" in s

    def test_patch_blocked_for_diagnostic_orders(self):
        o = create_order()
        with client() as c:
            r = c.patch(f"/orders/{o['id']}", json={"status": "completed", "completed_quantity": 150, "notes": "x"})
        assert r.status_code == 409

    def test_cancel_cancels_job_and_is_terminal(self):
        o = create_order()
        with client() as c:
            r = c.post(f"/orders/{o['id']}/cancel")
            assert r.status_code == 200 and r.json()["status"] == "cancelled"
            assert c.get(f"/orders/{o['id']}/job").json()["status"] == "cancelled"
            assert c.post(f"/orders/{o['id']}/cancel").status_code == 409


class TestDeliveryRecording:
    """Operator-recorded delivery is the only way delivered quantity changes."""

    def _make_verified(self, qty=100):
        from app.models import InstagramServiceJob, InstagramServiceOrder
        o = create_order(qty=qty)
        db = _db()
        try:
            # Test setup only: stop the job and mark the order verified so the
            # recording endpoint can be exercised without a real IG session.
            db.get(InstagramServiceJob, o["job_id"]).status = "cancelled"
            db.get(InstagramServiceOrder, o["id"]).status = "verified"
            db.commit()
        finally:
            db.close()
        return o

    def test_record_requires_verified(self):
        o = create_order()
        with client() as c:
            r = c.post(f"/orders/{o['id']}/deliveries", json={"quantity": 5, "source": "Meta ads report"})
            c.post(f"/orders/{o['id']}/cancel")
        assert r.status_code == 409

    def test_record_partial_then_complete(self):
        o = self._make_verified(qty=100)
        with client() as c:
            r = c.post(f"/orders/{o['id']}/deliveries", json={"quantity": 40, "source": "Meta ads report"})
            assert r.status_code == 201, r.text
            d = r.json()
            assert d["status"] == "partially_completed" and d["completed_quantity"] == 40
            assert d["operator_delivered"] == 40
            assert c.post(f"/orders/{o['id']}/deliveries", json={"quantity": 61, "source": "x"}).status_code == 422
            d = c.post(f"/orders/{o['id']}/deliveries", json={"quantity": 60, "source": "Meta ads report"}).json()
            assert d["status"] == "completed" and d["completed_quantity"] == 100
            logs = [l["event_type"] for l in c.get(f"/orders/{o['id']}/logs").json()]
        assert logs.count("operator_recorded") == 2

    def test_legacy_completed_diagnostic_shown_as_verified(self):
        from app.models import InstagramServiceJob, InstagramServiceOrder
        o = create_order()
        db = _db()
        try:
            db.get(InstagramServiceJob, o["job_id"]).status = "cancelled"
            db.get(InstagramServiceOrder, o["id"]).status = "completed"  # earlier-version row shape
            db.commit()
        finally:
            db.close()
        with client() as c:
            assert c.get(f"/orders/{o['id']}").json()["status"] == "verified"
            ids = [x["id"] for x in c.get("/orders", params={"status": "verified"}).json()]
            done = [x["id"] for x in c.get("/orders", params={"status": "completed"}).json()]
        assert o["id"] in ids and o["id"] not in done


# ---------------------------------------------------------------------------
# Real browser
# ---------------------------------------------------------------------------
BROWSER = pytest.mark.skipif(os.environ.get("RUN_BROWSER_DIAG") != "1",
                             reason="Real Chromium — set RUN_BROWSER_DIAG=1")

FAST = {"svc_diag_account_rest_min_seconds": "1", "svc_diag_account_rest_max_seconds": "2",
        "svc_diag_dwell_min_seconds": "1", "svc_diag_dwell_max_seconds": "2",
        "svc_diag_retry_backoff_seconds": "5,5", "svc_diag_session_cooldown_minutes": "0"}


@pytest.fixture(scope="module", autouse=True)
def seeded_account():
    """One dedicated TEST_ account (empty profile) so orders are processed
    instead of failing with no_connected_accounts. Run serially: -n 0."""
    from app.models import Account, Setting
    profile = BACKEND_DIR / "profiles" / "TEST_diag_profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".profile_placeholder").write_text("test profile dir", encoding="utf-8")
    db = _db()
    saved = {}
    try:
        for k, v in FAST.items():
            row = db.query(Setting).filter(Setting.key == k).first()
            saved[k] = row.value
            row.value = v
        acc = Account(account_name="TEST_diag_account", profile_path=str(profile),
                      session_status="connected", account_status="enabled")
        db.add(acc)
        db.commit()
        db.refresh(acc)
        yield {"id": acc.id}
    finally:
        for k, v in saved.items():
            db.query(Setting).filter(Setting.key == k).first().value = v
        db.query(Account).filter(Account.account_name == "TEST_diag_account").delete()
        db.commit()
        db.close()


@BROWSER
class TestBrowser:
    def test_real_reel_page_load_and_public_count(self):
        """Loads the Reel from the user's screenshot headless with the real
        readiness waits and reads its public like count."""
        from playwright.async_api import async_playwright
        from app.queue.instagram_services_worker import diagnostics_worker, _settings

        async def run():
            async with async_playwright() as p:
                b = await p.chromium.launch(headless=True)
                try:
                    return await diagnostics_worker.inspect_target(await b.new_page(), REEL, "likes", _settings())
                finally:
                    await b.close()
        m = asyncio.run(run())
        assert m["ready_selector"]
        assert m["metric"] == "likes" and isinstance(m["metric_value"], int)

    def test_order_end_to_end_headless_with_backoff(self, seeded_account):
        o = create_order(url=REEL)
        deadline, final = time.time() + 240, None
        with client() as c:
            while time.time() < deadline:
                d = c.get(f"/orders/{o['id']}").json()
                if d["status"] in ("verified", "failed", "cancelled"):
                    final = d
                    break
                time.sleep(3)
            logs = c.get(f"/orders/{o['id']}/logs").json()
            job = c.get(f"/orders/{o['id']}/job").json()
        assert final is not None, "order did not settle in 240s"
        msgs = " | ".join(l["message"] for l in logs)
        kinds = [l["event_type"] for l in logs]
        assert "browser_opened" in kinds and "background / headless" in msgs
        assert final["completed_quantity"] == 0
        if final["status"] == "failed":
            assert "session_unhealthy" in job["error"]
            assert "account_cooldown" in kinds and "retry_scheduled" in kinds
            retries = [l for l in logs if l["event_type"] == "retry_scheduled"]
            assert all("in 5s" in l["message"] for l in retries)
        from app.models import InstagramServiceAccountState
        db = _db()
        try:
            st = db.get(InstagramServiceAccountState, seeded_account["id"])
            assert st is not None and st.last_check_at is not None
        finally:
            db.close()
