#====================================================================================================
# START - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================

# THIS SECTION CONTAINS CRITICAL TESTING INSTRUCTIONS FOR BOTH AGENTS
# BOTH MAIN_AGENT AND TESTING_AGENT MUST PRESERVE THIS ENTIRE BLOCK

# Communication Protocol:
# If the `testing_agent` is available, main agent should delegate all testing tasks to it.
#
# You have access to a file called `test_result.md`. This file contains the complete testing state
# and history, and is the primary means of communication between main and the testing agent.
#
# Main and testing agents must follow this exact format to maintain testing data. 
# The testing data must be entered in yaml format Below is the data structure:
# 
## user_problem_statement: {problem_statement}
## backend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.py"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## frontend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.js"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## metadata:
##   created_by: "main_agent"
##   version: "1.0"
##   test_sequence: 0
##   run_ui: false
##
## test_plan:
##   current_focus:
##     - "Task name 1"
##     - "Task name 2"
##   stuck_tasks:
##     - "Task name with persistent issues"
##   test_all: false
##   test_priority: "high_first"  # or "sequential" or "stuck_first"
##
## agent_communication:
##     -agent: "main"  # or "testing" or "user"
##     -message: "Communication message between agents"

# Protocol Guidelines for Main agent
#
# 1. Update Test Result File Before Testing:
#    - Main agent must always update the `test_result.md` file before calling the testing agent
#    - Add implementation details to the status_history
#    - Set `needs_retesting` to true for tasks that need testing
#    - Update the `test_plan` section to guide testing priorities
#    - Add a message to `agent_communication` explaining what you've done
#
# 2. Incorporate User Feedback:
#    - When a user provides feedback that something is or isn't working, add this information to the relevant task's status_history
#    - Update the working status based on user feedback
#    - If a user reports an issue with a task that was marked as working, increment the stuck_count
#    - Whenever user reports issue in the app, if we have testing agent and task_result.md file so find the appropriate task for that and append in status_history of that task to contain the user concern and problem as well 
#
# 3. Track Stuck Tasks:
#    - Monitor which tasks have high stuck_count values or where you are fixing same issue again and again, analyze that when you read task_result.md
#    - For persistent issues, use websearch tool to find solutions
#    - Pay special attention to tasks in the stuck_tasks list
#    - When you fix an issue with a stuck task, don't reset the stuck_count until the testing agent confirms it's working
#
# 4. Provide Context to Testing Agent:
#    - When calling the testing agent, provide clear instructions about:
#      - Which tasks need testing (reference the test_plan)
#      - Any authentication details or configuration needed
#      - Specific test scenarios to focus on
#      - Any known issues or edge cases to verify
#
# 5. Call the testing agent with specific instructions referring to test_result.md
#
# IMPORTANT: Main agent must ALWAYS update test_result.md BEFORE calling the testing agent, as it relies on this file to understand what to test next.

#====================================================================================================
# END - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================



#====================================================================================================
# Testing Data - Main Agent and testing sub agent both should log testing data below this section
#====================================================================================================

user_problem_statement: |
  IG Automation OS — port the existing FastAPI + Jinja + SQLite foundation and extend it with the
  missing production core: a real Publishing Queue + Worker Pool + Playwright Publisher with
  per-account browser reuse. All existing endpoints (accounts, campaigns, bulk connect, settings,
  logs) must keep working; the new queue engine must materialise jobs, distribute them across a
  worker pool, retry transient failures and finalise campaigns. NOTE: this sandbox has no real
  logged-in Instagram sessions, so publish jobs are EXPECTED to fail fast with "login_required" —
  that is correct real behaviour, not a bug.

backend:
  - task: "Existing endpoints regression (accounts, campaigns, settings, logs, bulk settings)"
    implemented: true
    working: true
    file: "backend/app/api/accounts.py, campaigns.py, system.py, bulk_connect.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
        -working: true
        -agent: "main"
        -comment: "Ported from foundation repo. Verified boot + basic CRUD via curl. Needs regression pass."
        -working: true
        -agent: "testing"
        -comment: "✅ PASSED - All 18 regression tests passed: GET /api/health (200), GET/POST /api/v1/accounts (CRUD working), GET /api/v1/accounts/stats (all keys present), GET /api/v1/accounts/environment (200), PATCH /api/v1/accounts/{id} (updates working), GET /api/v1/campaigns (200), GET/PUT /api/v1/settings (working), GET /api/v1/logs with ?level=INFO filter (working), GET /api/bulk/settings (200), POST /api/bulk/settings (updates working), GET /api/bulk/batches (200), GET /api/bulk/environment (200), POST /api/bulk/csv/preview (returns token and valid count). No issues found."
  - task: "Publishing Queue engine — materialise jobs on campaign start"
    implemented: true
    working: true
    file: "backend/app/queue/queue_manager.py, backend/app/api/campaigns.py (POST /v1/campaigns/{id}/start)"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
        -working: true
        -agent: "main"
        -comment: "Verified: /start materialises Job rows per distribution mode and sets campaign running. Handles no-files / no-accounts with 400."
        -working: true
        -agent: "testing"
        -comment: "✅ PASSED - Full publish flow tested end-to-end: (1) POST /api/v1/accounts created account 'qa_acct_1' with profile_path set (201), (2) POST /api/v1/campaigns/upload with image file returned file_id (200), (3) POST /api/v1/campaigns with content_type=static, account_quantity=1, distribution_mode=same, file_ids=[id] created campaign with status=draft (201), (4) POST /api/v1/campaigns/{id}/start returned status=running and jobs_queued=1 (200), (5) POST /api/v1/campaigns/{id}/start twice returned 409 (already running) - correct behavior. Job materialization working correctly."
  - task: "Worker Pool with per-account browser reuse + retries + campaign finalize"
    implemented: true
    working: true
    file: "backend/app/queue/worker_pool.py, backend/app/automation/browser_pool.py, backend/app/automation/publisher.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
        -working: true
        -agent: "main"
        -comment: "Verified e2e: worker claims account, launches real persistent Chromium, ensure_logged_in fails fast with login_required (no session in sandbox), jobs -> failed, campaign -> completed. Fixed finalize bug on login_required path."
        -working: true
        -agent: "testing"
        -comment: "✅ PASSED - Worker pool functioning correctly: (1) GET /api/v1/queue/workers shows running=true and workers>=1, (2) Polled GET /api/v1/queue/jobs?campaign_id={id} for 30s, job transitioned from queued -> failed with error_message='login_required' (EXPECTED behavior in sandbox with no real IG sessions), (3) GET /api/v1/campaigns/{id} shows campaign status=completed and job_counts reflect failed job correctly. Database verification: Job status=failed, error_message='login_required', Campaign status=completed. Campaign finalization working correctly - no queued/processing jobs remain and status properly set to completed."
  - task: "Queue REST API — /v1/queue/stats, /workers, /jobs, /retry-failed"
    implemented: true
    working: true
    file: "backend/app/api/queue.py"
    stuck_count: 0
    priority: "high"
    needs_retesting: false
    status_history:
        -working: true
        -agent: "main"
        -comment: "Verified via curl: stats, workers status, jobs list with filters, retry-failed re-queues + re-opens campaign."
        -working: true
        -agent: "testing"
        -comment: "✅ PASSED - All queue endpoints working: (1) GET /api/v1/queue/stats returns {queued, processing, successful, failed, total, active_campaigns} with correct integer values (200), (2) GET /api/v1/queue/workers returns {running:true, workers:>=1, claimed_accounts:[], worker_status:{...}} (200), (3) GET /api/v1/queue/jobs?campaign_id={id} returns jobs list with correct structure (200), (4) GET /api/v1/queue/jobs?status=failed filter works correctly (200), (5) GET /api/v1/queue/jobs?q=<account_name> substring search works (200), (6) POST /api/v1/queue/retry-failed returns {requeued: N} and successfully re-queues failed jobs (200). All endpoints functioning as expected."

frontend:
  - task: "Queue page + campaign Start Publishing + live jobs table"
    implemented: true
    working: "NA"
    file: "backend/app/templates/queue.html, static/js/queue.js, templates/campaign_details.html, static/js/campaign_details.js"
    stuck_count: 0
    priority: "medium"
    needs_retesting: false
    status_history:
        -working: "NA"
        -agent: "main"
        -comment: "Server-rendered Jinja page. Rendered correctly in screenshot. Frontend auto-testing NOT run yet (awaiting user permission)."

metadata:
  created_by: "main_agent"
  version: "1.0"
  test_sequence: 1
  run_ui: false

test_plan:
  current_focus: []
  stuck_tasks: []
  test_all: false
  test_priority: "high_first"

agent_communication:
    -agent: "main"
    -message: |
      All routes are under /api (ingress requirement). Base URL for tests: use the internal
      http://0.0.0.0:8001 or the external REACT_APP_BACKEND_URL + /api. Key flow to validate:
      1) POST /api/v1/accounts {account_name} -> 201
      2) POST /api/v1/campaigns/upload (multipart content_type=static + a small image) -> file id
      3) POST /api/v1/campaigns {content_type:static, account_quantity:1, distribution_mode:same, file_ids:[id]} -> campaign
      4) POST /api/v1/campaigns/{id}/start -> {status:running, jobs_queued>=1}
      5) Poll GET /api/v1/queue/jobs?campaign_id={id} until job is terminal (expect status=failed,
         error_message=login_required within ~15s) and GET /api/v1/campaigns/{id} status -> completed.
      IMPORTANT: login_required is the EXPECTED terminal result in this sandbox (no real IG session).
      Do NOT treat login_required as a failure of the queue engine. Also validate: /start with no
      accounts returns 400; /v1/queue/retry-failed re-queues failed jobs; /v1/queue/stats and
      /v1/queue/workers return sane shapes. Please run a full regression on existing endpoints too.
    -agent: "testing"
    -message: |
      ✅ ALL BACKEND TESTS PASSED (28/28 tests, 0 failures, 1 warning)
      
      PRIORITY 1 - Publishing Queue Engine: ✅ WORKING
      - Queue stats endpoint: ✅ Returns correct structure with all required keys
      - Queue workers endpoint: ✅ Shows workers running (running=true, workers>=1)
      - Full publish flow: ✅ Complete end-to-end test passed
        * Account creation: ✅ POST /api/v1/accounts -> 201 with profile_path
        * File upload: ✅ POST /api/v1/campaigns/upload -> file_id
        * Campaign creation: ✅ POST /api/v1/campaigns -> campaign with status=draft
        * Campaign start: ✅ POST /api/v1/campaigns/{id}/start -> status=running, jobs_queued>=1
        * Idempotency: ✅ Starting twice returns 409 (already running)
        * Job processing: ✅ Jobs transition from queued -> failed with error_message='login_required' (EXPECTED)
        * Campaign finalization: ✅ Campaign status -> completed when no queued/processing jobs remain
      - Queue jobs endpoint: ✅ GET /api/v1/queue/jobs with filters (status, campaign_id, q) all working
      - Retry failed: ✅ POST /api/v1/queue/retry-failed re-queues jobs correctly
      
      PRIORITY 2 - Regression Tests: ✅ ALL WORKING (18 endpoints)
      - Health: ✅ GET /api/health
      - Accounts: ✅ GET/POST /api/v1/accounts, GET /api/v1/accounts/stats, PATCH /api/v1/accounts/{id}, GET /api/v1/accounts/environment
      - Campaigns: ✅ GET /api/v1/campaigns, GET /api/v1/campaigns/{id}
      - Settings: ✅ GET/PUT /api/v1/settings
      - Logs: ✅ GET /api/v1/logs (with ?level=INFO filter)
      - Bulk: ✅ GET /api/bulk/settings, POST /api/bulk/settings, GET /api/bulk/batches, GET /api/bulk/environment, POST /api/bulk/csv/preview
      
      DATABASE VERIFICATION:
      - Jobs table: ✅ Jobs created with correct campaign_id, account_id, status=failed, error_message='login_required'
      - Campaigns table: ✅ Campaign status correctly set to 'completed' after jobs finish
      
      WARNINGS (non-critical):
      - Account 'qa_acct_1' already exists from previous test run (handled gracefully with 409)
      
      NO CRITICAL ISSUES FOUND. All backend functionality working as expected.
      login_required errors are CORRECT behavior in this sandbox environment (no real Instagram sessions).