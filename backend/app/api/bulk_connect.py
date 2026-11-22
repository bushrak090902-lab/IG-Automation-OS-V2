"""Bulk Connect REST API — mounted under /api/bulk.

Completely isolated from publishing. Never touches publisher.py /
publisher_queue.py / browser_session.py.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse, PlainTextResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import BulkBatch, BulkBatchItem, Setting, Account
from ..schemas import (
    CSVPreviewResponse, BulkBatchCreateRequest, BulkBatchResponse, BulkBatchDetail,
    BulkBatchItemResponse, OTPSubmitRequest, SimpleOKResponse,
    BulkSettingsResponse, BulkSettingsUpdateRequest,
)
from ..services.logger import log
from ..automation import bulk_connect_worker as worker

router = APIRouter(prefix="/bulk", tags=["bulk-connect"])

# ---- CSV preview tokens (in-memory; passwords held here until batch start) ----
_PREVIEW_TOKENS: dict[str, dict] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================
# CSV Upload → Preview
# =============================================================
@router.post("/csv/preview", response_model=CSVPreviewResponse)
async def csv_preview(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a .csv file")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Unable to decode CSV: {e}") from e

    rows, errors = worker.parse_csv(text)
    if not rows:
        raise HTTPException(status_code=400, detail="No valid username,password rows found. " + ("; ".join(errors[:5]) if errors else ""))

    # De-duplicate by username (keep first)
    seen = set()
    unique: list[tuple[str, str]] = []
    duplicates = 0
    for u, p in rows:
        if u.lower() in seen:
            duplicates += 1
            continue
        seen.add(u.lower())
        unique.append((u, p))

    token = secrets.token_urlsafe(24)
    _PREVIEW_TOKENS[token] = {"rows": unique, "created_at": _now().timestamp()}

    return CSVPreviewResponse(
        token=token,
        total=len(rows) + duplicates,
        valid=len(unique),
        invalid=len(errors),
        duplicates=duplicates,
        accounts=[u for u, _ in unique][:500],
        errors=errors[:20],
    )


# =============================================================
# Start batch
# =============================================================
@router.post("/csv/start", response_model=BulkBatchResponse)
async def csv_start(payload: BulkBatchCreateRequest, db: Session = Depends(get_db)):
    slot = _PREVIEW_TOKENS.pop(payload.token, None)
    if not slot:
        raise HTTPException(status_code=400, detail="Preview token expired. Re-upload the CSV.")
    rows: list[tuple[str, str]] = slot["rows"]

    settings = worker.get_bulk_settings()
    concurrency = payload.concurrency or settings["concurrency"]
    delay = payload.delay_between_accounts if payload.delay_between_accounts is not None else settings["delay_between_accounts"]
    concurrency = max(settings["min_concurrency"], min(settings["max_concurrency"], concurrency))
    delay = max(settings["min_delay"], min(settings["max_delay"], delay))

    batch = BulkBatch(
        batch_name=(payload.batch_name or f"Batch {int(_now().timestamp())}").strip(),
        status="pending",
        total_accounts=len(rows),
        remaining_accounts=len(rows),
        concurrency=concurrency,
        delay_between_accounts=delay,
        created_by="operator",
    )
    db.add(batch)
    db.flush()

    items: list[tuple[int, str, str]] = []
    for u, p in rows:
        profile_path = str((worker.PROFILES_DIR / u).resolve())
        item = BulkBatchItem(
            batch_id=batch.id,
            username=u,
            status="pending",
            profile_path=profile_path,
            message="Queued",
        )
        db.add(item)
        db.flush()
        items.append((item.id, u, p))
        # Ensure an Account row exists so the pool integrates with the rest of the app
        existing = db.query(Account).filter(Account.account_name == u).first()
        if not existing:
            db.add(Account(account_name=u, profile_path=profile_path,
                           session_status="unknown", account_status="enabled"))

    log(db, "INFO", f"Bulk batch #{batch.id} started with {len(rows)} accounts (concurrency={concurrency}, delay={delay}s)")
    db.commit()
    db.refresh(batch)

    # ---- Capture the running FastAPI loop HERE (inside the request handler)
    # ---- and pass it into the worker. This is the loop uvicorn/FastAPI is
    # ---- serving on; Playwright must run on THIS loop.
    running_loop = asyncio.get_running_loop()
    worker.register_batch(batch.id, items, concurrency, loop=running_loop)
    # Awaited (async) — matches browser_session.connect_account pattern so
    # the task is created from within an active coroutine on the FastAPI loop.
    if not await worker.start_batch(batch.id):
        log(db, "WARNING", f"Bulk batch #{batch.id} could not start (playwright missing?)")

    # Passwords are no longer referenced anywhere except worker memory now
    rows.clear()
    return BulkBatchResponse.model_validate(batch)


# =============================================================
# Batch listing / detail
# =============================================================
@router.get("/batches", response_model=List[BulkBatchResponse])
async def list_batches(db: Session = Depends(get_db)):
    return db.query(BulkBatch).order_by(BulkBatch.created_at.desc()).all()


@router.get("/batches/{batch_id}", response_model=BulkBatchDetail)
async def get_batch_detail(
    batch_id: int,
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    batch = db.query(BulkBatch).filter(BulkBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    items_q = db.query(BulkBatchItem).filter(BulkBatchItem.batch_id == batch_id)
    if status:
        items_q = items_q.filter(BulkBatchItem.status == status)
    if q:
        items_q = items_q.filter(BulkBatchItem.username.ilike(f"%{q}%"))
    items = items_q.order_by(BulkBatchItem.id.asc()).all()
    data = BulkBatchResponse.model_validate(batch).model_dump()
    return BulkBatchDetail(**data, items=[BulkBatchItemResponse.model_validate(i) for i in items])


# =============================================================
# Server-Sent Events (live status)
# =============================================================
@router.get("/batches/{batch_id}/stream")
async def stream_batch(batch_id: int):
    q = worker.subscribe(batch_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Batch not running")

    async def generator():
        try:
            # Initial ping so the client knows the stream is up
            yield f"event: hello\ndata: {json.dumps({'batch_id': batch_id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"event: update\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # keepalive
                    yield ": keepalive\n\n"
        finally:
            worker.unsubscribe(batch_id, q)

    return StreamingResponse(generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# =============================================================
# Per-item actions
# =============================================================
@router.post("/batches/{batch_id}/items/{item_id}/otp", response_model=SimpleOKResponse)
async def submit_otp(batch_id: int, item_id: int, payload: OTPSubmitRequest):
    code = payload.otp.strip()
    if not code.isdigit():
        raise HTTPException(status_code=400, detail="OTP must be numeric")
    ok = await worker.submit_otp(batch_id, item_id, code)
    if not ok:
        raise HTTPException(status_code=409, detail="Item is not waiting for OTP")
    return SimpleOKResponse(ok=True, message="OTP submitted")


class _RetryReq(OTPSubmitRequest):
    # Reuses BaseModel structure — but retry requires the password again since
    # we do NOT store passwords server-side after the initial batch start.
    otp: str = ""
    password: str


@router.post("/batches/{batch_id}/items/{item_id}/retry", response_model=SimpleOKResponse)
async def retry_item(batch_id: int, item_id: int, payload: dict, db: Session = Depends(get_db)):
    password = (payload or {}).get("password") or ""
    if not password:
        raise HTTPException(status_code=400, detail="Password required to retry (it is never stored server-side)")
    item = db.query(BulkBatchItem).filter(
        BulkBatchItem.id == item_id, BulkBatchItem.batch_id == batch_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    ok = await worker.retry_item(batch_id, item_id, password)
    if not ok:
        raise HTTPException(status_code=409, detail="Batch is not active. Restart the batch.")
    return SimpleOKResponse(ok=True, message="Retry started")


# =============================================================
# Batch controls
# =============================================================
@router.post("/batches/{batch_id}/pause", response_model=SimpleOKResponse)
async def pause_batch(batch_id: int, db: Session = Depends(get_db)):
    if not worker.pause_batch(batch_id):
        raise HTTPException(status_code=404, detail="Batch not running")
    batch = db.query(BulkBatch).filter(BulkBatch.id == batch_id).first()
    if batch:
        batch.status = "paused"
        db.commit()
    return SimpleOKResponse(ok=True, message="Paused")


@router.post("/batches/{batch_id}/resume", response_model=SimpleOKResponse)
async def resume_batch(batch_id: int, db: Session = Depends(get_db)):
    if not worker.resume_batch(batch_id):
        raise HTTPException(status_code=404, detail="Batch not running")
    batch = db.query(BulkBatch).filter(BulkBatch.id == batch_id).first()
    if batch:
        batch.status = "running"
        db.commit()
    return SimpleOKResponse(ok=True, message="Resumed")


@router.post("/batches/{batch_id}/cancel", response_model=SimpleOKResponse)
async def cancel_batch(batch_id: int, db: Session = Depends(get_db)):
    if not worker.cancel_batch(batch_id):
        raise HTTPException(status_code=404, detail="Batch not running")
    batch = db.query(BulkBatch).filter(BulkBatch.id == batch_id).first()
    if batch:
        batch.status = "cancelled"
        batch.finished_at = _now()
        db.commit()
    return SimpleOKResponse(ok=True, message="Cancelled")


# =============================================================
# Settings
# =============================================================
def _get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


@router.get("/settings", response_model=BulkSettingsResponse)
async def get_settings(db: Session = Depends(get_db)):
    return BulkSettingsResponse(**worker.get_bulk_settings())


@router.post("/settings", response_model=BulkSettingsResponse)
async def update_settings(payload: BulkSettingsUpdateRequest, db: Session = Depends(get_db)):
    changes = payload.model_dump(exclude_none=True)
    key_map = {
        "concurrency": "bulk_connect_concurrency",
        "delay_between_accounts": "bulk_connect_delay_seconds",
        "headless": "bulk_connect_headless",
        "auto_retry": "bulk_connect_auto_retry",
        "browser_timeout": "bulk_connect_browser_timeout",
    }
    for k, v in changes.items():
        if k not in key_map:
            continue
        val = "true" if (k == "headless" and v) else ("false" if k == "headless" else str(v))
        _set_setting(db, key_map[k], val)
    log(db, "INFO", f"Bulk Connect settings updated: {', '.join(changes.keys())}")
    db.commit()
    return BulkSettingsResponse(**worker.get_bulk_settings())


# =============================================================
# Batch results export + logs zip
# =============================================================
@router.get("/batches/{batch_id}/export.csv")
async def export_batch_csv(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(BulkBatch).filter(BulkBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    items = db.query(BulkBatchItem).filter(BulkBatchItem.batch_id == batch_id)\
        .order_by(BulkBatchItem.id.asc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["username", "status", "message", "error", "started_at", "finished_at", "retry_count", "profile_path"])
    for i in items:
        w.writerow([
            i.username, i.status, i.message, i.last_error,
            i.started_at.isoformat() if i.started_at else "",
            i.finished_at.isoformat() if i.finished_at else "",
            i.retry_count, i.profile_path,
        ])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="batch_{batch_id}.csv"'})


@router.get("/logs/{batch_id}")
async def download_batch_logs(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(BulkBatch).filter(BulkBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    items = db.query(BulkBatchItem).filter(BulkBatchItem.batch_id == batch_id).all()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in items:
            if i.debug_dir and Path(i.debug_dir).is_dir():
                for f in Path(i.debug_dir).glob("*"):
                    zf.write(f, arcname=f"{i.username}/{f.name}")
        zf.writestr("summary.json", json.dumps({
            "batch_id": batch.id, "batch_name": batch.batch_name,
            "total": batch.total_accounts, "connected": batch.connected_accounts,
            "failed": batch.failed_accounts, "otp": batch.otp_required_accounts,
            "checkpoint": batch.checkpoint_accounts,
        }, indent=2))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="batch_{batch_id}_logs.zip"',
    })


@router.get("/environment")
async def environment():
    return worker.environment_status()
