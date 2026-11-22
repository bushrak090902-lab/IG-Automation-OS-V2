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
}


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
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
