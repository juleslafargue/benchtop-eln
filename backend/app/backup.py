"""Backups of the database and attachments.

A backup is a zip holding a consistent SQLite snapshot (taken with SQLite's online backup API,
safe while the server is running), the lab's plugin files and a manifest. Attached files live
beside the zips in one shared folder, `benchtop-files/`, each stored once under its SHA-256:

    backups/
      benchtop-backup-20261020-120000.zip     database + manifest (the list of files it needs)
      benchtop-backup-20261021-120000.zip
      benchtop-files/ab/ab12…                 every attached file, once

Attachments never change once saved, so a daily backup only has to add the new ones: 1 GB of
spectra costs about 1 GB of backup space, not 1 GB per backup kept. The second copy has the same
layout. To move or restore backups, keep the zip together with its `benchtop-files` folder.

Backups made before 0.15 carry their files inside the zip; they still restore and test as before.
The server makes a backup on startup if the newest is older than the interval, then checks hourly.

CLI (run from backend/):
    python -m app.backup create
    python -m app.backup list
    python -m app.backup test                   # unpack the newest backup and check it
    python -m app.backup restore <file.zip>     # stop the server first
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import settings

PREFIX = "benchtop-backup-"
FILES = "benchtop-files"  # the shared, write-once folder of attached files beside the zips
FORMAT = 2  # 1: files inside each zip (before 0.15); 2: files in FILES beside it
_SHA = re.compile(r"[0-9a-f]{64}")


class BackupError(RuntimeError):
    pass


def _sqlite_path() -> Path:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        raise BackupError("Built-in backups cover SQLite only; for Postgres use pg_dump on a schedule.")
    return Path(url.removeprefix("sqlite:///"))


def _newest_first(paths) -> list[Path]:
    """Backups newest first. Two made in the same second are named …-120000.zip and …-120000-2.zip, which
    plain name order would put the wrong way round."""
    def key(p: Path) -> tuple[str, int]:
        stem = p.stem.removeprefix(PREFIX)
        base, _, n = stem.partition("-")
        rest, _, n2 = n.partition("-")
        return (f"{base}-{rest}", int(n2) if n2.isdigit() else 1)
    return sorted(paths, key=key, reverse=True)


def list_backups() -> list[dict[str, Any]]:
    if not settings.backup_dir.exists():
        return []
    out = []
    for p in _newest_first(settings.backup_dir.glob(f"{PREFIX}*.zip")):
        st = p.stat()
        out.append({"name": p.name, "path": str(p), "size": st.st_size,
                    "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
    return out


def newest_age_hours() -> float | None:
    b = list_backups()
    if not b:
        return None
    return (time.time() - Path(b[0]["path"]).stat().st_mtime) / 3600


def newest_backup_info() -> dict[str, Any] | None:
    """The newest backup's database version (from its manifest) and age in minutes; None without a readable one."""
    b = list_backups()
    if not b:
        return None
    try:
        with zipfile.ZipFile(b[0]["path"]) as z:
            manifest = json.loads(z.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    return {"path": b[0]["path"], "schema_version": manifest.get("schema_version"),
            "age_minutes": (time.time() - Path(b[0]["path"]).stat().st_mtime) / 60}


_lock = threading.Lock()  # one backup at a time in this server (two clicks, or a click during the scheduled one)


def create_backup(reason: str = "manual") -> dict[str, Any]:
    with _lock:
        try:
            out = _create_backup(reason)
        except Exception as exc:  # recorded so the admin dashboard says why, not just that backups are getting old
            _note_failure(exc)
            raise
        _note_failure(None)
        return out


def _note_failure(exc: BaseException | None) -> None:
    import errno
    if exc is None:
        if load_state().get("last_failure"):
            _save_state(last_failure=None)
        return
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        msg = "the disk is full. Free some space (or choose a backup folder on a bigger disk)."
    else:
        msg = str(exc) or exc.__class__.__name__
    try:
        _save_state(last_failure={"at": datetime.now(timezone.utc).isoformat(), "error": msg})
    except OSError:  # a full disk can't take the note either; the log still has it
        pass


def _require_room(need: int, folder: Path, what: str) -> None:
    """A backup (or its copy) that wouldn't fit is refused before it starts (0.25): before, it filled the disk and then
    failed, and while the disk was full nobody could save."""
    from . import diskspace
    reserve = diskspace.reserve_bytes()
    free = diskspace.free_bytes(folder)
    if reserve <= 0 or free is None or free - need >= reserve:
        return
    raise BackupError(f"there isn't room for {what} on the disk with {folder}: it needs about {diskspace.fmt(need)}, "
                      f"{diskspace.fmt(free)} is free, and Benchtop keeps the last {diskspace.fmt(reserve)} for saving. "
                      "Free some space on that disk (or choose a backup folder on a bigger disk). Nothing was written.")


def _new_files_bytes(store: Path) -> int:
    """Attached files the shared folder doesn't have yet: what the next backup adds to it."""
    n = 0
    for b in settings.attachments_dir.rglob("*"):
        if _SHA.fullmatch(b.name) and not (store / b.name[:2] / b.name).exists():
            try:
                n += b.stat().st_size
            except OSError:
                pass
    return n


def _create_backup(reason: str) -> dict[str, Any]:
    db = _sqlite_path()
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    from .diskspace import database_bytes
    # the snapshot and the zip each take up to the database's size, while the new attached files are copied
    _require_room(2 * database_bytes() + _new_files_bytes(settings.backup_dir / FILES), settings.backup_dir, "a backup")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = settings.backup_dir / f"{PREFIX}{stamp}.zip"
    unique = f"{stamp}-{os.getpid()}-{threading.get_ident()}"  # also safe beside a ./benchtop backup in another process
    tmp_db = settings.backup_dir / f".snapshot-{unique}.db"
    tmp_zip = settings.backup_dir / f".{PREFIX}{unique}.zip.part"
    try:
        src = sqlite3.connect(db)
        dst = sqlite3.connect(tmp_db)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        # attached files go to the shared folder (new ones only); anything else in the folder rides in the zip
        files: dict[str, int] = {}
        damaged: list[str] = []
        others: list[Path] = []
        store = settings.backup_dir / FILES
        for b in sorted(settings.attachments_dir.rglob("*")):
            if not b.is_file():
                continue
            if not _SHA.fullmatch(b.name):
                others.append(b)
                continue
            try:
                _store_file(b, store, b.name)
            except _Damaged:
                damaged.append(b.name)
                if not (store / b.name[:2] / b.name).is_file():
                    others.append(b)  # no good copy anywhere: keep it as it is, inside the zip, for check to name
                    continue
            files[b.name] = (store / b.name[:2] / b.name).stat().st_size
        manifest = {
            "format": FORMAT, "app_version": __version__, "created_at": datetime.now(timezone.utc).isoformat(), "reason": reason,
            "database_sha256": _sha256(tmp_db), "attachments": len(files), "attachment_bytes": sum(files.values()),
            "files": files, **({"damaged": damaged} if damaged else {}),
            **_fingerprint(tmp_db),
        }
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            z.write(tmp_db, "benchtop.db")
            for b in others:
                z.write(b, f"attachments/{b.relative_to(settings.attachments_dir)}", compress_type=zipfile.ZIP_STORED)
            plug = settings.data_dir / "plugins"
            for f in sorted(plug.glob("*.toml")) if plug.is_dir() else []:  # your lab's plugin files
                z.write(f, f"plugins/{f.name}")
        final = _claim_name(tmp_zip, final)
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_zip.unlink(missing_ok=True)
    prune()
    out = {"name": final.name, "path": str(final), "size": final.stat().st_size,
           **{k: v for k, v in manifest.items() if k != "files"}}
    out["copy"] = copy_to_second_place(final)
    return out


# ------------------------------------------------------------------ the shared folder of attached files

class _Damaged(Exception):
    """A file's content no longer matches the fingerprint it's stored under."""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _store_file(src: Path, store: Path, sha: str) -> bool:
    """Put one attached file into a `benchtop-files` folder, unless it's already there.
    The copy is checked against its fingerprint on the way; a file already stored is never replaced,
    so a good copy survives if the original is damaged later. Returns True if it was copied."""
    dst = store / sha[:2] / sha
    if dst.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(f".{sha}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        h = hashlib.sha256()
        with open(src, "rb") as fi, open(part, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        if h.hexdigest() != sha:
            raise _Damaged(sha)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)
    return True


def read_manifest(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as z:
        return json.loads(z.read("manifest.json"))


def needed_files(manifest: dict[str, Any]) -> dict[str, int]:
    """The attached files a backup expects in the `benchtop-files` folder beside it ({} for old-style backups)."""
    return (manifest.get("files") or {}) if (manifest.get("format") or 1) >= 2 else {}


def missing_files(zip_path: Path, manifest: dict[str, Any]) -> list[str]:
    store = zip_path.parent / FILES
    return [sha for sha in needed_files(manifest) if not (store / sha[:2] / sha).is_file()]


def _collect_garbage(folder: Path, zips: list[Path]) -> int:
    """Remove stored files that no kept backup needs. In practice that's only after restoring an older
    backup, since Benchtop never deletes attachments. Careful on purpose: nothing is removed if any
    backup's list can't be read, nor a file that's still in Benchtop's data or is less than a day old."""
    store = folder / FILES
    if not store.is_dir():
        return 0
    keep: set[str] = set()
    for z in zips:
        try:
            keep.update(needed_files(read_manifest(z)))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            return 0
    cutoff = time.time() - 86400
    removed = 0
    for p in store.glob("*/*"):
        try:
            if p.stat().st_mtime > cutoff:
                continue
            if p.name.startswith("."):  # a copy interrupted more than a day ago
                p.unlink()
            elif p.name not in keep and not (settings.attachments_dir / p.name[:2] / p.name).exists():
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def storage() -> dict[str, Any]:
    """How much the shared folder of attached files holds, from the newest backup's list."""
    b = list_backups()
    if not b:
        return {"files": 0, "bytes": 0}
    try:
        m = read_manifest(Path(b[0]["path"]))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {"files": 0, "bytes": 0}
    f = needed_files(m)
    return {"files": len(f), "bytes": sum(f.values())}


def _fingerprint(db: Path) -> dict[str, Any]:
    """Database version and the audit trail's latest fingerprint, recorded in the manifest. Kept in a
    second place, these make it detectable if the trail were ever rewritten wholesale later."""
    try:
        con = sqlite3.connect(db)
        try:
            head = con.execute("SELECT hash FROM auditevent ORDER BY id DESC LIMIT 1").fetchone()
            n = con.execute("SELECT COUNT(*) FROM auditevent").fetchone()[0]
            try:
                v = con.execute("SELECT MAX(number) FROM benchtop_schema").fetchone()[0] or 0
            except sqlite3.OperationalError:
                v = 0
        finally:
            con.close()
        return {"schema_version": v, "audit_events": n, "audit_head": head[0] if head else None}
    except sqlite3.Error:
        return {}


# ------------------------------------------------------------------ a second copy, somewhere else

def _state_path() -> Path:
    return settings.data_dir / "backup-settings.json"


def load_state() -> dict[str, Any]:
    """Second-copy folder and the latest copy/check results (kept outside the database on purpose)."""
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(**changes: Any) -> dict[str, Any]:
    st = {**load_state(), **changes}
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(_state_path())
    return st


def copy_dir() -> Path | None:
    env = os.environ.get("BENCHTOP_BACKUP_COPY_DIR")
    raw = env if env is not None else load_state().get("copy_dir")
    return Path(raw).expanduser() if raw else None


def validate_copy_dir(raw: str) -> Path:
    """A folder that exists (or whose parent does), is writable, and isn't inside Benchtop's own data."""
    try:
        return _validate_copy_dir(raw)
    except OSError as exc:  # a name too long, no permission, a drive that vanished…
        raise BackupError(f"Benchtop can't use that folder: {exc.strerror or exc}") from None


def _validate_copy_dir(raw: str) -> Path:
    if len(raw) > 1024:
        raise BackupError("That path is too long")
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        raise BackupError("Give the full path of the folder, e.g. /Volumes/MyDrive/Benchtop backups")
    p = p.resolve()
    data = settings.data_dir.resolve()
    if p == data or data in p.parents or p == settings.backup_dir.resolve():
        raise BackupError("Choose a folder outside Benchtop's data folder; the point is a copy somewhere else")
    if not p.exists():
        if not p.parent.is_dir():
            raise BackupError(f"{p.parent} doesn't exist. Is the drive plugged in?")
        p.mkdir()
    if not p.is_dir():
        raise BackupError(f"{p} is a file, not a folder")
    probe = p / ".benchtop-write-test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise BackupError(f"Benchtop can't write to {p}: {exc.strerror or exc}") from None
    return p


def set_copy_dir(raw: str | None) -> dict[str, Any]:
    if not raw:
        return _save_state(copy_dir=None, last_copy=None)
    p = validate_copy_dir(raw)
    return _save_state(copy_dir=str(p), last_copy=None)


def suggested_copy_dirs() -> list[dict[str, str]]:
    """Places a Mac usually has for an off-laptop copy: iCloud Drive and plugged-in drives."""
    out = []
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.is_dir():
        out.append({"label": "iCloud Drive", "path": str(icloud / "Benchtop backups")})
    vols = Path("/Volumes")
    if vols.is_dir():
        for v in sorted(vols.iterdir()):
            if v.is_dir() and not v.is_symlink() and v.name not in ("Macintosh HD", "Recovery", "Preboot", "VM", "Data"):
                out.append({"label": f"Drive “{v.name}”", "path": str(v / "Benchtop backups")})
    for name in ("Dropbox", "Google Drive", "OneDrive"):
        d = Path.home() / name
        if d.is_dir():
            out.append({"label": name, "path": str(d / "Benchtop backups")})
    return out


def copy_backups() -> list[Path]:
    d = copy_dir()
    return _newest_first(d.glob(f"{PREFIX}*.zip")) if d and d.is_dir() else []


def copy_to_second_place(src: Path) -> dict[str, Any] | None:
    """Copy a finished backup to the second folder (keeping the same number as locally): first the
    attached files it needs that aren't there yet, each checked, then the zip itself. So a zip in the
    second place always has its files beside it."""
    d = copy_dir()
    if d is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    try:
        if not d.is_dir():
            raise BackupError(f"{d} isn't there. If it's on an external drive, plug it in; the next backup will be copied.")
        need = src.stat().st_size + sum(size for sha, size in needed_files(read_manifest(src)).items()
                                        if not (d / FILES / sha[:2] / sha).exists())
        _require_room(need, d, "the second copy")
        new_files = 0
        for sha in needed_files(read_manifest(src)):
            here = src.parent / FILES / sha[:2] / sha
            try:
                new_files += _store_file(here, d / FILES, sha)
            except _Damaged:
                raise BackupError(f"A stored file ({sha[:12]}…) no longer matches its fingerprint; run ./benchtop check") from None
            except FileNotFoundError:
                raise BackupError(f"A file this backup needs ({sha[:12]}…) is missing from {src.parent / FILES}") from None
        part = d / f".{src.name}.{os.getpid()}-{threading.get_ident()}.part"
        shutil.copyfile(src, part)
        if _sha256(part) != _sha256(src):
            part.unlink(missing_ok=True)
            raise BackupError("The copy didn't match the original; it was removed")
        part.replace(d / src.name)
        for old in copy_backups()[settings.backup_keep:]:
            old.unlink(missing_ok=True)
        _collect_garbage(d, copy_backups())
        res = {"ok": True, "at": now, "name": src.name, "folder": str(d), "new_files": new_files}
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, BackupError) as exc:
        import errno
        full = isinstance(exc, OSError) and exc.errno == errno.ENOSPC
        res = {"ok": False, "at": now, "name": src.name, "folder": str(d),
               "error": f"the disk with {d} is full. Free some space there, or choose another folder." if full else str(exc)}
    _save_state(last_copy=res)
    return res


# ------------------------------------------------------------------ the weekly backup test

CHECK_EVERY_DAYS = 7


def test_latest_backup() -> dict[str, Any] | None:
    """Unpack the newest backup (the second copy when there is one: that's what you'd restore from if
    this computer were lost) and check it as if it had been restored."""
    from .check import check_backup

    copies = copy_backups()
    target = copies[0] if copies else (Path(list_backups()[0]["path"]) if list_backups() else None)
    if target is None:
        return None
    res = check_backup(target)
    summary = {"ok": res["ok"], "at": datetime.now(timezone.utc).isoformat(), "name": target.name,
               "where": "second copy" if copies else "this computer", "checks": res["checks"], "counts": res.get("counts", {})}
    _save_state(last_check=summary)
    return summary


def maybe_test_backup() -> dict[str, Any] | None:
    if settings.backup_interval_hours <= 0:
        return None
    last = load_state().get("last_check") or {}
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])).total_seconds() / 86400 if last else None
    except (KeyError, ValueError):
        age = None
    if age is not None and age < CHECK_EVERY_DAYS and last.get("ok"):
        return None
    if age is not None and age < 1:  # a failed test is retried daily, not hourly
        return None
    return test_latest_backup()


def health() -> dict[str, Any]:
    """What the admin page and dashboard show about backups."""
    st = load_state()
    b = list_backups()
    newest = b[0] if b else None
    age_h = newest_age_hours()
    warnings = []
    if not settings.database_url.startswith("sqlite:///"):
        warnings.append("Automatic backups can't run: they need Benchtop's standard database (SQLite), and this server uses "
                        "another one (Postgres). Back up the database with pg_dump and copy the attached files yourself; "
                        "see “Postgres (advanced)” in docs/SERVER.md.")
    elif settings.backup_interval_hours > 0:
        if st.get("last_failure"):
            warnings.append(f"The last backup failed: {st['last_failure']['error']}")
        if newest is None and not st.get("last_failure"):
            warnings.append("No backups yet.")
        elif age_h is not None and age_h > max(72, settings.backup_interval_hours * 3):
            warnings.append(f"The newest backup is {age_h / 24:.0f} days old.")
        if copy_dir() is None:
            warnings.append("Backups are only on this computer. Choose a second place (iCloud Drive or an external drive) in Admin → Backups.")
        elif st.get("last_copy") and not st["last_copy"].get("ok"):
            warnings.append(f"Copying the latest backup failed: {st['last_copy'].get('error')}")
        if st.get("last_check") and not st["last_check"].get("ok"):
            bad = [c["name"] for c in st["last_check"].get("checks", []) if not c["ok"]]
            warnings.append(f"The latest backup test found problems ({', '.join(bad) or 'see Admin → Backups'}).")
    return {"copy_dir": str(copy_dir()) if copy_dir() else None, "copy_dir_from_env": "BENCHTOP_BACKUP_COPY_DIR" in os.environ,
            "last_copy": st.get("last_copy"), "last_check": st.get("last_check"), "copies": len(copy_backups()),
            "files": storage(), "warnings": warnings}


def _claim_name(tmp: Path, final: Path) -> Path:
    """Give the finished zip its name without ever replacing another backup (two in the same second get -2, -3…)."""
    n = 1
    while True:
        target = final if n == 1 else final.with_name(f"{final.stem}-{n}{final.suffix}")
        try:
            os.link(tmp, target)  # fails if the name is taken, unlike rename
        except FileExistsError:
            n += 1
            continue
        except OSError:  # file systems without hard links: fall back to a checked rename
            if target.exists():
                n += 1
                continue
            tmp.rename(target)
            return target
        tmp.unlink(missing_ok=True)
        return target


def prune(keep: int | None = None) -> None:
    keep = settings.backup_keep if keep is None else keep
    for b in list_backups()[keep:]:
        Path(b["path"]).unlink(missing_ok=True)
    _clear_leftovers()
    _collect_garbage(settings.backup_dir, [Path(b["path"]) for b in list_backups()])


def _clear_leftovers() -> None:
    """Pieces an interrupted backup, restore or upload leaves behind (Benchtop stopped, the Mac turned off): a database
    snapshot can be as big as the database, so they're removed once they're a day old."""
    cutoff = time.time() - 86400
    spots = [(settings.backup_dir, (".snapshot-*", f".{PREFIX}*.part")), (settings.data_dir / ".incoming", ("*.part",))]
    if not (settings.data_dir / JOURNAL).exists():  # a restore still to be finished needs its staged files
        spots.append((settings.data_dir, (".restore-*",)))
    for folder, patterns in spots:
        for pat in patterns:
            for p in folder.glob(pat) if folder.is_dir() else []:
                try:
                    if p.stat().st_mtime < cutoff:
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
                except OSError:
                    continue


def restore(zip_path: str, allow_missing: bool = False) -> Path:
    """Replace the current database and attachments with a backup's.
    The current data is moved aside first, never deleted."""
    return _restore(zip_path, allow_missing)[0]


def _restore(zip_path: str, allow_missing: bool = False) -> tuple[Path, list[str]]:
    """Everything is unpacked and checked into a staging folder first; only then is the current data
    moved aside and the backup's put in its place. Returns (where the old data went, problems)."""
    finish_interrupted_restore()  # an earlier restore that was cut off comes first
    zp = Path(zip_path).expanduser().resolve()
    if not zp.exists():
        raise BackupError(f"{zp} not found")
    problems: list[str] = []
    token = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{threading.get_ident()}"
    stage = settings.data_dir / f".restore-{token}"
    db = _sqlite_path()
    new_db = db.with_name(f".restore-{token}.db")  # beside the database, so putting it in place is one rename
    try:
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
            if "benchtop.db" not in names or "manifest.json" not in names:
                raise BackupError("Not a Benchtop backup (missing benchtop.db or manifest.json)")
            manifest = json.loads(z.read("manifest.json"))
            db_bytes = z.read("benchtop.db")
            if hashlib.sha256(db_bytes).hexdigest() != manifest["database_sha256"]:
                raise BackupError("Backup is corrupt: database checksum does not match")
            from .migrations import SCHEMA_VERSION
            if (manifest.get("schema_version") or 0) > SCHEMA_VERSION:
                raise BackupError(f"This backup was made by a newer Benchtop ({manifest.get('app_version')}); install that version "
                                  "or newer first, then restore it. Nothing was changed.")
            missing = set(missing_files(zp, manifest))
            if missing and not allow_missing:
                raise BackupError(
                    f"This backup keeps its attached files in the “{FILES}” folder next to it, and {len(missing)} of "
                    f"{len(needed_files(manifest))} aren't there ({zp.parent / FILES}). Copy the whole backups folder, not just "
                    "the zip, and try again. Nothing was changed. (To restore without them anyway: "
                    "./benchtop restore FILE --without-missing-files)")
            if missing:
                problems.append(f"{len(missing)} attached files were missing from the backup and couldn't be restored")
            att = stage / "attachments"
            att.mkdir(parents=True)
            for n in names:  # files inside the zip (every attachment, for backups made before 0.15)
                if n.startswith("attachments/") and not n.endswith("/"):
                    target = (att / n.removeprefix("attachments/")).resolve()
                    if att.resolve() not in target.parents:
                        raise BackupError(f"Refusing unsafe path in backup: {n}. Nothing was changed.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(n) as fi, open(target, "wb") as fo:
                        shutil.copyfileobj(fi, fo, 1 << 20)
                        fo.flush()
                        os.fsync(fo.fileno())
            damaged = 0
            for sha in needed_files(manifest):
                if sha in missing:
                    continue
                try:
                    _store_file(zp.parent / FILES / sha[:2] / sha, att, sha)
                except _Damaged:  # restore what there is; ./benchtop check will name the record
                    damaged += 1
                    target = att / sha[:2] / sha
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(zp.parent / FILES / sha[:2] / sha, target)
            if damaged:
                problems.append(f"{damaged} attached files in the backup no longer match their fingerprints")
            plug_names = [n for n in names if n.startswith("plugins/") and n.endswith(".toml") and "/" not in n.removeprefix("plugins/")]
            if plug_names:  # backups from v0.10 on also hold the lab's plugin files
                (stage / "plugins").mkdir()
                for n in plug_names:
                    (stage / "plugins" / Path(n).name).write_bytes(z.read(n))
        with open(new_db, "wb") as f:
            f.write(db_bytes)
            f.flush()
            os.fsync(f.fileno())
        # everything is ready. From here on, a restore cut off part-way (Benchtop killed, the Mac turned off) is
        # finished the next time Benchtop starts, so the data is never left half old, half restored (0.24.2)
        aside = settings.data_dir / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}"
        n = 1
        while aside.exists():  # two restores within the same second
            n += 1
            aside = aside.with_name(f"{aside.name.split('~')[0]}~{n}")
        _write_journal({"stage": str(stage), "new_db": str(new_db), "db": str(db), "aside": str(aside), "from": str(zp)})
        _finish_restore()
        return aside, problems
    finally:
        if not (settings.data_dir / JOURNAL).exists():
            shutil.rmtree(stage, ignore_errors=True)
            new_db.unlink(missing_ok=True)


JOURNAL = "restore-in-progress.json"


def _write_journal(info: dict[str, str]) -> None:
    j = settings.data_dir / JOURNAL
    tmp = j.with_suffix(".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(info, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, j)
    _sync_dir(settings.data_dir)


def _sync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _set_aside(p: Path, target: Path) -> None:
    """Move something out of the way, never over something already set aside (which is kept as well)."""
    n = 1
    while os.path.lexists(target):
        n += 1
        target = target.with_name(f"{target.name.split('~')[0]}~{n}")
    shutil.move(str(p), target)


def finish_interrupted_restore() -> Path | None:
    """A restore cut off part-way is finished: called first thing when Benchtop starts, and before a restore.
    Returns where the data from before it went (None if there was nothing to finish)."""
    if not (settings.data_dir / JOURNAL).exists():
        return None
    return _finish_restore()


def _finish_restore() -> Path:
    """Put the staged backup in place. Each step is a rename, and each is skipped once done, so this can be cut off
    and run again any number of times: the data ends up as the backup's, and the data from before is set aside."""
    from . import integrity
    from .db import engine

    j = settings.data_dir / JOURNAL
    try:
        info = json.loads(j.read_text())
    except ValueError as exc:
        raise BackupError(f"{j} is damaged ({exc}), so the restore it describes can't be finished. Nothing more was "
                          f"changed; the data from before the restore is in a pre-restore-… folder in {settings.data_dir}.") from None
    stage, new_db, db, aside = (Path(info[k]) for k in ("stage", "new_db", "db", "aside"))
    engine.dispose()  # release pooled connections to the file being replaced
    aside.mkdir(parents=True, exist_ok=True)
    for name, live in (("attachments", settings.attachments_dir), ("plugins", settings.data_dir / "plugins")):
        if (stage / name).exists():  # not yet in place, so what's there now is the data from before
            if os.path.lexists(live):
                _set_aside(live, aside / name)
            os.replace(stage / name, live)
    if new_db.exists():
        for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if p.exists():
                _set_aside(p, aside / p.name)
        os.replace(new_db, db)
        _sync_dir(db.parent)
    integrity.forget()  # the last data check was about the data just moved aside
    shutil.rmtree(stage, ignore_errors=True)
    j.unlink()
    engine.dispose()
    return aside


def maybe_backup() -> dict[str, Any] | None:
    """Make a backup if none exists or the newest is older than the interval."""
    if settings.backup_interval_hours <= 0:
        return None
    try:
        _sqlite_path()
    except BackupError:
        return None
    age = newest_age_hours()
    if age is None or age >= settings.backup_interval_hours:
        return create_backup("scheduled")
    return None


def _which_backup() -> int:
    """./benchtop restore without a file: the backups there are, and the commands to paste."""
    import shlex
    bs = list_backups()
    if not bs:
        print(f"No backups in {settings.backup_dir}, so there's nothing to restore.\n"
              "To restore a backup from somewhere else, give its path: ./benchtop restore path/to/benchtop-backup-….zip")
        return 1
    print(f"Which backup? These are in {settings.backup_dir}, newest first:")
    for b in bs[:10]:
        print(f"  {b['name']}  {b['size'] / 1e6:6.1f} MB")
    if len(bs) > 10:
        print(f"  … and {len(bs) - 10} older ones (./benchtop backups lists them all)")
    newest = shlex.quote(bs[0]["path"])
    print(f"\nTo restore the newest (your current data is moved aside, not deleted):\n  ./benchtop restore {newest}\n"
          f"To check a backup without changing anything:\n  ./benchtop check {newest}")
    return 1


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "help"
    try:
        if cmd == "create":
            b = create_backup("manual")
            print(f"Backup written: {b['path']} ({b['size'] / 1e6:.1f} MB; {b['attachments']} attached files, "
                  f"{b['attachment_bytes'] / 1e6:.1f} MB, kept once in {settings.backup_dir / FILES})")
            if b.get("damaged"):
                print(f"Warning: {len(b['damaged'])} attached files no longer match their fingerprints. Run ./benchtop check.")
        elif cmd == "list":
            bs = list_backups()
            print(f"Backups in {settings.backup_dir}:" if bs else f"No backups in {settings.backup_dir}")
            for b in bs:
                print(f"  {b['name']}  {b['size'] / 1e6:6.1f} MB")
            if bs:
                st = storage()
                print(f"Attached files, kept once for all of them in {FILES}/: {st['files']:,} ({st['bytes'] / 1e6:.1f} MB)")
        elif cmd == "test":
            from .check import report
            r = test_latest_backup()
            print("No backups to test yet." if r is None else f"Tested {r['name']} ({r['where']}):\n" + report(r))
            return 0 if r is None or r["ok"] else 1
        elif cmd == "restore" and len(argv) == 2:
            return _which_backup()
        elif cmd == "restore" and len(argv) > 2:
            aside, problems = _restore(argv[2], allow_missing="--without-missing-files" in argv[3:])
            print(f"Restored {argv[2]}.\nYour previous data was kept in {aside}")
            for p in problems:
                print(f"Warning: {p}. Run ./benchtop check to see which records are affected.")
        else:
            print(__doc__)
            return 1
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
