"""Health check of a Benchtop database: is it intact, and do the records still verify?

Used by `./benchtop check` (on a snapshot of the live data, safe while the server runs) and by the
weekly backup test (on a backup restored into a temporary folder).

CLI (run from backend/):
    python -m app.check                 # the live data
    python -m app.check backup.zip      # a backup, unpacked to a temporary folder
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlmodel import Session, select


def _sha256(p: Path) -> str:
    """In 1 MB pieces: instrument data can be gigabytes."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _all(n: int, many: str, one: str) -> str:
    """"all 24 match their signatures", but "1, and it matches its signature" and "none yet"."""
    return "none yet" if n == 0 else f"1, {one}" if n == 1 else f"all {n:,} {many}"


def _item(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def check_files(db_path: Path, attachments_dir: Path, rehash: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Every check on one database file plus its attachment folder (neither is modified).
    `rehash`: re-read only the attached files it picks (by SHA-256); the rest are checked for being there with the right
    size (the daily check of a big lab, which re-reads each file once a week). None re-reads every file."""
    from . import audit, migrations
    from .models import Attachment, Experiment, ExperimentStatus, Signature
    from .services import content_hash

    checks: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    # 1. the file itself
    con = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
    try:
        res = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        con.close()
        return {"ok": False, "checks": [_item("Database file", False, f"Not a readable database: {exc}")], "counts": counts}
    con.close()
    checks.append(_item("Database file", res == ["ok"], "SQLite integrity check passed" if res == ["ok"] else "; ".join(res[:5])))

    engine = create_engine(f"sqlite:///file:{db_path}?immutable=1&uri=true")
    try:
        # 2. upgrade level
        v = migrations.current_version(engine)
        checks.append(_item("Database version", v <= migrations.SCHEMA_VERSION,
                            f"version {v} (this Benchtop understands up to {migrations.SCHEMA_VERSION})"))
        with Session(engine) as s:
            # 3. audit trail
            chain = audit.verify_chain(s)
            counts["audit_events"] = chain["checked"]
            fork_note = (f" ({chain['forks']} pair{'s' if chain['forks'] != 1 else ''} written at the same moment by an older version;"
                         " both intact, not tampering)") if chain.get("forks") else ""
            checks.append(_item("Audit trail", chain["ok"],
                                f"{chain['checked']:,} events, unbroken{fork_note}; latest fingerprint {chain['head'][:16]}…" if chain["ok"]
                                else f"broken at event {chain.get('broken_at')} ({chain.get('reason')})"))
            # 4. signed records still match their signatures
            bad, n = [], 0
            for exp in s.exec(select(Experiment).where(Experiment.status.in_([ExperimentStatus.signed, ExperimentStatus.witnessed]))):
                n += 1
                sig = s.exec(select(Signature).where(Signature.experiment_id == exp.id, Signature.meaning.in_(["author", "witness"]))
                             .order_by(Signature.id.desc())).first()
                if sig is None or sig.content_hash != content_hash(s, exp):
                    bad.append(exp.eid)
            counts["signed_records"] = n
            checks.append(_item("Signed records", not bad, _all(n, "match their signatures", "and it matches its signature") if not bad
                                else f"{len(bad)} of {n} no longer match: {', '.join(bad[:10])}{'…' if len(bad) > 10 else ''}"))
            # 5. trusted timestamps still verify against what was signed, with the certificate they were fetched with
            from .models import AuditEvent, SignatureTimestamp
            from . import timestamps as tsmod
            import base64 as _b64
            fetched = {e.object_id: e.details or {} for e in s.exec(
                select(AuditEvent).where(AuditEvent.action == "timestamp", AuditEvent.object_type == "signature"))}
            good = bad = waiting = untraced = 0
            bad_ids = []
            for row in s.exec(select(SignatureTimestamp)):
                if row.status != "ok":
                    waiting += row.status == "pending"
                    continue
                sig = s.get(Signature, row.signature_id)
                same = sig is not None and hashlib.sha256(row.payload.encode()).hexdigest() == row.digest and \
                    tsmod.payload_for(s, sig) == row.payload
                info = tsmod.verify(_b64.b64decode(row.token or ""), bytes.fromhex(row.digest)) if same and tsmod.available() else {"ok": False}
                rec = fetched.get(row.signature_id, {})
                # the certificate must be the one recorded (in the tamper-evident audit trail) when it was fetched;
                # whether it's still traced to a well-known authority is only noted: authorities do get retired
                ok = info["ok"] and (not rec.get("cert_sha256") or rec["cert_sha256"] == info["cert_sha256"]) \
                    and (not rec.get("digest") or rec["digest"] == row.digest)
                if ok:
                    good += 1
                    untraced += not info["trusted"]
                else:
                    bad += 1
                    bad_ids.append(str(row.signature_id))
            counts["timestamps"] = good
            if good or bad or waiting:
                note = (f"; {untraced} from a service whose certificate isn't traced to a well-known authority (e.g. FreeTSA)"
                        if untraced else "")
                checks.append(_item("Trusted timestamps", not bad,
                                    (_all(good, "verify", "and it verifies") if not bad else f"{bad} don't verify (signatures {', '.join(bad_ids[:10])})")
                                    + note + (f"; {waiting} still to fetch" if waiting else "")))
            # 6. attached files
            missing, damaged, n, reread = [], [], 0, 0
            for a in s.exec(select(Attachment)):
                n += 1
                p = attachments_dir / a.sha256[:2] / a.sha256
                if not p.is_file():
                    missing.append(a.filename)
                elif a.size and p.stat().st_size != a.size:  # recorded since 0.7; a different size is a changed file
                    damaged.append(a.filename)
                elif rehash is None or rehash(a.sha256):
                    reread += 1
                    if _sha256(p) != a.sha256:
                        damaged.append(a.filename)
            counts["attachments"] = n
            detail = _all(n, "present and unchanged", "present and unchanged")
            if rehash is not None and n:
                detail = (f"all {n:,} present, the right size; {reread:,} re-read today and unchanged "
                          "(each file is re-read once a week)")
            if missing or damaged:
                detail = "; ".join(x for x in (f"{len(missing)} missing ({', '.join(missing[:5])})" if missing else "",
                                               f"{len(damaged)} changed ({', '.join(damaged[:5])})" if damaged else "") if x)
            checks.append(_item("Attached files", not (missing or damaged), detail))
    finally:
        engine.dispose()
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "counts": counts,
            "audit_head": chain["head"] if chain["ok"] else None}


def check_live(rehash: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Check the running data via a consistent snapshot, so the server can keep running."""
    from .backup import _sqlite_path
    from .config import settings

    with tempfile.TemporaryDirectory(prefix="benchtop-check-") as tmp:
        snap = Path(tmp) / "snapshot.db"
        src, dst = sqlite3.connect(_sqlite_path()), sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return check_files(snap, settings.attachments_dir, rehash)


def _bring_up_to_date(db: Path) -> int | None:
    """A backup from an older version (such as the one made just before an upgrade, which keeps the old version on
    purpose) is upgraded, in its temporary copy only, as restoring and starting Benchtop would. Before 0.24.2 its check
    failed with an error instead, because today's checks read it with today's tables. Returns its version if older."""
    from sqlmodel import SQLModel

    from . import migrations, models  # noqa: F401  (register tables)
    from .db import add_missing_columns

    con = sqlite3.connect(db)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return None  # a damaged file isn't upgraded: the checks report it as it is
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()
    eng = create_engine(f"sqlite:///{db}")
    try:
        have = migrations.current_version(eng)
        if have >= migrations.SCHEMA_VERSION:
            return None
        SQLModel.metadata.create_all(eng)
        add_missing_columns(eng)
        migrations.upgrade(eng)
        return have
    finally:
        eng.dispose()


def check_backup(zip_path: Path) -> dict[str, Any]:
    """Unpack a backup to a temporary folder and check it as if it had been restored."""
    from .backup import FILES, missing_files, needed_files

    with tempfile.TemporaryDirectory(prefix="benchtop-backup-test-") as tmp:
        root = Path(tmp)
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
                if "benchtop.db" not in names or "manifest.json" not in names:
                    return {"ok": False, "checks": [_item("Backup file", False, "Not a Benchtop backup")], "counts": {}}
                manifest = json.loads(z.read("manifest.json"))
                bad = z.testzip()
                if bad:
                    return {"ok": False, "checks": [_item("Backup file", False, f"Damaged inside the zip: {bad}")], "counts": {}}
                (root / "benchtop.db").write_bytes(z.read("benchtop.db"))
                att = root / "attachments"
                att.mkdir()
                for n in names:
                    if n.startswith("attachments/") and not n.endswith("/"):
                        t = (att / n.removeprefix("attachments/")).resolve()
                        if att.resolve() not in t.parents:
                            return {"ok": False, "checks": [_item("Backup file", False, f"Unsafe path inside: {n}")], "counts": {}}
                        t.parent.mkdir(parents=True, exist_ok=True)
                        t.write_bytes(z.read(n))
        except (zipfile.BadZipFile, OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "checks": [_item("Backup file", False, f"Couldn't read it: {exc}")], "counts": {}}
        db_ok = hashlib.sha256((root / "benchtop.db").read_bytes()).hexdigest() == manifest.get("database_sha256")
        first = _item("Backup file", db_ok, f"unpacked; made by Benchtop {manifest.get('app_version', '?')}" if db_ok
                      else "the database inside doesn't match its checksum")
        if not db_ok:
            return {"ok": False, "checks": [first], "counts": {}}
        try:
            older = _bring_up_to_date(root / "benchtop.db")
        except Exception as exc:  # noqa: BLE001 - reported as the result, like any other problem found
            return {"ok": False, "counts": {}, "checks": [first, _item(
                "Database version", False, f"made by an older Benchtop, and it couldn't be brought up to date: {exc}")]}
        if older is not None:
            first["detail"] += (f" (database version {older}; checked as it would be after restoring, brought up to date "
                                "the way Benchtop does when it starts)")
        # newer backups keep attached files once, in the shared folder beside the zip: check them there
        store = zip_path.parent / FILES
        if needed_files(manifest):
            for sha in needed_files(manifest):
                link = att / sha[:2] / sha
                if not link.exists() and (store / sha[:2] / sha).is_file():
                    link.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(store / sha[:2] / sha, link)  # read in place; nothing large is copied
        res = check_files(root / "benchtop.db", att)
        missing = missing_files(zip_path, manifest)
        if missing:
            res["checks"].append(_item("Shared file folder", False, f"{len(missing)} files this backup needs aren't in {store}"))
            res["ok"] = False
        res["checks"].insert(0, first)
        res["manifest"] = manifest
        return res


def report(res: dict[str, Any]) -> str:
    lines = [f"  {'✓' if c['ok'] else '✗'} {c['name']}: {c['detail']}" for c in res["checks"]]
    lines.append("All good." if res["ok"] else "Problems found — see above. Your latest backups are in the backups folder.")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    if len(argv) > 1:
        p = Path(argv[1]).expanduser()
        print(f"Checking backup {p.name}…")
        res = check_backup(p)
    else:
        from .backup import JOURNAL
        from .config import settings
        if (settings.data_dir / JOURNAL).exists():
            print("A restore was cut off part-way, so the data is between the old and the restored. Start Benchtop "
                  "(./benchtop), which finishes the restore first, then run this check again.")
            return 1
        print("Checking your Benchtop data…")
        res = check_live()
    print(report(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
