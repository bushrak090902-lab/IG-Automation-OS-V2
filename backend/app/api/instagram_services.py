"""Manual-only internal Instagram service request tracking."""
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session
from sqlalchemy import func

from ..database import get_db
from ..models import Account, InstagramServiceOrder as Order, InstagramServiceOrderLog as OrderLog

router = APIRouter(prefix="/v1/instagram-services", tags=["instagram-services"])
SERVICES = ("followers", "views", "likes", "comments", "shares", "reposts")
STATUSES = ("pending", "processing", "completed", "partially_completed", "failed", "cancelled")


def validate_url(value):
    value = value.strip()
    try:
        u = urlparse(value)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or host not in ("instagram.com", "www.instagram.com") or not u.path.strip("/"):
            raise ValueError()
        if u.username or u.password or u.port:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Enter a valid https://www.instagram.com/ URL")
    return value


class CreateOrder(BaseModel):
    service_type: Literal["followers", "views", "likes", "comments", "shares", "reposts"]
    instagram_url: str = Field(..., max_length=1000)
    requested_quantity: int = Field(..., ge=1, le=1000000)
    notes: str = Field("", max_length=2000)

    @validator("instagram_url")
    def valid_url(cls, v):
        return validate_url(v)


class UpdateOrder(BaseModel):
    status: Literal["pending", "processing", "completed", "partially_completed", "failed", "cancelled"]
    completed_quantity: int = Field(..., ge=0)
    notes: str = Field("", max_length=2000)


def as_dict(o):
    return {k: getattr(o, k).isoformat() if isinstance(getattr(o, k), datetime) else getattr(o, k)
            for k in ("id", "service_type", "instagram_url", "requested_quantity", "completed_quantity", "status", "execution_mode", "notes", "created_at", "updated_at", "completed_at")}


def log(db, o, kind, msg):
    db.add(OrderLog(order_id=o.id, event_type=kind, message=msg))


@router.get("/services")
def services():
    return [{"id": s, "name": s.title(), "execution_mode": "manual"} for s in SERVICES]


@router.get("/accounts/availability")
def availability(db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.id).all()
    return {"total": len(accounts), "available": sum(a.session_status == "connected" and a.account_status == "enabled" for a in accounts),
            "accounts": [{"id": a.id, "name": a.account_name, "session_status": a.session_status, "account_status": a.account_status} for a in accounts]}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    counts = dict(db.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
    return {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in STATUSES}}


@router.post("/orders", status_code=201)
def create_order(payload: CreateOrder, db: Session = Depends(get_db)):
    path = urlparse(payload.instagram_url).path.strip("/").split("/")
    is_post = path[0] in ("p", "reel", "tv") and len(path) >= 2
    if payload.service_type == "followers" and (is_post or path[0] in ("stories", "explore")):
        raise HTTPException(422, "Followers requires an Instagram profile URL")
    if payload.service_type != "followers" and not is_post:
        raise HTTPException(422, "This service requires an Instagram post or reel URL")
    o = Order(**payload.dict(), status="pending", completed_quantity=0, execution_mode="manual")
    db.add(o)
    db.flush()
    log(db, o, "created", "Manual workflow request created; no Instagram actions executed")
    db.commit()
    db.refresh(o)
    return as_dict(o)



@router.post("/test-orders", status_code=201)
def create_test_order(payload: CreateOrder, db: Session = Depends(get_db)):
    path = urlparse(payload.instagram_url).path.strip("/").split("/")
    is_post = path[0] in ("p", "reel", "tv") and len(path) >= 2

    if payload.service_type == "followers" and (
        is_post or path[0] in ("stories", "explore")
    ):
        raise HTTPException(422, "Followers requires a profile URL")

    if payload.service_type != "followers" and not is_post:
        raise HTTPException(422, "Requires a post or reel URL")

    order = Order(
        **payload.dict(),
        status="pending",
        completed_quantity=0,
        execution_mode="test",
    )
    db.add(order)
    db.flush()
    log(db, order, "test_created",
        "Simulation-only order; no Instagram actions")
    db.commit()
    db.refresh(order)
    return as_dict(order)


@router.get("/orders")
def list_orders(status: str = Query(None), service: str = Query(None), db: Session = Depends(get_db)):
    q = db.query(Order)
    if status:
        if status not in STATUSES: raise HTTPException(422, "Invalid status")
        q = q.filter(Order.status == status)
    if service:
        if service not in SERVICES: raise HTTPException(422, "Invalid service")
        q = q.filter(Order.service_type == service)
    return [as_dict(o) for o in q.order_by(Order.id.desc()).limit(500).all()]


@router.get("/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o: raise HTTPException(404, "Order not found")
    return as_dict(o)


@router.get("/orders/{order_id}/logs")
def get_logs(order_id: int, db: Session = Depends(get_db)):
    if not db.get(Order, order_id): raise HTTPException(404, "Order not found")
    return [{"id": x.id, "event_type": x.event_type, "message": x.message, "created_at": x.created_at.isoformat()} for x in db.query(OrderLog).filter(OrderLog.order_id == order_id).order_by(OrderLog.id).all()]


@router.patch("/orders/{order_id}")
def update_order(order_id: int, payload: UpdateOrder, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o: raise HTTPException(404, "Order not found")
    if o.status in ("completed", "cancelled", "failed"):
        raise HTTPException(409, "Terminal order cannot be modified")
    if payload.completed_quantity > o.requested_quantity:
        raise HTTPException(422, "Completed quantity cannot exceed requested quantity")
    if payload.status == "completed" and payload.completed_quantity != o.requested_quantity:
        raise HTTPException(422, "Completed orders require full verified quantity")
    if payload.status == "partially_completed" and not 0 < payload.completed_quantity < o.requested_quantity:
        raise HTTPException(422, "Partial completion requires a partial verified quantity")
    if payload.status == "pending" and o.status != "pending":
        raise HTTPException(409, "Cannot revert order to pending")
    if payload.status == "cancelled" and payload.completed_quantity:
        raise HTTPException(422, "Use partially_completed for requests with recorded progress")
    o.status = payload.status
    o.completed_quantity = payload.completed_quantity
    o.notes = payload.notes
    if o.status in ("completed", "partially_completed", "failed", "cancelled"):
        o.completed_at = datetime.now(timezone.utc)
    log(db, o, "updated", "Manual workflow status updated to " + o.status)
    db.commit()
    db.refresh(o)
    return as_dict(o)


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o: raise HTTPException(404, "Order not found")
    if o.status != "pending" or o.completed_quantity:
        raise HTTPException(409, "Only pending orders without progress can be cancelled")
    o.status = "cancelled"
    o.completed_at = datetime.now(timezone.utc)
    log(db, o, "cancelled", "Manual workflow request cancelled")
    db.commit()
    return as_dict(o)
