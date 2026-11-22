# Instagram Services — Real Diagnostic Worker

Branch: `feature/instagram-services-diagnostics`

## What changed
Every order created from the existing **Create Order** form is now:

1. Saved immediately in SQLite (`instagram_service_orders`, `execution_mode="diagnostic"`).
2. Enqueued as a job in a new persistent SQLite queue table, `instagram_service_jobs`.
3. Claimed by the isolated diagnostics worker lane (`diag-1` … `diag-N`, concurrency 1 by default).
4. Assigned a connected account whose persistent profile no other lane is using: not a publishing-pool claimed account, not busy, connecting, or verifying, and not already claimed by another diagnostic worker.
5. Run as a real Playwright diagnostic against that account's persistent profile:
   - session health: a `sessionid` cookie is present, with no login redirect and no login form
   - target check: the profile, post, or Reel URL loads, isn't "page isn't available", and isn't login-walled
   - capture: a full-page `screenshot.png` and `meta.json` (final URL, title, og:title, description) saved to `backend/logs/instagram_services_debug/order_<id>_job_<id>_<ts>/`
6. Marked **completed** only when every check passes. Otherwise it's retried (default 2 retries), then marked **failed** with the real error (e.g. `session_unhealthy: sessionid=missing`).

The requested quantity is stored as an order reference only. `completed_quantity` stays 0, and the UI shows "Checks passed — 0 delivered". No likes, follows, comments, shares, or reposts are automated.

## Reliability features
- **Persistent queue:** jobs survive restarts.
- **Crash recovery:** on startup, `assigned`/`running` jobs are requeued, and orphaned `processing` orders get a new job.
- **Per-account browser lock:** the same profile is never opened by two diagnostic workers, and accounts claimed by publishing workers are skipped.
- **Timeouts:** a per-step Playwright timeout (`svc_diag_browser_timeout_ms`) and an overall job timeout (`svc_diag_job_timeout_seconds`).
- **Cancellation:** the Cancel button works for pending orders, and `POST /orders/{id}/cancel` also works on processing orders. The worker checks for cancellation between steps and closes the browser.
- **Cleanup:** the browser context is always closed in a `finally` block, and the account claim is always released.
- **Logs:** every real event is written to `instagram_service_order_logs`: job_queued, job_started, account_assigned, browser_opened, session_healthy, target_accessible, diagnostics_captured, browser_closed, retry_scheduled, diagnostic_failed, job_succeeded, cancelled, recovered.

## Settings (seeded only if absent; existing values untouched)
| key | default |
|---|---|
| svc_diag_concurrency | 1 |
| svc_diag_headless | false (visible Chromium) |
| svc_diag_browser_timeout_ms | 60000 |
| svc_diag_job_timeout_seconds | 180 |
| svc_diag_max_retries | 2 |

Concurrency changes apply on the next app restart. On a Linux host without a display, the worker falls back to headless mode and logs `headless_fallback`.

## New / changed API (all under `/api/v1/instagram-services`)
- `POST /orders` now creates a diagnostic order plus a job. The response includes `job_id`, `job_status`, `job_worker`, `job_attempts`, `job_max_attempts`, `job_error`, and `job_result`.
- `GET /orders`, `GET /orders/{id}`: these now include the job summary fields.
- `GET /orders/{id}/job`: the full job row.
- `GET /worker/status`: the diagnostics lane status and settings.
- `PATCH /orders/{id}`: returns 409 for diagnostic orders (only the worker can set their outcome).
- `POST /orders/{id}/cancel`: also cancels the queued or running job.

Historical `manual` and `test` (simulation) rows are untouched. The table labels each one as Manual, Test (Simulation), or Diagnostic.

## Run locally on Windows
```powershell
cd IG-Automation-OS-V2\backend
git fetch origin
git checkout feature/instagram-services-diagnostics
# (Back up your data first)
copy data\ig_automation.db data\ig_automation.db.bak
.venv\Scripts\activate          # or your existing venv
pip install -r requirements.txt
python -m playwright install chromium
uvicorn server:app --host 0.0.0.0 --port 8001
```
The new `instagram_service_jobs` table is created automatically on startup by `create_all`. Existing tables and rows are not modified.

Open http://localhost:8001/api/instagram-services, enter a Reel URL and quantity, then click **Create Order**. Within a few seconds a visible Chromium window opens on one connected account's profile, loads Instagram, checks the session, and opens the URL. Click **Details** to see live logs. Screenshots are in `backend\logs\instagram_services_debug\`.

### Tests
```powershell
# With the server running on 8001:
set IG_BASE_URL=http://localhost:8001
python -m pytest tests\test_instagram_services_diagnostics.py -v
# Real browser end-to-end (creates and then deletes a TEST_diag_account with an empty profile):
set RUN_BROWSER_DIAG=1
python -m pytest tests\test_instagram_services_diagnostics.py -v
```
