"""Pydantic v2 schemas for API I/O.

Bulk Connect schemas NEVER expose passwords or OTP values in responses.
"""
from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict


# ---------------- Accounts / Campaigns (existing) ----------------
class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_name: str
    profile_path: str
    session_status: str
    account_status: str
    last_used_at: Optional[datetime] = None
    last_successful_post_at: Optional[datetime] = None
    created_at: datetime


class AccountCreate(BaseModel):
    account_name: str = Field(min_length=1, max_length=255)


class AccountUpdate(BaseModel):
    account_status: Optional[str] = None
    session_status: Optional[str] = None


class CampaignFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    file_type: str
    size_bytes: int
    order_index: int


class CampaignCreate(BaseModel):
    content_type: str
    caption: str = ""
    account_quantity: int = Field(gt=0, le=10000)
    distribution_mode: str
    audio_mode: str = "original"
    music_search: str = ""
    file_ids: List[int] = []


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    content_type: str
    caption: str
    account_quantity: int
    distribution_mode: str
    audio_mode: str
    music_search: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class CampaignDetail(CampaignOut):
    files: List[CampaignFileOut] = []
    job_counts: dict = {}


class SettingsUpdate(BaseModel):
    concurrent_workers: Optional[str] = None
    delay_between_jobs_seconds: Optional[str] = None
    max_retries: Optional[str] = None
    browser_headless: Optional[str] = None
    browser_timeout_seconds: Optional[str] = None
    profiles_dir: Optional[str] = None
    uploads_dir: Optional[str] = None
    logs_dir: Optional[str] = None


class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    level: str
    message: str
    campaign_id: Optional[int] = None
    account_id: Optional[int] = None
    created_at: datetime


# ---------------- Bulk Connect ----------------
BulkItemStatus = Literal[
    "pending", "logging_in", "connected", "otp_required",
    "checkpoint_required", "retrying", "failed", "completed",
]
BulkBatchStatus = Literal[
    "pending", "running", "paused", "completed", "cancelled", "failed",
]


class CSVAccountRow(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)  # request-only


class CSVPreviewResponse(BaseModel):
    token: str
    total: int
    valid: int
    invalid: int
    duplicates: int
    accounts: List[str]  # usernames only; passwords never echoed back
    errors: List[str] = []


class BulkBatchCreateRequest(BaseModel):
    token: str
    batch_name: str = ""
    concurrency: Optional[int] = Field(default=None, ge=1, le=10)
    delay_between_accounts: Optional[int] = Field(default=None, ge=5, le=120)


class BulkBatchItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_id: int
    username: str
    status: str
    message: str
    profile_path: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_count: int
    last_error: str
    last_url: str
    checkpoint: str
    otp_required: int
    debug_dir: str
    created_at: datetime


class BulkBatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_name: str
    status: str
    total_accounts: int
    connected_accounts: int
    failed_accounts: int
    otp_required_accounts: int
    checkpoint_accounts: int
    retrying_accounts: int
    remaining_accounts: int
    concurrency: int
    delay_between_accounts: int
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class BulkBatchDetail(BulkBatchResponse):
    items: List[BulkBatchItemResponse] = []


class OTPSubmitRequest(BaseModel):
    otp: str = Field(min_length=4, max_length=12)


class SimpleOKResponse(BaseModel):
    ok: bool = True
    message: str = ""


class BulkSettingsResponse(BaseModel):
    concurrency: int
    delay_between_accounts: int
    headless: bool
    auto_retry: int
    browser_timeout: int
    min_concurrency: int
    max_concurrency: int
    min_delay: int
    max_delay: int


class BulkSettingsUpdateRequest(BaseModel):
    concurrency: Optional[int] = Field(default=None, ge=1, le=10)
    delay_between_accounts: Optional[int] = Field(default=None, ge=5, le=120)
    headless: Optional[bool] = None
    auto_retry: Optional[int] = Field(default=None, ge=0, le=5)
    browser_timeout: Optional[int] = Field(default=None, ge=10000, le=180000)
