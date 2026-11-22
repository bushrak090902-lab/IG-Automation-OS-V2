"""Bulk Username Mapping — ISOLATED feature.

Renames ONLY ``Account.account_name`` for existing account IDs from a CSV.
It never creates/deletes accounts, never touches profile_path, browser
profiles, cookies, sessions, campaigns, jobs, statuses or IDs.

Endpoints (all new, mounted under /api/v1/username-mapping):
  GET  /template         -> CSV template with every existing ID, blank usernames
  POST /preview          -> validate a CSV, return per-row status + a token
  POST /import           -> apply validated updates in ONE transaction

Nothing here imports or calls publishing / queue / bulk_connect / Playwright.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
import time
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import Account
from ..services.logger import log
from .accounts import PROFILES_DIR

router = APIRouter(prefix="/v1/username-mapping", tags=["username-mapping"])

# Instagram usernames: letters, numbers, dot, underscore, up to 30 chars.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
ID_RE = re.compile(r"^#?\s*(\d+)\s*$")

# Preview tokens hold validated updates in memory until import (short-lived).
_PREVIEW_TOKENS: dict[str, dict] = {}
_TOKEN_TTL = 1800  # seconds


def _now() -> float:
    return time.time()


def _gc_tokens() -> None:
    dead = [t for t, v in _PREVIEW_TOKENS.items() if _now() - v["created_at"] > _TOKEN_TTL]
    for t in dead:
        _PREVIEW_TOKENS.pop(t, None)


def _parse_id(raw: str) -> Optional[int]:
    if raw is None:
        return None
    m = ID_RE.match(str(raw).strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _parse_csv(text: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return (rows, parse_errors). Rows are raw (id_str, username) tuples."""
    rows: List[Tuple[str, str]] = []
    errors: List[str] = []
    reader = csv.reader(io.StringIO(text))
    header_seen = False
    for i, raw in enumerate(reader, start=1):
        if not raw or all(not (c or "").strip() for c in raw):
            continue
        if not header_seen:
            header_seen = True
            lower = [c.strip().lower() for c in raw]
            if "id" in lower and "username" in lower:
                continue  # skip header row
        id_str = (raw[0] if len(raw) > 0 else "").strip()
        username = (raw[1] if len(raw) > 1 else "").strip()
        rows.append((id_str, username))
    return rows, errors


# ---------------------------------------------------------------------------
# GET /template
# ---------------------------------------------------------------------------
@router.get("/template")
async def download_template(db: Session = Depends(get_db)):
    ids = [row[0] for row in db.query(Account.id).order_by(Account.id.asc()).all()]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Username"])
    for _id in ids:
        w.writerow([f"#{_id}", ""])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="username_mapping_template.csv"'},
    )


# ---------------------------------------------------------------------------
# POST /preview
# ---------------------------------------------------------------------------
class PreviewRow(BaseModel):
    id: Optional[int] = None
    raw_id: str = ""
    old_username: str = ""
    new_username: str = ""
    status: str  # will_update | will_create | duplicate | invalid_id | missing_username | unchanged


class PreviewSummary(BaseModel):
    total_rows: int = 0
    updated: int = 0        # will_update count
    created: int = 0        # will_create count (new accounts)
    skipped: int = 0
    invalid_ids: int = 0
    duplicate_usernames: int = 0
    missing_usernames: int = 0
    unchanged: int = 0
    errors: int = 0


class PreviewResponse(BaseModel):
    token: str
    summary: PreviewSummary
    rows: List[PreviewRow]


def _build_preview(rows: List[Tuple[str, str]], db: Session) -> Tuple[List[PreviewRow], List[Tuple[int, str]]]:
    # Snapshot of the account pool.
    id_to_name: dict[int, str] = {a.id: a.account_name for a in db.query(Account.id, Account.account_name).all()}
    name_to_id: dict[str, int] = {name.lower(): _id for _id, name in id_to_name.items()}

    seen_new: dict[str, int] = {}  # lower(new_username) -> first row index
    preview: List[PreviewRow] = []
    updates: List[Tuple[int, str]] = []
    creates: List[Tuple[int, str]] = []  # (parsed_id, new_username) — created with the EXACT CSV id

    for (id_str, username) in rows:
        acc_id = _parse_id(id_str)
        old = id_to_name.get(acc_id, "") if acc_id is not None else ""

        # 1) ID validity — unparseable id is invalid; a valid id that does not
        #    exist means a NEW account will be created for it.
        if acc_id is None:
            preview.append(PreviewRow(id=None, raw_id=id_str, old_username=old,
                                      new_username=username, status="invalid_id"))
            continue
        if acc_id not in id_to_name:
            if not username or not USERNAME_RE.match(username):
                preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username="",
                                          new_username=username, status="missing_username"))
                continue
            low = username.lower()
            if low in seen_new or name_to_id.get(low) is not None:
                preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username="",
                                          new_username=username, status="duplicate"))
                continue
            seen_new[low] = acc_id
            creates.append((acc_id, username))
            preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username="",
                                      new_username=username, status="will_create"))
            continue
        # 2) Missing username
        if not username:
            preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username=old,
                                      new_username="", status="missing_username"))
            continue
        # invalid characters count as missing/invalid -> treat as invalid username (skip)
        if not USERNAME_RE.match(username):
            preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username=old,
                                      new_username=username, status="missing_username"))
            continue

        low = username.lower()
        # 3) Duplicate within CSV
        if low in seen_new:
            preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username=old,
                                      new_username=username, status="duplicate"))
            continue
        # 4) Duplicate against another account in DB
        owner = name_to_id.get(low)
        if owner is not None and owner != acc_id:
            preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username=old,
                                      new_username=username, status="duplicate"))
            continue
        # 5) Unchanged (already identical) — skip actual write
        if old == username:
            seen_new[low] = acc_id
            preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username=old,
                                      new_username=username, status="unchanged"))
            continue

        # Will update
        seen_new[low] = acc_id
        preview.append(PreviewRow(id=acc_id, raw_id=id_str, old_username=old,
                                  new_username=username, status="will_update"))
        updates.append((acc_id, username))

    return preview, updates, creates


def _summarize(preview: List[PreviewRow]) -> PreviewSummary:
    s = PreviewSummary(total_rows=len(preview))
    for r in preview:
        if r.status == "will_update":
            s.updated += 1
        elif r.status == "will_create":
            s.created += 1
        elif r.status == "invalid_id":
            s.invalid_ids += 1
            s.skipped += 1
        elif r.status == "duplicate":
            s.duplicate_usernames += 1
            s.skipped += 1
        elif r.status == "missing_username":
            s.missing_usernames += 1
            s.skipped += 1
        elif r.status == "unchanged":
            s.unchanged += 1
            s.skipped += 1
    return s


@router.post("/preview", response_model=PreviewResponse)
async def preview(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a .csv file")
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=400, detail=f"Unable to decode CSV: {e}") from e

    rows, _ = _parse_csv(text)
    if not rows:
        raise HTTPException(status_code=400, detail="No data rows found. Expected 'ID,Username'.")

    preview_rows, updates, creates = _build_preview(rows, db)
    summary = _summarize(preview_rows)

    _gc_tokens()
    token = secrets.token_urlsafe(24)
    _PREVIEW_TOKENS[token] = {"updates": updates, "creates": creates, "created_at": _now()}

    # Cap rows returned to the UI to keep payload light for very large files.
    return PreviewResponse(token=token, summary=summary, rows=preview_rows[:1000])


# ---------------------------------------------------------------------------
# POST /import
# ---------------------------------------------------------------------------
class ImportRequest(BaseModel):
    token: str


class ImportSummary(BaseModel):
    total_rows: int = 0
    updated: int = 0
    created: int = 0
    skipped: int = 0
    invalid_ids: int = 0
    duplicate_usernames: int = 0
    errors: int = 0


@router.post("/import", response_model=ImportSummary)
async def do_import(payload: ImportRequest):
    slot = _PREVIEW_TOKENS.pop(payload.token, None)
    if not slot:
        raise HTTPException(status_code=400, detail="Preview token expired. Re-upload the CSV.")
    updates: List[Tuple[int, str]] = slot["updates"]
    creates: List[Tuple[int, str]] = slot.get("creates", [])

    updated = 0
    created = 0
    errors = 0
    db = SessionLocal()
    try:
        # Re-validate uniqueness at write-time to be safe (DB may have changed).
        existing = {a.account_name.lower(): a.id
                    for a in db.query(Account.id, Account.account_name).all()}
        for acc_id, new_name in updates:
            low = new_name.lower()
            owner = existing.get(low)
            if owner is not None and owner != acc_id:
                errors += 1
                continue
            acc = db.query(Account).filter(Account.id == acc_id).first()
            if not acc:
                errors += 1
                continue
            if acc.account_name == new_name:
                continue  # identical — no write
            old_low = acc.account_name.lower()
            acc.account_name = new_name  # ONLY the display name changes
            existing.pop(old_low, None)
            existing[low] = acc_id
            updated += 1
        # Create NEW accounts using the EXACT CSV id (not auto-increment).
        # Profile folder mirrors Add Account: profiles/account_{id:04d}.
        existing_ids = set(existing.values())
        for acc_id, new_name in creates:
            low = new_name.lower()
            if acc_id in existing_ids or low in existing:
                errors += 1
                continue
            profile_path = PROFILES_DIR / f"account_{acc_id:04d}"
            profile_path.mkdir(parents=True, exist_ok=True)
            acc = Account(
                id=acc_id,
                account_name=new_name,
                profile_path=str(profile_path),
                session_status="unknown",
                account_status="enabled",
            )
            db.add(acc)
            db.flush()
            log(db, "INFO",
                f"Account '{new_name}' added — profile {profile_path.name}", account_id=acc_id)
            existing_ids.add(acc_id)
            existing[low] = acc_id
            created += 1
        db.commit()
        log(db, "INFO",
            f"Bulk Username Mapping: {updated} updated, {created} created, {errors} error(s)")
    except Exception as e:  # rollback on any unexpected error
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Import failed and was rolled back: {e}") from e
    finally:
        db.close()

    total = len(updates) + len(creates)
    return ImportSummary(
        total_rows=total,
        updated=updated,
        created=created,
        skipped=total - updated - created - errors,
        invalid_ids=0,
        duplicate_usernames=0,
        errors=errors,
    )
