"""Queue manager — job materialisation, account claiming and stats.

Single responsibility: translate campaigns into ``Job`` rows and provide the
atomic primitives the worker pool needs to distribute work fairly across a
large account pool WITHOUT two workers ever touching the same account.

All functions take an explicit SQLAlchemy ``Session`` so they can be called
from request handlers or from background worker tasks (each of which owns its
own session).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Account, Campaign, CampaignFile, Job
from ..services.distribution import plan_pairs
from ..services.logger import log

# Statuses considered "active" for a campaign the workers should service.
RUNNABLE_CAMPAIGN_STATUSES = ("running",)
TERMINAL_JOB_STATUSES = ("successful", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def select_eligible_accounts(db: Session, quantity: int) -> List[Account]:
    """Pick up to ``quantity`` enabled accounts for a campaign.

    Connected sessions are preferred (they can actually publish); within each
    group the least-recently-used account is chosen first so load spreads
    evenly across the pool at scale.
    """
    connected = (
        db.query(Account)
        .filter(Account.account_status == "enabled", Account.session_status == "connected")
        .order_by(Account.last_used_at.asc().nullsfirst(), Account.id.asc())
        .limit(quantity)
        .all()
    )
    if len(connected) >= quantity:
        return connected
    # Top up with other enabled accounts (publisher will fail-fast if not authed).
    remaining = quantity - len(connected)
    chosen_ids = {a.id for a in connected}
    others = (
        db.query(Account)
        .filter(Account.account_status == "enabled", ~Account.id.in_(chosen_ids or {-1}))
        .order_by(Account.last_used_at.asc().nullsfirst(), Account.id.asc())
        .limit(remaining)
        .all()
    )
    return connected + others


def materialize_jobs(db: Session, campaign: Campaign) -> int:
    """Create ``Job`` rows for a campaign based on its distribution mode.

    Returns the number of jobs created. Raises ValueError with a human message
    when the campaign cannot be started.
    """
    files = (
        db.query(CampaignFile)
        .filter(CampaignFile.campaign_id == campaign.id)
        .order_by(CampaignFile.order_index.asc())
        .all()
    )
    if not files:
        raise ValueError("Campaign has no files to publish.")

    accounts = select_eligible_accounts(db, campaign.account_quantity)
    if not accounts:
        raise ValueError("No enabled accounts available. Add and connect accounts first.")

    file_ids = [f.id for f in files]
    account_ids = [a.id for a in accounts]
    pairs = list(plan_pairs(campaign.distribution_mode, file_ids, account_ids))
    if not pairs:
        raise ValueError("Distribution produced no jobs — check files and accounts.")

    created = 0
    for account_id, file_id in pairs:
        db.add(Job(
            campaign_id=campaign.id,
            account_id=account_id,
            file_id=file_id,
            status="queued",
            attempt_count=0,
            error_message="",
        ))
        created += 1

    campaign.status = "running"
    campaign.started_at = _now()
    log(db, "INFO",
        f"Campaign #{campaign.id} started — {created} job(s) queued across {len(account_ids)} account(s)",
        campaign_id=campaign.id)
    db.commit()
    return created


def claim_account(db: Session, excluded: set[int]) -> Optional[int]:
    """Return an account_id that has queued jobs in a running campaign and is
    not already claimed by another worker. Atomic enough for our single-process
    worker pool because each worker adds to ``excluded`` under a shared lock
    before releasing the DB read.
    """
    q = (
        db.query(Job.account_id)
        .join(Campaign, Campaign.id == Job.campaign_id)
        .filter(
            Job.status == "queued",
            Job.account_id.isnot(None),
            Campaign.status.in_(RUNNABLE_CAMPAIGN_STATUSES),
        )
    )
    if excluded:
        q = q.filter(~Job.account_id.in_(excluded))
    row = q.order_by(Job.account_id.asc()).first()
    return row[0] if row else None


def next_job_for_account(db: Session, account_id: int) -> Optional[Job]:
    """Next queued job for an account whose campaign is still running."""
    return (
        db.query(Job)
        .join(Campaign, Campaign.id == Job.campaign_id)
        .filter(
            Job.account_id == account_id,
            Job.status == "queued",
            Campaign.status.in_(RUNNABLE_CAMPAIGN_STATUSES),
        )
        .order_by(Job.id.asc())
        .first()
    )


def finalize_campaign_if_done(db: Session, campaign_id: int) -> None:
    """Mark a campaign completed once no queued/processing jobs remain."""
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign or campaign.status not in RUNNABLE_CAMPAIGN_STATUSES:
        return
    remaining = (
        db.query(func.count(Job.id))
        .filter(Job.campaign_id == campaign_id, Job.status.in_(("queued", "processing")))
        .scalar()
    ) or 0
    if remaining == 0:
        campaign.status = "completed"
        campaign.completed_at = _now()
        log(db, "SUCCESS", f"Campaign #{campaign_id} completed", campaign_id=campaign_id)
        db.commit()


def queue_stats(db: Session) -> dict:
    rows = db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    counts = {"queued": 0, "processing": 0, "successful": 0, "failed": 0}
    for status, n in rows:
        counts[status] = counts.get(status, 0) + n
    counts["total"] = sum(counts.values())
    active_campaigns = (
        db.query(func.count(Campaign.id))
        .filter(Campaign.status == "running").scalar()
    ) or 0
    counts["active_campaigns"] = active_campaigns
    return counts
