"""SQLAlchemy models for IG Automation OS."""
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from .database import Base


def _now():
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(255), nullable=False, unique=True)
    profile_path = Column(String(500), nullable=False)
    session_status = Column(String(32), default="unknown", nullable=False)
    account_status = Column(String(32), default="enabled", nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    last_successful_post_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    content_type = Column(String(32), nullable=False)
    caption = Column(Text, default="", nullable=False)
    account_quantity = Column(Integer, nullable=False)
    distribution_mode = Column(String(32), nullable=False)
    audio_mode = Column(String(32), default="original", nullable=False)
    music_search = Column(String(500), default="", nullable=False)
    status = Column(String(32), default="draft", nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    files = relationship("CampaignFile", back_populates="campaign", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="campaign", cascade="all, delete-orphan")


class CampaignFile(Base):
    __tablename__ = "campaign_files"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(500), nullable=False)
    stored_path = Column(String(500), nullable=False)
    file_type = Column(String(64), nullable=False)
    size_bytes = Column(Integer, default=0, nullable=False)
    order_index = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)

    campaign = relationship("Campaign", back_populates="files")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    file_id = Column(Integer, ForeignKey("campaign_files.id"), nullable=True)
    status = Column(String(32), default="queued", nullable=False)
    attempt_count = Column(Integer, default=0, nullable=False)
    error_message = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    campaign = relationship("Campaign", back_populates="jobs")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(128), primary_key=True)
    value = Column(Text, default="", nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


class LogEntry(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String(16), nullable=False, default="INFO")
    message = Column(Text, nullable=False)
    campaign_id = Column(Integer, nullable=True)
    account_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)


# ---------------------------------------------------------------------------
# Bulk Connect — completely isolated from publishing.
# Never store passwords, OTP, cookies, or session tokens.
# ---------------------------------------------------------------------------
class BulkBatch(Base):
    __tablename__ = "bulk_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_name = Column(String(255), nullable=False, default="")
    # pending | running | paused | completed | cancelled | failed
    status = Column(String(32), default="pending", nullable=False, index=True)
    total_accounts = Column(Integer, default=0, nullable=False)
    connected_accounts = Column(Integer, default=0, nullable=False)
    failed_accounts = Column(Integer, default=0, nullable=False)
    otp_required_accounts = Column(Integer, default=0, nullable=False)
    checkpoint_accounts = Column(Integer, default=0, nullable=False)
    retrying_accounts = Column(Integer, default=0, nullable=False)
    remaining_accounts = Column(Integer, default=0, nullable=False)
    concurrency = Column(Integer, default=3, nullable=False)
    delay_between_accounts = Column(Integer, default=10, nullable=False)
    created_by = Column(String(255), default="operator", nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # future-ready free-form JSON metadata (proxy config, device fp, tags, notes)
    meta = Column(Text, default="{}", nullable=False)

    items = relationship("BulkBatchItem", back_populates="batch", cascade="all, delete-orphan")


class BulkBatchItem(Base):
    __tablename__ = "bulk_batch_items"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("bulk_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    username = Column(String(255), nullable=False, index=True)
    # pending | logging_in | connected | otp_required | checkpoint_required | retrying | failed | completed
    status = Column(String(32), default="pending", nullable=False, index=True)
    message = Column(Text, default="", nullable=False)
    profile_path = Column(String(500), default="", nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, default="", nullable=False)
    last_url = Column(String(1000), default="", nullable=False)
    checkpoint = Column(String(255), default="", nullable=False)
    otp_required = Column(Integer, default=0, nullable=False)  # 0/1 (SQLite friendly)
    debug_dir = Column(String(500), default="", nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False, index=True)
    # future-ready free-form JSON (device_fp, proxy, country, login history)
    meta = Column(Text, default="{}", nullable=False)

    batch = relationship("BulkBatch", back_populates="items")


Index("ix_bulk_batch_items_batch_status", BulkBatchItem.batch_id, BulkBatchItem.status)


# Internal service request tracking. No engagement automation is performed.
class InstagramServiceOrder(Base):
    __tablename__ = "instagram_service_orders"
    id = Column(Integer, primary_key=True)
    service_type = Column(String(32), nullable=False, index=True)
    instagram_url = Column(String(1000), nullable=False)
    requested_quantity = Column(Integer, nullable=False)
    completed_quantity = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="pending", index=True)
    execution_mode = Column(String(32), nullable=False, default="manual")
    notes = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class InstagramServiceOrderLog(Base):
    __tablename__ = "instagram_service_order_logs"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("instagram_service_orders.id"), nullable=False, index=True)
    event_type = Column(String(40), nullable=False)
    message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=_now, nullable=False)


# ---------------------------------------------------------------------------
# Diagnostics job queue — persistent SQLite queue for Instagram Services.
# One job row per diagnostic order; the isolated diagnostics worker pool
# (app/queue/instagram_services_worker.py) claims jobs from this table.
# Deliberately a separate table from the publishing `jobs` table so the two
# worker lanes can never interfere with each other.
# ---------------------------------------------------------------------------
class InstagramServiceJob(Base):
    __tablename__ = "instagram_service_jobs"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("instagram_service_orders.id"), nullable=False, index=True)
    # queued | assigned | running | succeeded | failed | cancelled
    status = Column(String(32), nullable=False, default="queued", index=True)
    worker_id = Column(String(64), nullable=False, default="")
    account_id = Column(Integer, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    error = Column(Text, nullable=False, default="")
    result = Column(Text, nullable=False, default="{}")  # JSON diagnostic result (no cookies/secrets)
    created_at = Column(DateTime, default=_now, nullable=False)
    assigned_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)


Index("ix_svc_job_order_status", InstagramServiceJob.order_id, InstagramServiceJob.status)
