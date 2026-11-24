"""SQLite database configuration for IG Automation OS."""
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "data"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "ig_automation.db"

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DEFAULT_SETTINGS = {
    # existing publishing settings
    "concurrent_workers": "2",
    "delay_between_jobs_seconds": "30",
    "max_retries": "3",
    "browser_headless": "true",
    "browser_timeout_seconds": "60",
    "profiles_dir": str(BASE_DIR / "profiles"),
    "uploads_dir": str(BASE_DIR / "uploads"),
    "logs_dir": str(BASE_DIR / "logs"),
    # Bulk Connect (isolated from publishing)
    "bulk_connect_concurrency": "3",
    "bulk_connect_delay_seconds": "10",
    "bulk_connect_headless": "false",
    "bulk_connect_auto_retry": "2",
    "bulk_connect_browser_timeout": "60000",
    "bulk_connect_min_concurrency": "1",
    "bulk_connect_max_concurrency": "10",
    "bulk_connect_min_delay": "5",
    "bulk_connect_max_delay": "120",
    # Instagram Services diagnostics worker (fully isolated from publishing).
    # Older keys svc_diag_concurrency / svc_diag_headless are no longer read.
    "svc_diag_workers": "10",
    "svc_diag_show_browser": "false",  # background (headless) unless turned on for debugging
    "svc_diag_browser_timeout_ms": "60000",
    "svc_diag_job_timeout_seconds": "180",
    "svc_diag_max_retries": "2",
    "svc_diag_retry_backoff_seconds": "300,1200",
    "svc_diag_account_rest_min_seconds": "120",
    "svc_diag_account_rest_max_seconds": "300",
    "svc_diag_dwell_min_seconds": "3",
    "svc_diag_dwell_max_seconds": "8",
    "svc_diag_restriction_cooldown_minutes": "1440",
    "svc_diag_session_cooldown_minutes": "360",
    "svc_diag_pause_threshold": "3",
    "svc_diag_pause_window_minutes": "15",
    "svc_diag_pause_minutes": "30",
    "svc_diag_followup_minutes": "60",
}


def _migrate_services_jobs():
    """Additive, idempotent: add new columns to the Instagram Services job
    table if it was created by an earlier version. No data is changed."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "instagram_service_jobs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("instagram_service_jobs")}
    with engine.begin() as conn:
        if "kind" not in cols:
            conn.execute(text("ALTER TABLE instagram_service_jobs ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'diagnostic'"))
        if "not_before" not in cols:
            conn.execute(text("ALTER TABLE instagram_service_jobs ADD COLUMN not_before DATETIME"))


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate_services_jobs()
    from .models import Setting
    db = SessionLocal()
    try:
        # Idempotent seed — never overwrite operator-modified values
        for key, value in DEFAULT_SETTINGS.items():
            if not db.query(Setting).filter(Setting.key == key).first():
                db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()
