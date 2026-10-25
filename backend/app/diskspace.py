"""Free disk space (0.25): warn the admins early, and keep the last of it for saving.

Nothing watched free space before 0.25. A full disk makes saves, uploads and backups fail, and the first anyone heard
of it was an "Internal Server Error".

- The disks that matter are the one with Benchtop's data (database and attached files), and the ones with the backups
  and their second copy when those are elsewhere. Looking at them is one quick system call, so it's done each time the
  dashboard or Admin → Backups loads, not on a schedule.
- Admins are warned on the dashboard and in Admin → Backups when one has less than BENCHTOP_DISK_WARN_GB free
  (default 5 GB; at least twice the database's size, so a backup always fits), saying what to do.
- An upload, an instrument file or an import that would leave less than RESERVE_BYTES (1 GB) free is refused before
  anything is written, and so is a backup that wouldn't fit: the space that's left is kept for saving what people type.
  A disk that fills up anyway (another program) gives a plain message instead of a server error.
- BENCHTOP_DISK_WARN_GB=0 turns both off. A value below 1 also lowers the reserve to it (for a very small disk).
- macOS: Finder can show more "available" than Benchtop does, as it counts space macOS can free by itself (purgeable
  space, local Time Machine snapshots). Benchtop counts only what is free now, which is what a save can use.
"""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Any

from .config import settings

GB = 1e9
RESERVE_BYTES = 1 * GB

SAVE_FAILED = ("Not saved: the disk Benchtop keeps its data on is full. Tell your Benchtop admin, who'll find what to do "
               "on their dashboard. What you typed is still on this page: keep it open, or copy it somewhere safe.")


class DiskFull(OSError):
    """Refused before writing: this would leave less free space than Benchtop keeps for saving."""

    def __init__(self, message: str) -> None:
        super().__init__(errno.ENOSPC, message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def fmt(n: float) -> str:
    """1.2 GB, 850 MB: what people see in Finder (powers of 1000)."""
    if n >= 100 * GB:
        return f"{n / GB:.0f} GB"
    if n >= GB:
        return f"{n / GB:.1f} GB"
    return f"{max(n, 0) / 1e6:.0f} MB"


def warn_bytes() -> float:
    return max(settings.disk_warn_gb, 0) * GB


def reserve_bytes() -> float:
    return min(RESERVE_BYTES, warn_bytes())


def _existing(p: Path) -> Path | None:
    """The folder itself, or the nearest one above it that exists (a backup folder not made yet)."""
    for q in (p, *p.parents):
        if q.is_dir():
            return q
    return None


def free_bytes(p: Path) -> int | None:
    q = _existing(p)
    if q is None:
        return None
    try:
        return shutil.disk_usage(q).free
    except OSError:
        return None


def _device(p: Path) -> int | None:
    try:
        return os.stat(p).st_dev
    except OSError:
        return None


def database_bytes() -> int:
    """The database file and its write-ahead log (SQLite); 0 on Postgres, which keeps it elsewhere."""
    if not settings.database_url.startswith("sqlite:///"):
        return 0
    db = Path(settings.database_url.removeprefix("sqlite:///"))
    n = 0
    for p in (db, db.with_name(db.name + "-wal")):
        try:
            n += p.stat().st_size
        except OSError:
            pass
    return n


def places() -> list[dict[str, Any]]:
    """Each disk that matters, once, with what's on it. Benchtop's data comes first; a second-copy folder that isn't
    there (an external drive not plugged in) is left out: the backup warnings already say the copy failed."""
    from . import backup

    wanted = [("data", settings.data_dir), ("backups", settings.backup_dir)]
    if backup.copy_dir() is not None and backup.copy_dir().is_dir():
        wanted.append(("copy", backup.copy_dir()))
    out: list[dict[str, Any]] = []
    for what, p in wanted:
        q = _existing(p)
        if q is None:
            continue
        dev = _device(q)
        same = next((o for o in out if dev is not None and o["device"] == dev), None)
        if same is not None:
            same["holds"].append(what)
            continue
        try:
            u = shutil.disk_usage(q)
        except OSError:
            continue
        out.append({"device": dev, "path": str(p), "holds": [what], "free": u.free, "total": u.total})
    for o in out:
        o["label"] = _label(o["holds"])
    return out


def _label(holds: list[str], start: bool = True) -> str:
    """"Benchtop's data and the backups"; `start`: at the start of a sentence."""
    names = {"data": "Benchtop's data", "backups": "the backups", "copy": "the second copy of the backups"}
    parts = [names[h] for h in holds]
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return text[0].upper() + text[1:] if start else text


def threshold(holds: list[str]) -> float:
    """Below this much free space, admins are warned. A disk with the backups needs room for the next one."""
    t = warn_bytes()
    if t and ("backups" in holds or "copy" in holds):
        t = max(t, 2 * database_bytes())
    return t


def _advice(holds: list[str]) -> str:
    if "data" in holds:
        what = "saving, uploads and backups fail" if "backups" in holds else "saving and uploads fail"
        todo = "Delete files you no longer need on that disk, or move them to another drive."
    elif "backups" in holds:
        what = "backups fail"
        todo = "Free some space on it, or point BENCHTOP_BACKUP_DIR at a bigger disk."
    else:
        what = "the second copy of the backups fails"
        todo = "Free some space on it, or choose another folder for the second copy in Admin → Backups."
    return f"When it's full, {what}. {todo}"


def warnings() -> list[str]:
    """For the admin's dashboard and Admin → Backups; nothing when there's room (or the warning is turned off)."""
    if warn_bytes() <= 0:
        return []
    out = []
    for p in places():
        if p["free"] >= threshold(p["holds"]):
            continue
        holds = p["holds"]
        msg = (f"Only {fmt(p['free'])} free on the disk with {_label(holds, start=False)} "
               f"({p['path']}). {_advice(holds)}")
        if "data" in holds and p["free"] < reserve_bytes():
            msg += f" Until there's {fmt(reserve_bytes())} free, new uploads and instrument files are refused, to keep room for saving."
        out.append(msg)
    return out


def usage() -> dict[str, Any]:
    """For Admin → Backups: each disk, and how much of it is Benchtop's own."""
    from . import backup

    zips = sum(b.get("size", 0) for b in backup.list_backups())
    return {"places": [{k: v for k, v in p.items() if k != "device"} | {"warn_below": threshold(p["holds"])} for p in places()],
            "database": database_bytes(), "attached": _attached_bytes(),
            "backups": zips + backup.storage()["bytes"], "warn_gb": settings.disk_warn_gb,
            "reserve": reserve_bytes(), "warnings": warnings()}


def _attached_bytes() -> int:
    """Attached files, each counted once (they're stored once however many records use them)."""
    import sqlite3
    if not settings.database_url.startswith("sqlite:///"):
        return 0
    try:
        con = sqlite3.connect(f"file:{settings.database_url.removeprefix('sqlite:///')}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        return int(con.execute("SELECT COALESCE(SUM(s), 0) FROM (SELECT MAX(size) AS s FROM attachment GROUP BY sha256)").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        con.close()


# ------------------------------------------------------------------ keeping room for saving

def require_room(nbytes: int, where: Path | None = None, what: str = "This file") -> None:
    """Raise DiskFull if writing `nbytes` to `where` (Benchtop's data folder) would leave less than the reserve."""
    reserve = reserve_bytes()
    if reserve <= 0:
        return
    free = free_bytes(where or settings.data_dir)
    if free is None or free - max(nbytes, 0) >= reserve:
        return
    raise DiskFull(f"{what} wasn't saved: the disk Benchtop keeps its data on is nearly full ({fmt(free)} free), and "
                   f"Benchtop keeps the last {fmt(reserve)} for saving what people type. Tell your Benchtop admin, who'll "
                   "find what to do on their dashboard.")


def is_disk_full(exc: BaseException) -> bool:
    """A full disk, however it shows up: an OSError from writing a file, or SQLite's "database or disk is full"."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return True
        if exc.__class__.__name__ == "OperationalError" and "disk is full" in str(exc):
            return True
        exc = getattr(exc, "orig", None) or exc.__cause__ or exc.__context__
    return False


def message(exc: BaseException, file: bool = False) -> str:
    """What the person sees. `file`: it was a file being received (an upload), not something typed."""
    if isinstance(exc, DiskFull):
        return exc.message
    if file:
        return ("This file wasn't saved: the disk Benchtop keeps its data on is full. Tell your Benchtop admin, who'll "
                "find what to do on their dashboard.")
    return SAVE_FAILED
