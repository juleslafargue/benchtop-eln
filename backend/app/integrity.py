"""The daily check of the live data (0.24).

`./benchtop check` and the weekly backup test only run when asked, or on the newest backup. This checks the data
Benchtop is actually using, once a day, in the background: the database file, its version, the audit trail, that
signed records still match their signatures, trusted timestamps, and that every attached file is there and unchanged.
A failing disk, or a file changed or deleted outside Benchtop, shows up the next day instead of whenever someone looks.

- It reads a snapshot of the database (as `./benchtop check` does), so the server keeps running normally.
- Attached files are re-read byte for byte every day while they total up to FULL_CHECK_BYTES (20 GB: about a minute
  on a laptop SSD). Above that, every file is checked for being there with the right size every day, and a seventh of
  them are re-read each day, so each file is re-read once a week.
- What it finds is shown to admins on the dashboard and in Admin → Backups, saying what to do; admins with an email
  address get one email when a problem first appears (not every day).
- A check that found problems (or couldn't run) is tried again after RETRY_HOURS, not every hour.
- BENCHTOP_CHECK_INTERVAL_HOURS (default 24) sets how often; 0 turns it off.
- It needs Benchtop's standard database (SQLite), like `./benchtop check`; on Postgres it doesn't run, and says so.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("benchtop.check")
FULL_CHECK_BYTES = 20e9
RETRY_HOURS = 6
STATE = "data-check.json"  # kept outside the database, like backup-settings.json
POSTGRES = ("The daily data check needs Benchtop's standard database (SQLite), and this server uses Postgres, so it "
            "doesn't run here. Check the database with Postgres's own tools, and the attached files against your backups.")


def _path():
    return settings.data_dir / STATE


def load_state() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(**changes: Any) -> dict[str, Any]:
    st = {**load_state(), **changes}
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(_path())
    return st


def forget() -> None:
    """After a restore: the last result was about other data, so the next start checks again straight away."""
    _path().unlink(missing_ok=True)


def available() -> bool:
    return settings.database_url.startswith("sqlite:///")


def enabled() -> bool:
    return settings.check_interval_hours > 0


def _attached_bytes() -> int:
    from .backup import _sqlite_path
    con = sqlite3.connect(f"file:{_sqlite_path()}?mode=ro", uri=True)
    try:
        return int(con.execute("SELECT COALESCE(SUM(size), 0) FROM attachment").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def todays_share(now: datetime):
    """For a big lab: the seventh of the files re-read today (by fingerprint, so each one comes up once a week)."""
    day = now.date().toordinal() % 7
    return lambda sha: int(sha[:8], 16) % 7 == day


def run(now: datetime | None = None) -> dict[str, Any]:
    """Check the live data now, save the result, and email the admins if a new problem appeared."""
    from . import check

    now = now or datetime.now(timezone.utc)
    started = time.monotonic()
    every_file = True
    try:
        every_file = _attached_bytes() <= FULL_CHECK_BYTES
        res = check.check_live(None if every_file else todays_share(now))
    except Exception as exc:  # noqa: BLE001 - whatever went wrong is shown, and it's tried again later
        log.exception("The daily data check couldn't run")
        res = {"ok": False, "checks": [], "counts": {}, "error": str(exc) or exc.__class__.__name__}
    summary = {"ok": res["ok"], "at": now.isoformat(), "checks": res["checks"], "counts": res.get("counts", {}),
               "every_file": every_file, "seconds": round(time.monotonic() - started, 1),
               **({"error": res["error"]} if res.get("error") else {})}
    _save_state(last=summary)
    (log.info if summary["ok"] else log.warning)("Daily data check: %s", "all good" if summary["ok"] else "; ".join(warnings(summary)))
    _email_if_new(summary)
    return summary


def maybe_run(now: datetime | None = None) -> dict[str, Any] | None:
    """Called by the hourly housekeeping: run when the last check is a day old (or, if it found problems, a few hours)."""
    if not enabled() or not available():
        return None
    now = now or datetime.now(timezone.utc)
    last = load_state().get("last") or {}
    try:
        age_h = (now - datetime.fromisoformat(last["at"])).total_seconds() / 3600 if last else None
    except (KeyError, ValueError):
        age_h = None
    if age_h is not None and 0 <= age_h < (settings.check_interval_hours if last.get("ok") else min(RETRY_HOURS, settings.check_interval_hours)):
        return None
    return run(now)


# ------------------------------------------------------------------ what it means, in plain words

RESTORE = ("restore the latest backup that passes ./benchtop check (restoring replaces everything changed since that "
           "backup, so note any newer work first)")
ADVICE = {
    "Database file": "Stop Benchtop and " + RESTORE + ".",
    "Database version": "Install the newer Benchtop that last opened this data.",
    "Audit trail": "An event was changed or removed outside Benchtop. Find out who could reach the data folder; to get the "
                   "original trail back, " + RESTORE + ".",
    "Signed records": "They were changed outside Benchtop. Open each one and use Verify integrity to see what changed; to get "
                      "the signed content back, " + RESTORE + ".",
    "Trusted timestamps": "They were changed outside Benchtop. To get them back, " + RESTORE + ".",
    "Attached files": "Backups keep every attached file unchanged: use Put back from backups in Admin → Backups to copy the "
                      "good ones back (nothing else changes).",
}


def warnings(summary: dict[str, Any] | None) -> list[str]:
    if not summary or summary.get("ok"):
        return []
    if summary.get("error"):
        return [f"The daily data check couldn't run: {summary['error']}. It tries again in a few hours."]
    out = []
    for c in summary.get("checks", []):
        if not c["ok"]:
            out.append(f"{c['name']}: {c['detail']}. {ADVICE.get(c['name'], '')}".strip())
    return out or ["The daily data check found problems; see Admin → Backups."]


def health() -> dict[str, Any]:
    """For Admin → Backups and the dashboard."""
    last = load_state().get("last")
    return {"enabled": enabled(), "available": available(), "interval_hours": settings.check_interval_hours,
            "full_check_gb": FULL_CHECK_BYTES / 1e9, "last": last, "note": None if available() else POSTGRES,
            "warnings": warnings(last) if enabled() and available() else []}


# ------------------------------------------------------------------ putting attached files back

def put_back_files() -> dict[str, list[str]]:
    """Attached files that are missing or changed, copied back from the backups' shared folder, which keeps each one
    under its fingerprint (checked again before it's used). A changed file is moved aside to damaged-files/, not
    deleted. Nothing else changes: the database, and so every record and signature, stays as it is."""
    import os
    import shutil
    from datetime import datetime as _dt

    from . import backup
    from .check import _sha256

    stores = [settings.backup_dir / backup.FILES] + ([backup.copy_dir() / backup.FILES] if backup.copy_dir() else [])
    con = sqlite3.connect(f"file:{backup._sqlite_path()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT sha256, MIN(filename), MAX(size) FROM attachment GROUP BY sha256").fetchall()
    finally:
        con.close()
    put_back, no_copy = [], []
    for sha, name, size in rows:
        p = settings.attachments_dir / sha[:2] / sha
        if p.is_file() and (not size or p.stat().st_size == size) and _sha256(p) == sha:
            continue
        good = next((st / sha[:2] / sha for st in stores if (st / sha[:2] / sha).is_file() and _sha256(st / sha[:2] / sha) == sha), None)
        if good is None:
            no_copy.append(name)
            continue
        if p.exists():
            aside = settings.data_dir / "damaged-files"
            aside.mkdir(exist_ok=True)
            shutil.move(str(p), aside / f"{sha}-{_dt.now():%Y%m%d-%H%M%S}")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".putting-back")
        shutil.copyfile(good, tmp)
        os.replace(tmp, p)
        put_back.append(name)
    return {"put_back": put_back, "no_good_copy": no_copy}


# ------------------------------------------------------------------ email, once per new problem

def _email_if_new(summary: dict[str, Any]) -> None:
    if summary["ok"]:
        if load_state().get("emailed"):
            _save_state(emailed=None)  # all good again: the next problem gets an email too
        return
    found = warnings(summary)
    key = hashlib.sha256(json.dumps(sorted(c["name"] for c in summary["checks"] if not c["ok"]) or [summary.get("error", "")])
                         .encode()).hexdigest()[:16]
    if load_state().get("emailed") == key:
        return  # already told them about this
    from sqlmodel import Session, select

    from . import mailer
    from .db import engine
    from .models import Role, User
    if not mailer.configured():
        return
    try:
        with Session(engine) as s:
            admins = [u for u in s.exec(select(User).where(User.role == Role.admin)) if mailer.wants(u, "system")]
            for u in admins:
                mailer.queue_email(s, u.email, "data_check", problems=found, link=mailer.base_url() + "/admin/backups")
            s.commit()
        if admins:
            _save_state(emailed=key)
    except Exception:  # noqa: BLE001 - the dashboard still shows it
        log.exception("Couldn't email the admins about the data check")
