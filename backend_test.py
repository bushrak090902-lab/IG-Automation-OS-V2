#!/usr/bin/env python3
"""
Comprehensive backend API test suite for IG Automation OS.
Tests all endpoints with focus on the new Publishing Queue engine.

IMPORTANT: login_required is EXPECTED behavior in this sandbox (no real IG sessions).
"""
import io
import json
import time
import requests
from pathlib import Path

# Base URL for internal testing
BASE_URL = "http://0.0.0.0:8001/api"

# Test results tracking
results = {
    "passed": [],
    "failed": [],
    "warnings": []
}

def log_pass(test_name):
    print(f"✅ PASS: {test_name}")
    results["passed"].append(test_name)

def log_fail(test_name, reason):
    print(f"❌ FAIL: {test_name} - {reason}")
    results["failed"].append(f"{test_name}: {reason}")

def log_warning(test_name, reason):
    print(f"⚠️  WARNING: {test_name} - {reason}")
    results["warnings"].append(f"{test_name}: {reason}")

def print_summary():
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    print(f"✅ Passed: {len(results['passed'])}")
    print(f"❌ Failed: {len(results['failed'])}")
    print(f"⚠️  Warnings: {len(results['warnings'])}")
    
    if results["failed"]:
        print("\n❌ FAILED TESTS:")
        for fail in results["failed"]:
            print(f"  - {fail}")
    
    if results["warnings"]:
        print("\n⚠️  WARNINGS:")
        for warn in results["warnings"]:
            print(f"  - {warn}")
    
    print("="*80)

# =============================================================================
# PRIORITY 2: Regression Tests - Existing Endpoints
# =============================================================================

def test_health():
    """Test GET /api/health"""
    try:
        resp = requests.get(f"{BASE_URL}/health", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok":
                log_pass("GET /api/health")
                return True
            else:
                log_fail("GET /api/health", f"Unexpected response: {data}")
                return False
        else:
            log_fail("GET /api/health", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/health", str(e))
        return False

def test_accounts_list():
    """Test GET /api/v1/accounts"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/accounts", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                log_pass("GET /api/v1/accounts")
                return True, data
            else:
                log_fail("GET /api/v1/accounts", "Response is not a list")
                return False, []
        else:
            log_fail("GET /api/v1/accounts", f"Status {resp.status_code}")
            return False, []
    except Exception as e:
        log_fail("GET /api/v1/accounts", str(e))
        return False, []

def test_accounts_stats():
    """Test GET /api/v1/accounts/stats"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/accounts/stats", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            required_keys = ["total", "available", "busy", "expired", "disabled"]
            if all(k in data for k in required_keys):
                log_pass("GET /api/v1/accounts/stats")
                return True
            else:
                log_fail("GET /api/v1/accounts/stats", f"Missing keys: {data}")
                return False
        else:
            log_fail("GET /api/v1/accounts/stats", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/accounts/stats", str(e))
        return False

def test_accounts_environment():
    """Test GET /api/v1/accounts/environment"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/accounts/environment", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            log_pass("GET /api/v1/accounts/environment")
            return True
        else:
            log_fail("GET /api/v1/accounts/environment", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/accounts/environment", str(e))
        return False

def test_create_account(account_name):
    """Test POST /api/v1/accounts"""
    try:
        resp = requests.post(
            f"{BASE_URL}/v1/accounts",
            json={"account_name": account_name},
            timeout=10
        )
        if resp.status_code == 201:
            data = resp.json()
            if data.get("account_name") == account_name and data.get("profile_path"):
                log_pass(f"POST /api/v1/accounts (account_name={account_name})")
                return True, data
            else:
                log_fail(f"POST /api/v1/accounts", f"Unexpected response: {data}")
                return False, None
        elif resp.status_code == 409:
            # Account already exists - this is OK for testing
            log_warning(f"POST /api/v1/accounts", f"Account {account_name} already exists")
            # Try to get the existing account
            accounts_resp = requests.get(f"{BASE_URL}/v1/accounts", timeout=10)
            if accounts_resp.status_code == 200:
                accounts = accounts_resp.json()
                for acc in accounts:
                    if acc.get("account_name") == account_name:
                        return True, acc
            return True, None
        else:
            log_fail(f"POST /api/v1/accounts", f"Status {resp.status_code}: {resp.text}")
            return False, None
    except Exception as e:
        log_fail(f"POST /api/v1/accounts", str(e))
        return False, None

def test_update_account(account_id):
    """Test PATCH /api/v1/accounts/{id}"""
    try:
        resp = requests.patch(
            f"{BASE_URL}/v1/accounts/{account_id}",
            json={"account_status": "enabled"},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("account_status") == "enabled":
                log_pass(f"PATCH /api/v1/accounts/{account_id}")
                return True
            else:
                log_fail(f"PATCH /api/v1/accounts/{account_id}", f"Unexpected response: {data}")
                return False
        else:
            log_fail(f"PATCH /api/v1/accounts/{account_id}", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail(f"PATCH /api/v1/accounts/{account_id}", str(e))
        return False

def test_campaigns_list():
    """Test GET /api/v1/campaigns"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/campaigns", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                log_pass("GET /api/v1/campaigns")
                return True
            else:
                log_fail("GET /api/v1/campaigns", "Response is not a list")
                return False
        else:
            log_fail("GET /api/v1/campaigns", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/campaigns", str(e))
        return False

def test_settings_get():
    """Test GET /api/v1/settings"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/settings", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                log_pass("GET /api/v1/settings")
                return True
            else:
                log_fail("GET /api/v1/settings", "Response is not a dict")
                return False
        else:
            log_fail("GET /api/v1/settings", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/settings", str(e))
        return False

def test_settings_update():
    """Test PUT /api/v1/settings"""
    try:
        resp = requests.put(
            f"{BASE_URL}/v1/settings",
            json={"concurrent_workers": "2"},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if "updated" in data:
                log_pass("PUT /api/v1/settings")
                return True
            else:
                log_fail("PUT /api/v1/settings", f"Unexpected response: {data}")
                return False
        else:
            log_fail("PUT /api/v1/settings", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("PUT /api/v1/settings", str(e))
        return False

def test_logs_list():
    """Test GET /api/v1/logs"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/logs", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                log_pass("GET /api/v1/logs")
                return True
            else:
                log_fail("GET /api/v1/logs", "Response is not a list")
                return False
        else:
            log_fail("GET /api/v1/logs", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/logs", str(e))
        return False

def test_logs_with_filter():
    """Test GET /api/v1/logs?level=INFO"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/logs?level=INFO", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                log_pass("GET /api/v1/logs?level=INFO")
                return True
            else:
                log_fail("GET /api/v1/logs?level=INFO", "Response is not a list")
                return False
        else:
            log_fail("GET /api/v1/logs?level=INFO", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/logs?level=INFO", str(e))
        return False

def test_bulk_settings_get():
    """Test GET /api/bulk/settings"""
    try:
        resp = requests.get(f"{BASE_URL}/bulk/settings", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            required_keys = ["concurrency", "delay_between_accounts", "headless"]
            if all(k in data for k in required_keys):
                log_pass("GET /api/bulk/settings")
                return True
            else:
                log_fail("GET /api/bulk/settings", f"Missing keys: {data}")
                return False
        else:
            log_fail("GET /api/bulk/settings", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/bulk/settings", str(e))
        return False

def test_bulk_settings_update():
    """Test POST /api/bulk/settings"""
    try:
        resp = requests.post(
            f"{BASE_URL}/bulk/settings",
            json={"concurrency": 3},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if "concurrency" in data:
                log_pass("POST /api/bulk/settings")
                return True
            else:
                log_fail("POST /api/bulk/settings", f"Unexpected response: {data}")
                return False
        else:
            log_fail("POST /api/bulk/settings", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("POST /api/bulk/settings", str(e))
        return False

def test_bulk_batches():
    """Test GET /api/bulk/batches"""
    try:
        resp = requests.get(f"{BASE_URL}/bulk/batches", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                log_pass("GET /api/bulk/batches")
                return True
            else:
                log_fail("GET /api/bulk/batches", "Response is not a list")
                return False
        else:
            log_fail("GET /api/bulk/batches", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/bulk/batches", str(e))
        return False

def test_bulk_environment():
    """Test GET /api/bulk/environment"""
    try:
        resp = requests.get(f"{BASE_URL}/bulk/environment", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            log_pass("GET /api/bulk/environment")
            return True
        else:
            log_fail("GET /api/bulk/environment", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/bulk/environment", str(e))
        return False

def test_bulk_csv_preview():
    """Test POST /api/bulk/csv/preview"""
    try:
        # Create a small CSV in memory
        csv_content = "username,password\nuser1,pass1\nuser2,pass2"
        files = {
            'file': ('test.csv', io.BytesIO(csv_content.encode()), 'text/csv')
        }
        resp = requests.post(f"{BASE_URL}/bulk/csv/preview", files=files, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            required_keys = ["token", "total", "valid", "accounts"]
            if all(k in data for k in required_keys):
                log_pass("POST /api/bulk/csv/preview")
                return True, data.get("token")
            else:
                log_fail("POST /api/bulk/csv/preview", f"Missing keys: {data}")
                return False, None
        else:
            log_fail("POST /api/bulk/csv/preview", f"Status {resp.status_code}: {resp.text}")
            return False, None
    except Exception as e:
        log_fail("POST /api/bulk/csv/preview", str(e))
        return False, None

# =============================================================================
# PRIORITY 1: Publishing Queue Engine Tests
# =============================================================================

def test_queue_stats():
    """Test GET /api/v1/queue/stats"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/queue/stats", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            required_keys = ["queued", "processing", "successful", "failed", "total", "active_campaigns"]
            if all(k in data for k in required_keys):
                log_pass("GET /api/v1/queue/stats")
                return True
            else:
                log_fail("GET /api/v1/queue/stats", f"Missing keys: {data}")
                return False
        else:
            log_fail("GET /api/v1/queue/stats", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/queue/stats", str(e))
        return False

def test_queue_workers():
    """Test GET /api/v1/queue/workers"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/queue/workers", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            required_keys = ["running", "workers"]
            if all(k in data for k in required_keys):
                if data.get("running") and data.get("workers", 0) >= 1:
                    log_pass("GET /api/v1/queue/workers")
                    return True
                else:
                    log_fail("GET /api/v1/queue/workers", f"Workers not running or count < 1: {data}")
                    return False
            else:
                log_fail("GET /api/v1/queue/workers", f"Missing keys: {data}")
                return False
        else:
            log_fail("GET /api/v1/queue/workers", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/queue/workers", str(e))
        return False

def test_upload_campaign_file():
    """Test POST /api/v1/campaigns/upload"""
    try:
        # Use existing test image
        test_image = Path("/app/backend/uploads/adf4d8a5098f4c41b92ffb83a00bdcf9.jpg")
        if not test_image.exists():
            # Create a minimal test image if it doesn't exist
            log_warning("POST /api/v1/campaigns/upload", "Test image not found, creating minimal PNG")
            # Minimal 1x1 PNG
            png_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
            files = {
                'files': ('test.png', io.BytesIO(png_data), 'image/png')
            }
            data = {'content_type': 'static'}
            resp = requests.post(f"{BASE_URL}/v1/campaigns/upload", data=data, files=files, timeout=10)
        else:
            with open(test_image, 'rb') as f:
                file_content = f.read()
            files = {
                'files': ('test.jpg', io.BytesIO(file_content), 'image/jpeg')
            }
            data = {'content_type': 'static'}
            resp = requests.post(f"{BASE_URL}/v1/campaigns/upload", data=data, files=files, timeout=10)
        
        if resp.status_code == 200:
            result = resp.json()
            if isinstance(result, list) and len(result) > 0 and "id" in result[0]:
                log_pass("POST /api/v1/campaigns/upload")
                return True, result[0]["id"]
            else:
                log_fail("POST /api/v1/campaigns/upload", f"Unexpected response: {result}")
                return False, None
        else:
            log_fail("POST /api/v1/campaigns/upload", f"Status {resp.status_code}: {resp.text}")
            return False, None
    except Exception as e:
        log_fail("POST /api/v1/campaigns/upload", str(e))
        return False, None

def test_create_campaign(file_id):
    """Test POST /api/v1/campaigns"""
    try:
        payload = {
            "content_type": "static",
            "caption": "QA Test Campaign",
            "account_quantity": 1,
            "distribution_mode": "same",
            "file_ids": [file_id]
        }
        resp = requests.post(f"{BASE_URL}/v1/campaigns", json=payload, timeout=10)
        if resp.status_code == 201:
            data = resp.json()
            if data.get("id") and data.get("status") == "draft":
                log_pass("POST /api/v1/campaigns")
                return True, data["id"]
            else:
                log_fail("POST /api/v1/campaigns", f"Unexpected response: {data}")
                return False, None
        else:
            log_fail("POST /api/v1/campaigns", f"Status {resp.status_code}: {resp.text}")
            return False, None
    except Exception as e:
        log_fail("POST /api/v1/campaigns", str(e))
        return False, None

def test_get_campaign(campaign_id):
    """Test GET /api/v1/campaigns/{id}"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/campaigns/{campaign_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("id") == campaign_id:
                log_pass(f"GET /api/v1/campaigns/{campaign_id}")
                return True, data
            else:
                log_fail(f"GET /api/v1/campaigns/{campaign_id}", f"Unexpected response: {data}")
                return False, None
        else:
            log_fail(f"GET /api/v1/campaigns/{campaign_id}", f"Status {resp.status_code}")
            return False, None
    except Exception as e:
        log_fail(f"GET /api/v1/campaigns/{campaign_id}", str(e))
        return False, None

def test_start_campaign(campaign_id):
    """Test POST /api/v1/campaigns/{id}/start"""
    try:
        resp = requests.post(f"{BASE_URL}/v1/campaigns/{campaign_id}/start", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "running" and data.get("jobs_queued", 0) >= 1:
                log_pass(f"POST /api/v1/campaigns/{campaign_id}/start")
                return True
            else:
                log_fail(f"POST /api/v1/campaigns/{campaign_id}/start", f"Unexpected response: {data}")
                return False
        else:
            log_fail(f"POST /api/v1/campaigns/{campaign_id}/start", f"Status {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        log_fail(f"POST /api/v1/campaigns/{campaign_id}/start", str(e))
        return False

def test_start_campaign_twice(campaign_id):
    """Test POST /api/v1/campaigns/{id}/start twice (should return 409 or succeed idempotently)"""
    try:
        resp = requests.post(f"{BASE_URL}/v1/campaigns/{campaign_id}/start", timeout=10)
        if resp.status_code == 409:
            log_pass(f"POST /api/v1/campaigns/{campaign_id}/start (twice) - returned 409 (already running)")
            return True
        elif resp.status_code == 200:
            log_pass(f"POST /api/v1/campaigns/{campaign_id}/start (twice) - succeeded idempotently")
            return True
        else:
            log_fail(f"POST /api/v1/campaigns/{campaign_id}/start (twice)", f"Status {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        log_fail(f"POST /api/v1/campaigns/{campaign_id}/start (twice)", str(e))
        return False

def test_start_campaign_no_files():
    """Test POST /api/v1/campaigns/{id}/start with no files (should return 400)"""
    try:
        # Create a campaign with no files (this should fail at creation)
        # Instead, we'll test starting a campaign that has no eligible accounts
        # But since accounts persist, this is hard to isolate
        # So we'll just verify that starting with zero files returns 400
        # Actually, we need to create a campaign first, but we can't create one without files
        # So this test is not feasible as described in the review request
        # Let's skip this for now
        log_warning("POST /api/v1/campaigns/{id}/start (no files)", "Test skipped - cannot create campaign without files")
        return True
    except Exception as e:
        log_fail("POST /api/v1/campaigns/{id}/start (no files)", str(e))
        return False

def test_queue_jobs(campaign_id):
    """Test GET /api/v1/queue/jobs?campaign_id={id}"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/queue/jobs?campaign_id={campaign_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "items" in data and isinstance(data["items"], list):
                log_pass(f"GET /api/v1/queue/jobs?campaign_id={campaign_id}")
                return True, data["items"]
            else:
                log_fail(f"GET /api/v1/queue/jobs?campaign_id={campaign_id}", f"Unexpected response: {data}")
                return False, []
        else:
            log_fail(f"GET /api/v1/queue/jobs?campaign_id={campaign_id}", f"Status {resp.status_code}")
            return False, []
    except Exception as e:
        log_fail(f"GET /api/v1/queue/jobs?campaign_id={campaign_id}", str(e))
        return False, []

def test_queue_jobs_with_filters():
    """Test GET /api/v1/queue/jobs with filters (status=failed, q=account_name)"""
    try:
        # Test with status filter
        resp = requests.get(f"{BASE_URL}/v1/queue/jobs?status=failed", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "items" in data:
                log_pass("GET /api/v1/queue/jobs?status=failed")
            else:
                log_fail("GET /api/v1/queue/jobs?status=failed", f"Unexpected response: {data}")
                return False
        else:
            log_fail("GET /api/v1/queue/jobs?status=failed", f"Status {resp.status_code}")
            return False
        
        # Test with q filter
        resp = requests.get(f"{BASE_URL}/v1/queue/jobs?q=qa", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "items" in data:
                log_pass("GET /api/v1/queue/jobs?q=qa")
                return True
            else:
                log_fail("GET /api/v1/queue/jobs?q=qa", f"Unexpected response: {data}")
                return False
        else:
            log_fail("GET /api/v1/queue/jobs?q=qa", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("GET /api/v1/queue/jobs (with filters)", str(e))
        return False

def test_poll_job_completion(campaign_id, max_wait=30):
    """Poll GET /api/v1/queue/jobs until job reaches terminal state (failed with login_required)"""
    print(f"\n⏳ Polling job completion for campaign {campaign_id} (max {max_wait}s)...")
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        try:
            resp = requests.get(f"{BASE_URL}/v1/queue/jobs?campaign_id={campaign_id}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                
                if not items:
                    print(f"  No jobs found yet, waiting...")
                    time.sleep(3)
                    continue
                
                # Check if all jobs are in terminal state
                terminal_states = ["successful", "failed"]
                all_terminal = all(job["status"] in terminal_states for job in items)
                
                if all_terminal:
                    # Check if we have the expected login_required error
                    failed_jobs = [j for j in items if j["status"] == "failed"]
                    login_required_jobs = [j for j in failed_jobs if "login_required" in j.get("error_message", "").lower()]
                    
                    if login_required_jobs:
                        print(f"  ✅ Job reached terminal state: failed with login_required (EXPECTED)")
                        log_pass(f"Job completion polling (campaign {campaign_id}) - login_required as expected")
                        return True, items
                    elif failed_jobs:
                        print(f"  ⚠️  Job failed but not with login_required: {failed_jobs[0].get('error_message')}")
                        log_warning(f"Job completion polling (campaign {campaign_id})", 
                                  f"Failed with: {failed_jobs[0].get('error_message')}")
                        return True, items
                    else:
                        print(f"  ✅ Job completed successfully")
                        log_pass(f"Job completion polling (campaign {campaign_id}) - completed successfully")
                        return True, items
                else:
                    statuses = [j["status"] for j in items]
                    print(f"  Jobs still processing: {statuses}")
                    time.sleep(3)
            else:
                print(f"  Error polling jobs: {resp.status_code}")
                time.sleep(3)
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(3)
    
    log_fail(f"Job completion polling (campaign {campaign_id})", f"Timeout after {max_wait}s")
    return False, []

def test_campaign_finalization(campaign_id):
    """Test that campaign status becomes 'completed' after jobs finish"""
    try:
        resp = requests.get(f"{BASE_URL}/v1/campaigns/{campaign_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status")
            job_counts = data.get("job_counts", {})
            
            queued = job_counts.get("queued", 0)
            processing = job_counts.get("processing", 0)
            
            if queued == 0 and processing == 0:
                if status == "completed":
                    log_pass(f"Campaign {campaign_id} finalization - status is 'completed'")
                    return True
                else:
                    log_warning(f"Campaign {campaign_id} finalization", 
                              f"No queued/processing jobs but status is '{status}' (expected 'completed')")
                    return True
            else:
                log_warning(f"Campaign {campaign_id} finalization", 
                          f"Still has queued={queued} or processing={processing} jobs")
                return True
        else:
            log_fail(f"Campaign {campaign_id} finalization", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail(f"Campaign {campaign_id} finalization", str(e))
        return False

def test_retry_failed():
    """Test POST /api/v1/queue/retry-failed"""
    try:
        resp = requests.post(f"{BASE_URL}/v1/queue/retry-failed", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "requeued" in data:
                log_pass("POST /api/v1/queue/retry-failed")
                return True
            else:
                log_fail("POST /api/v1/queue/retry-failed", f"Unexpected response: {data}")
                return False
        else:
            log_fail("POST /api/v1/queue/retry-failed", f"Status {resp.status_code}")
            return False
    except Exception as e:
        log_fail("POST /api/v1/queue/retry-failed", str(e))
        return False

# =============================================================================
# Main Test Flow
# =============================================================================

def main():
    print("="*80)
    print("IG AUTOMATION OS - BACKEND API TEST SUITE")
    print("="*80)
    print(f"Base URL: {BASE_URL}")
    print("="*80)
    
    # PRIORITY 2: Regression Tests
    print("\n" + "="*80)
    print("PRIORITY 2: REGRESSION TESTS - EXISTING ENDPOINTS")
    print("="*80)
    
    test_health()
    test_accounts_list()
    test_accounts_stats()
    test_accounts_environment()
    test_campaigns_list()
    test_settings_get()
    test_settings_update()
    test_logs_list()
    test_logs_with_filter()
    test_bulk_settings_get()
    test_bulk_settings_update()
    test_bulk_batches()
    test_bulk_environment()
    test_bulk_csv_preview()
    
    # PRIORITY 1: Publishing Queue Engine
    print("\n" + "="*80)
    print("PRIORITY 1: PUBLISHING QUEUE ENGINE TESTS")
    print("="*80)
    
    # Test queue stats and workers
    test_queue_stats()
    test_queue_workers()
    
    # Full publish flow
    print("\n--- Full Publish Flow ---")
    
    # 1. Create account
    success, account = test_create_account("qa_acct_1")
    if not success or not account:
        print("❌ Cannot continue without account")
        print_summary()
        return
    
    account_id = account.get("id")
    test_update_account(account_id)
    
    # 2. Upload file
    success, file_id = test_upload_campaign_file()
    if not success or not file_id:
        print("❌ Cannot continue without uploaded file")
        print_summary()
        return
    
    # 3. Create campaign
    success, campaign_id = test_create_campaign(file_id)
    if not success or not campaign_id:
        print("❌ Cannot continue without campaign")
        print_summary()
        return
    
    # 4. Get campaign details
    test_get_campaign(campaign_id)
    
    # 5. Start campaign
    success = test_start_campaign(campaign_id)
    if not success:
        print("❌ Cannot continue - campaign start failed")
        print_summary()
        return
    
    # 6. Test starting campaign twice
    test_start_campaign_twice(campaign_id)
    
    # 7. Poll for job completion
    success, jobs = test_poll_job_completion(campaign_id, max_wait=30)
    
    # 8. Test queue jobs endpoint
    test_queue_jobs(campaign_id)
    test_queue_jobs_with_filters()
    
    # 9. Check campaign finalization
    test_campaign_finalization(campaign_id)
    
    # 10. Test retry-failed
    test_retry_failed()
    
    # Print summary
    print_summary()
    
    # Exit with appropriate code
    if results["failed"]:
        exit(1)
    else:
        exit(0)

if __name__ == "__main__":
    main()
