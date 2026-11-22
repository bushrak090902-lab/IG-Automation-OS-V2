"""Publishing queue REST API \u2014 mounted under /api/v1/queue."""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Account, Campaign, Job
from ..queue import queue_manager as qm
from ..queue.worker_pool import pool

router = APIRouter(prefix="/v1/queue", tags=["queue"])


@router.get("/stats")
async def stats(db: Session = Depends(get_db)):
    return qm.queue_stats(db)


@router.get("/workers")
async def workers():
    return pool.status()


@router.get("/jobs")
async def list_jobs(
    db: Session = Depends(get_db),
    status: Optional[str] = Query(default=None),
    campaign_id: Optional[int] = Query(default=None),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
):
    query = db.query(Job)
    if status:
        query = query.filter(Job.status == status)
    if campaign_id is not None:
        query = query.filter(Job.campaign_id == campaign_id)
    total = query.count()
    rows = query.order_by(Job.id.desc()).offset(offset).limit(limit).all()

    # Resolve account names in one pass
    acc_ids = {r.account_id for r in rows if r.account_id}
    accounts = {a.id: a.account_name for a in db.query(Account).filter(Account.id.in_(acc_ids or {-1})).all()}
    camp_ids = {r.campaign_id for r in rows}
    camps = {c.id: c.content_type for c in db.query(Campaign).filter(Campaign.id.in_(camp_ids or {-1})).all()}

    items = []
    for r in rows:
        name = accounts.get(r.account_id, "\u2014") if r.account_id else "\u2014"
        if q and q.lower() not in name.lower():
            continue
        items.append({
            "id": r.id,
            "campaign_id": r.campaign_id,
            "content_type": camps.get(r.campaign_id, ""),
            "account_id": r.account_id,
            "account_name": name,
            "file_id": r.file_id,
            "status": r.status,
            "attempt_count": r.attempt_count,
            "error_message": r.error_message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        })
    return {"total": total, "items": items}


@router.post("/retry-failed")
async def retry_failed(campaign_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Job).filter(Job.status == "failed")
    if campaign_id is not None:
        query = query.filter(Job.campaign_id == campaign_id)
    # Re-open the campaigns of these jobs so workers pick them up again.
    affected_campaigns = {j.campaign_id for j in query.all()}
    updated = query.update({"status": "queued", "error_message": "", "attempt_count": 0},
                           synchronize_session=False)
    for cid in affected_campaigns:
        c = db.query(Campaign).filter(Campaign.id == cid).first()
        if c and c.status in ("completed", "stopped", "paused"):
            c.status = "running"
    db.commit()
    pool.notify()
    return {"requeued": updated}
