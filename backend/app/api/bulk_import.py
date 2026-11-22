"""Bulk Import + Bulk Connect REST endpoints (Phase 2b).

Does NOT touch publisher.py, publisher_queue.py, or browser_session.py's
login logic. All new logic lives in `app.automation.bulk_connect`.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Body
from pydantic import BaseModel

from ..automation import bulk_connect

router = APIRouter(prefix="/v1/bulk-import", tags=["bulk-import"])


class OtpPayload(BaseModel):
    code: str


@router.post("/preview")
async def preview(csv_file: UploadFile = File(...)):
    """Parse the CSV and return a masked preview WITHOUT starting anything."""
    try:
        raw = await csv_file.read()
        rows = bulk_connect.parse_csv(raw)
    except bulk_connect.CsvParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad CSV: {e}")

    preview_rows = [
        {"index": i, "username": r["username"], "password_masked": "•" * 8}
        for i, r in enumerate(rows)
    ]
    return {"count": len(rows), "accounts": preview_rows}


@router.post("/start")
async def start(csv_file: UploadFile = File(...)):
    """Parse the CSV and start the bulk-connect job."""
    try:
        raw = await csv_file.read()
        rows = bulk_connect.parse_csv(raw)
    except bulk_connect.CsvParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        job = bulk_connect.start_job(rows)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {"job_id": job.job_id, "total": len(job.accounts)}


@router.get("/status")
async def status_current():
    """Return the current (most-recent) job status."""
    job = bulk_connect.current_job()
    if not job:
        return {"job_id": None, "status": "idle", "totals": None, "accounts": []}
    return job.status_snapshot()


@router.get("/{job_id}/status")
async def status_by_id(job_id: str):
    snap = bulk_connect.get_status(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return snap


@router.post("/{job_id}/accounts/{account_index}/otp")
async def submit_otp(job_id: str, account_index: int, payload: OtpPayload):
    ok = bulk_connect.submit_otp(job_id, account_index, payload.code)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Cannot submit OTP: job/account not found, wrong state, or empty code.",
        )
    return {"ok": True}
