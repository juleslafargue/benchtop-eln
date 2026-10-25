from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import audit, backup, diskspace, integrity, mailer, sitesettings, timestamps
from ..config import settings
from ..security import DB, Admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/backups")
def list_backups(_: Admin):
    return {"directory": str(settings.backup_dir), "keep": settings.backup_keep,
            "interval_hours": settings.backup_interval_hours, "backups": backup.list_backups(),
            **backup.health(), "suggestions": backup.suggested_copy_dirs(), "data_check": integrity.health(),
            "disk": diskspace.usage()}


class CopyDirIn(BaseModel):
    path: str | None = None


@router.put("/backups/copy-dir")
def set_backup_copy_dir(body: CopyDirIn, user: Admin, session: DB):
    """Where a second copy of every backup goes (iCloud Drive, an external drive…); empty to stop."""
    if "BENCHTOP_BACKUP_COPY_DIR" in os.environ:
        raise HTTPException(409, "The second-copy folder is set by BENCHTOP_BACKUP_COPY_DIR in this server's settings")
    try:
        backup.set_copy_dir((body.path or "").strip() or None)
    except backup.BackupError as exc:
        raise HTTPException(400, str(exc))
    audit.record(session, user, "backup_copy_dir", "system", None, path=body.path or "")
    session.commit()
    first = None
    newest = backup.list_backups()
    if body.path and newest:  # copy the newest one straight away, so there's a copy from today
        first = backup.copy_to_second_place(Path(newest[0]["path"]))
    return {**backup.health(), "first_copy": first}


@router.post("/backups/test")
def test_backup(user: Admin, session: DB):
    """Unpack the newest backup and check it as if it had been restored."""
    res = backup.test_latest_backup()
    if res is None:
        raise HTTPException(409, "There are no backups to test yet; make one first")
    audit.record(session, user, "backup_test", "system", None, name=res["name"], ok=res["ok"])
    session.commit()
    return res


@router.post("/check")
def check_now(user: Admin, session: DB):
    """Run the daily data check now (Admin → Backups), e.g. after restoring a file."""
    if not integrity.available():
        raise HTTPException(409, integrity.POSTGRES)
    res = integrity.run()
    audit.record(session, user, "data_check", "system", None, ok=res["ok"])
    session.commit()
    return integrity.health()


@router.post("/check/put-back-files")
def put_back_files(user: Admin, session: DB):
    """Copy missing or changed attached files back from the backups, then check again."""
    if not integrity.available():
        raise HTTPException(409, integrity.POSTGRES)
    done = integrity.put_back_files()
    audit.record(session, user, "put_back_files", "system", None, **done)
    session.commit()
    integrity.run()
    return {**integrity.health(), **done}


@router.post("/backups", status_code=201)
def create_backup(user: Admin, session: DB):
    try:
        b = backup.create_backup(f"manual by {user.username}")
    except backup.BackupError as exc:
        raise HTTPException(400, str(exc))
    audit.record(session, user, "backup", "system", None, name=b["name"], size=b["size"])
    session.commit()
    return b


# ------------------------------------------------------------------ email, witness reminders, trusted timestamps

class EmailSettingsIn(BaseModel):
    host: str | None = None
    port: int | None = None
    security: str | None = None
    username: str | None = None
    password: str | None = None  # None = keep the stored one
    from_address: str | None = None
    from_name: str | None = None
    base_url: str | None = None


@router.get("/email")
def get_email_settings(_: Admin, session: DB):
    from ..models import EmailLog
    from sqlmodel import select
    recent = session.exec(select(EmailLog).order_by(EmailLog.id.desc()).limit(25)).all()
    from sqlmodel import func
    waiting = session.exec(select(func.count()).select_from(EmailLog).where(EmailLog.status == "queued", EmailLog.error != "")).one()
    return {**sitesettings.public("email"), "configured": mailer.configured(), "link_base": mailer.base_url(), "waiting": waiting,
            "log": [{"to": e.to, "subject": e.subject, "status": "retrying" if e.status == "queued" and e.error else e.status,
                     "error": e.error, "next_try": e.next_try.isoformat() + "Z" if e.status == "queued" and e.next_try else None,
                     "at": (e.sent_at or e.created_at).isoformat() + "Z"} for e in recent]}


@router.put("/email")
def set_email_settings(body: EmailSettingsIn, user: Admin, session: DB):
    changes = body.model_dump(exclude_none=True)
    if body.password is not None:
        changes["password"] = body.password
    if "security" in changes and changes["security"] not in ("starttls", "ssl", "none"):
        raise HTTPException(422, "Security must be starttls, ssl or none")
    if "port" in changes and not 1 <= changes["port"] <= 65535:
        raise HTTPException(422, "Port must be between 1 and 65535")
    for k in ("host", "username", "from_address", "from_name", "base_url"):
        if k in changes:
            changes[k] = changes[k].strip()[:300]
            if any(ord(ch) < 32 for ch in changes[k]):
                raise HTTPException(422, "Settings can't contain line breaks or tabs")
    if changes.get("host") and any(ch in changes["host"] for ch in " /:@"):
        raise HTTPException(422, "The email server should be just its name, like smtp.gmail.com (put the port in Port)")
    if changes.get("from_address") and "@" not in changes["from_address"]:
        raise HTTPException(422, "The From address should be an email address")
    out = sitesettings.update("email", changes)
    audit.record(session, user, "settings_email", "system", None, host=out["host"], from_address=out["from_address"])
    session.commit()
    return {**out, "configured": mailer.configured(), "link_base": mailer.base_url()}


class TestEmailIn(BaseModel):
    to: str


@router.post("/email/test")
async def test_email(body: TestEmailIn, _: Admin):
    if "@" not in body.to:
        raise HTTPException(422, "Enter an email address to send the test to")
    err = await run_in_threadpool(mailer.send_test, body.to.strip())
    if err:
        raise HTTPException(400, err)
    return {"ok": True}


class WitnessSettingsIn(BaseModel):
    remind_after_days: int | None = None
    overdue_after_days: int | None = None


@router.get("/witness-settings")
def get_witness_settings(_: Admin):
    return sitesettings.public("witness")


@router.put("/witness-settings")
def set_witness_settings(body: WitnessSettingsIn, user: Admin, session: DB):
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        if not 1 <= v <= 90:
            raise HTTPException(422, "Use between 1 and 90 days")
    out = sitesettings.update("witness", changes)
    audit.record(session, user, "settings_witness", "system", None, **changes)
    session.commit()
    return out


class TimestampSettingsIn(BaseModel):
    enabled: bool | None = None
    tsa_url: str | None = None


def _timestamp_status(session) -> dict:
    from sqlmodel import func, select
    from ..models import SignatureTimestamp
    counts = dict(session.exec(select(SignatureTimestamp.status, func.count()).group_by(SignatureTimestamp.status)).all())
    last_err = session.exec(select(SignatureTimestamp.error).where(SignatureTimestamp.status != "ok", SignatureTimestamp.error != "")
                            .order_by(SignatureTimestamp.updated_at.desc())).first()
    return {**sitesettings.public("timestamps"), "available": timestamps.available(), "services": timestamps.TSAS,
            "counts": {"ok": counts.get("ok", 0), "pending": counts.get("pending", 0), "failed": counts.get("failed", 0)},
            "last_error": last_err or ""}


@router.get("/timestamps")
def get_timestamp_settings(_: Admin, session: DB):
    return _timestamp_status(session)


@router.put("/timestamps")
def set_timestamp_settings(body: TimestampSettingsIn, user: Admin, session: DB):
    changes = body.model_dump(exclude_none=True)
    if "tsa_url" in changes:
        url = changes["tsa_url"].strip()
        if not url.startswith(("http://", "https://")) or len(url) > 300:
            raise HTTPException(422, "The timestamp service address should start with http:// or https://")
        changes["tsa_url"] = url
    sitesettings.update("timestamps", changes)
    audit.record(session, user, "settings_timestamps", "system", None, **changes)
    session.commit()
    return _timestamp_status(session)


class TestTsaIn(BaseModel):
    tsa_url: str | None = None


@router.post("/timestamps/test")
async def test_timestamp_service(body: TestTsaIn, _: Admin):
    url = (body.tsa_url or sitesettings.get("timestamps")["tsa_url"]).strip()
    return await run_in_threadpool(timestamps.test_service, url)


@router.post("/timestamps/retry")
def retry_timestamps(_: Admin, session: DB):
    n = timestamps.retry_pending(include_failed=True)
    return {**_timestamp_status(session), "retried": n}
