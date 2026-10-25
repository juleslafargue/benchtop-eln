"""Instrument folders: files an instrument saves into a watched folder are attached to the right experiment.

How it works
  * An admin adds a folder (Admin → Instruments): a name, a type, and who sees its unmatched files.
  * About once a minute Benchtop lists each folder. It only reads: nothing in a watched folder is ever moved,
    renamed or deleted. Folders are polled rather than watched for change events, because events don't
    arrive reliably from network drives, OneDrive or Dropbox; that's also why the same code works unchanged
    on a lab server (mount the share there and give its path).
  * What was already in a folder when it was added is noted and left alone ("baseline"), whatever its dates:
    a USB copy or a sync keeps a file's original date, so dates can't tell new from old.
  * An arrival is one file, or one dataset folder (a Bruker experiment number). It's handled once nothing in
    it has changed for the folder's quiet time (2 minutes by default), so half-copied files aren't taken.
  * Its name, folder names and (for Bruker) its title are searched for an experiment number such as
    EXP-00042, or a sample code handed to an experiment (JL-013, see samples.py). If they point to exactly one
    draft, the files are attached to its "Instrument data" section. Anything else waits in the Inbox with a
    note saying why. Benchtop never guesses.

Visibility follows Benchtop's sharing rules (see `can_see`): an arrival's file names can be revealing, so the
Inbox shows each person only what they're allowed to see.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from sqlalchemy import update
from sqlmodel import Session, select

from . import audit, storage
from .config import settings
from .models import (
    Attachment, Entry, EntryKind, EntryVersion, Experiment, ExperimentStatus, Instrument, InstrumentFile,
    Notification, Role, User, utcnow,
)

log = logging.getLogger("benchtop.instruments")

KINDS = {
    "bruker_nmr": "Bruker NMR",
    "agilent_nmr": "Agilent / Varian NMR",
    "chromatography": "LC-MS, HPLC or GC",
    "files": "Any files",
}
# vendor data that is a folder: Agilent ChemStation, OpenLab CDS and MassHunter, Bruker MS (.d); Waters MassLynx (.raw)
VENDOR_DIRS = (".d", ".raw")
# vendor data that is a file (Thermo Xcalibur/Chromeleon .raw, Sciex .wiff, Shimadzu .lcd/.qgd/.gcd, netCDF .cdf,
# .mzML) arrives as it is, like any other file in the folder, and so do PDF and CSV exports saved beside it
COMPANIONS = {".wiff": (".scan",), ".wiff2": (".scan",)}  # Sciex keeps the spectra in a .wiff.scan beside the .wiff
REPORTS = (".pdf", ".csv")  # inside a vendor folder: also attached on their own, so they open without the vendor software
SYSTEM_USERNAME = "@instruments"  # can't be a real username (no '@' allowed), so it never clashes
SECTION_TITLE = "Instrument data"
EID = re.compile(r"(?<![A-Za-z0-9])EXP-(\d{5,})(?!\d)", re.IGNORECASE)
MAX_DEPTH = 10
# never arrivals: system clutter, partial downloads and files still being written by common tools
_SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".localized", "icon\r"}
_SKIP_SUFFIXES = (".tmp", ".temp", ".part", ".partial", ".crdownload", ".download", ".icloud", ".lock", ".lck")


# ====================================================================== looking at a folder

def _skip(name: str) -> bool:
    low = name.lower()
    return name.startswith(".") or name.startswith("~$") or low in _SKIP_NAMES or low.endswith(_SKIP_SUFFIXES)


def discover(root: Path, kind: str) -> Iterator[tuple[str, Path]]:
    """(path inside the folder, full path) for each arrival currently there. Never follows links out."""
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not _skip(d))
        if len(here.parts) - root_depth >= MAX_DEPTH:
            dirnames[:] = []
        if kind == "bruker_nmr":
            if "acqus" in filenames and here != root and not (here / "acqus").is_symlink():
                dirnames[:] = []  # one experiment number: the whole folder is one arrival
                yield here.relative_to(root).as_posix(), here
            continue
        if kind == "agilent_nmr":
            for d in [d for d in dirnames if d.lower().endswith(".fid")]:
                dirnames.remove(d)
                if not (here / d).is_symlink():
                    yield (here / d).relative_to(root).as_posix(), here / d
            continue
        if kind == "chromatography":
            for d in [d for d in dirnames if d.lower().endswith(VENDOR_DIRS)]:
                dirnames.remove(d)  # a vendor data folder is one arrival, whatever is inside
                if not (here / d).is_symlink():
                    yield (here / d).relative_to(root).as_posix(), here / d
        for f in sorted(filenames):
            if _skip(f) or _is_companion(here, f):
                continue
            fp = here / f
            if fp.is_file() and not fp.is_symlink():
                yield fp.relative_to(root).as_posix(), fp


def _is_companion(here: Path, name: str) -> bool:
    """A file that belongs to another arrival (a Sciex .wiff.scan travels with its .wiff)."""
    low = name.lower()
    for main, sufs in COMPANIONS.items():
        for suf in sufs:
            if low.endswith(main + suf) and (here / name[: -len(suf)]).is_file():
                return True
    return False


def _companions(p: Path) -> list[Path]:
    out = []
    for suf in COMPANIONS.get(p.suffix.lower(), ()):
        c = p.with_name(p.name + suf)
        if c.is_file() and not c.is_symlink():
            out.append(c)
    return out


def _files_in(p: Path) -> list[Path]:
    """The files of an arrival. Links are never followed: someone who can write to an instrument share
    mustn't be able to make Benchtop copy a file from elsewhere on the computer (its secret key, say)."""
    if p.is_file():
        return [] if p.is_symlink() else [p, *_companions(p)]
    out = []
    for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            fp = Path(dirpath) / f
            if not f.startswith(".") and f.lower() not in _SKIP_NAMES and not fp.is_symlink() and fp.is_file():
                out.append(fp)
    return out


def fingerprint(p: Path) -> tuple[str, int, float]:
    """(fingerprint, total size, newest modification time) of a file or a folder's contents."""
    h = hashlib.sha1()
    total, newest = 0, 0.0
    for f in _files_in(p):
        st = f.stat()
        total += st.st_size
        newest = max(newest, st.st_mtime)
        h.update(f"{f.relative_to(p) if p.is_dir() else f.name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest(), total, newest


def describe_text(p: Path, kind: str) -> str:
    """Extra text that may name the experiment: what was typed on the instrument as the title or sample name."""
    def read(t: Path) -> str:
        try:
            return t.read_bytes()[:16384].decode("latin-1")
        except OSError:
            return ""
    if kind == "bruker_nmr" and p.is_dir():
        return read(p / "pdata" / "1" / "title") or read(p / "title")
    if kind == "agilent_nmr" and p.is_dir():
        return " ".join([read(p / "text"), *_procpar(read(p / "procpar"), ("samplename", "comment", "notebook", "page"))])
    return ""


def _procpar(text: str, names: tuple[str, ...]) -> list[str]:
    """String values of some VnmrJ parameters: a header line starting with the name, then `1 "value"`."""
    out, lines = [], text.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.split(" ", 1)[0] in names:
            m = re.match(r'\s*\d+\s+"(.*)"', lines[i + 1])
            if m:
                out.append(m.group(1))
    return out


# ====================================================================== storing what arrived

def _incoming() -> Path:
    return storage.incoming()  # outside the attachments folder, so a backup never picks up half a file


def blob_path(sha: str) -> Path:
    return storage.blob_path(sha)


def _store(tmp: Path) -> tuple[str, int]:
    """Move a finished temporary file into the content-addressed attachment store."""
    return storage.put(tmp)


def _tmp_name() -> Path:
    return _incoming() / f"{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}.part"


def _copy(src: Path, name: str) -> dict[str, Any]:
    from . import diskspace
    diskspace.require_room(src.stat().st_size)  # left in the folder, and tried again, while the disk is nearly full
    tmp = _tmp_name()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        sha, size = _store(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return {"name": name, "sha256": sha, "size": size, "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream"}


def _zip(files: list[tuple[Path, str]], name: str, compress: bool) -> dict[str, Any]:
    from . import diskspace
    diskspace.require_room(sum(f.stat().st_size for f, _ in files))
    tmp = _tmp_name()
    try:
        # raw vendor data is stored as it is: it barely compresses, and gigabytes would take minutes
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED, allowZip64=True) as z:
            for f, arc in files:
                z.write(f, arc)
        sha, size = _store(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return {"name": name, "sha256": sha, "size": size, "content_type": "application/zip"}


def pack(rel: str, p: Path, kind: str) -> list[dict[str, Any]]:
    """Copy an arrival into Benchtop's store, streaming (never whole files in memory).
      * a file: as it is; with its companions (Sciex .wiff + .wiff.scan), as one zip;
      * a dataset folder: one zip. A Bruker experiment number goes in as <sample>/<expno>/…, which the spectrum
        viewer opens directly. PDF and CSV reports inside an LC/GC vendor folder are also attached on their own."""
    if p.is_file():
        extra = _companions(p)
        if not extra:
            return [_copy(p, p.name)]
        return [_zip([(f, f.name) for f in [p, *extra]], f"{p.name}.zip", compress=False)]
    if kind == "bruker_nmr":
        parent = Path(rel).parent.name
        top = Path(parent) / p.name if parent else Path(p.name)
        name = f"{parent}_{p.name}.zip" if parent else f"{p.name}.zip"
    else:
        top, name = Path(p.name), f"{p.name}.zip"
    files = _files_in(p)
    out = [_zip([(f, (top / f.relative_to(p)).as_posix()) for f in files], name, compress=kind != "chromatography")]
    if kind == "chromatography":
        reports = [f for f in files if f.suffix.lower() in REPORTS and len(f.relative_to(p).parts) <= 3
                   and f.stat().st_size <= 100_000_000][:20]
        out += [_copy(f, f"{p.name} {f.name}") for f in reports]
    return out


# ====================================================================== deciding where it goes

@dataclass
class Decision:
    action: str  # attach | inbox
    note: str = ""
    experiment: Optional[Experiment] = None
    matched_ids: list[int] = field(default_factory=list)
    rule: str = ""
    person_id: Optional[int] = None  # whose sample code it carries (an unknown number of theirs)


def experiment_numbers(*texts: str) -> list[int]:
    seen: list[int] = []
    for t in texts:
        for m in EID.finditer(t or ""):
            n = int(m.group(1))
            if n not in seen:
                seen.append(n)
    return seen


def decide(session: Session, rel: str, extra: str = "") -> Decision:
    """Where an arrival goes. The rules, in order; the first that fits wins, and anything unclear waits:
      1. an experiment number (EXP-00042) in its name, folder names or title;
      2. a sample code handed to an experiment (JL-013);
         (both may be there, if they agree)
      3. a known prefix with a number never handed out (JL-099) → that person's Inbox;
      4. otherwise → the Inbox, with a note saying why."""
    from . import samples
    nums = experiment_numbers(rel, extra)
    codes = samples.find(session, rel, extra)
    if len(nums) > 1:
        found = [e.id for e in (session.get(Experiment, n) for n in nums) if e and not e.is_template]
        names = " and ".join(f"EXP-{n:05d}" for n in nums[:4])
        return Decision("inbox", f"The name mentions more than one experiment ({names}), so Benchtop didn't choose.",
                        matched_ids=found)
    known = [c for c in codes if c.code is not None]
    targets = sorted({*nums, *(c.code.experiment_id for c in known)})
    if len(targets) > 1:
        parts = [f"EXP-{n:05d}" for n in nums] + [f"{c.text} (EXP-{c.code.experiment_id:05d})" for c in known]
        return Decision("inbox", f"The name points to more than one experiment ({', '.join(parts[:4])}), so Benchtop didn't choose.",
                        matched_ids=[t for t in targets if (e := session.get(Experiment, t)) and not e.is_template])
    if len(targets) == 1:
        exp = session.get(Experiment, targets[0])
        eid = f"EXP-{targets[0]:05d}"
        via = eid if nums else f"{known[0].text} ({eid})"
        rule = "eid" if nums else "sample"
        if exp is None or exp.is_template:
            return Decision("inbox", f"There's no experiment {eid}. Check the number, or assign it to the right one.")
        if exp.status == ExperimentStatus.open:
            return Decision("attach", experiment=exp, matched_ids=[exp.id], rule=rule)
        if exp.status == ExperimentStatus.voided:
            return Decision("inbox", f"{via} was voided, so nothing more is added to it.", matched_ids=[exp.id])
        return Decision("inbox", f"Matched {via}, but it's already signed, so it wasn't attached. "
                                 "Attach it to a follow-up experiment.", matched_ids=[exp.id])
    unknown = [c for c in codes if c.code is None and c.owner_id is not None]
    if unknown:
        c = unknown[0]
        return Decision("inbox", f"{c.text} isn't a sample code that's been given to an experiment yet. Assign it, "
                                 f"and next time use “New sample code” on the experiment first.", person_id=c.owner_id)
    return Decision("inbox", "No experiment number (like EXP-00042) or sample code in the name.")


# ====================================================================== attaching

def system_user(session: Session) -> User:
    """The account instrument files arrive under. It can't sign in, and isn't listed with people."""
    u = session.exec(select(User).where(User.username == SYSTEM_USERNAME)).first()
    if u is None:
        u = User(username=SYSTEM_USERNAME, full_name="Instruments", role=Role.viewer, active=False,
                 password_hash="!disabled")
        session.add(u)
        session.flush()
    return u


class NotOpen(Exception):
    """The experiment was signed (or voided) before the files could be attached."""


def _claim_open(session: Session, exp: Experiment) -> None:
    res = session.execute(update(Experiment).where(Experiment.id == exp.id, Experiment.status == ExperimentStatus.open)
                          .values(status=ExperimentStatus.open).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise NotOpen(exp.eid)


def instrument_section(session: Session, exp: Experiment, actor: User) -> Entry:
    """The experiment's "Instrument data" section, made at the end if it has none yet."""
    for e in session.exec(select(Entry).where(Entry.experiment_id == exp.id, Entry.deleted == False,  # noqa: E712
                                              Entry.kind == EntryKind.file).order_by(Entry.position, Entry.id)):
        if (e.content or {}).get("source") == "instruments":
            return e
    last = session.exec(select(Entry.position).where(Entry.experiment_id == exp.id).order_by(Entry.position.desc())).first()
    content = {"attachment_ids": [], "caption": "", "source": "instruments"}
    e = Entry(experiment_id=exp.id, kind=EntryKind.file, title=SECTION_TITLE, content=content,
              position=(last if last is not None else -1) + 1, updated_by=actor.id)
    session.add(e)
    session.flush()
    session.add(EntryVersion(entry_id=e.id, version=1, title=e.title, content=content, created_by=actor.id))
    audit.record(session, actor, "create", "entry", e.id, experiment=exp.eid, kind="file", title=e.title)
    return e


def _set_section_ids(session: Session, e: Entry, ids: list[int], actor: User, exp: Experiment) -> None:
    content = {**(e.content or {}), "attachment_ids": ids}
    e.content = content
    e.version += 1
    e.updated_by, e.updated_at = actor.id, utcnow()
    session.add_all([e, EntryVersion(entry_id=e.id, version=e.version, title=e.title, content=content, created_by=actor.id)])
    audit.record(session, actor, "update", "entry", e.id, experiment=exp.eid, version=e.version,
                 content_sha256=hashlib.sha256(audit.canonical(content).encode()).hexdigest())


def attach(session: Session, row: InstrumentFile, inst: Instrument, exp: Experiment, actor: User, rule: str) -> list[int]:
    """Attach an arrival's files to a draft experiment. Raises NotOpen if it's no longer a draft.
    A file already in the record (the same content: renamed, or copied in twice) isn't added again.
    Returns the ids of attachments added. The caller commits; it's one transaction with its audit events."""
    audit._take_write_lock(session)
    _claim_open(session, exp)
    session.refresh(exp)
    already = {a.sha256: a.id for a in session.exec(select(Attachment).where(
        Attachment.experiment_id == exp.id, Attachment.withdrawn_at == None))}  # noqa: E711
    ids, added = [], []
    for f in row.files:
        if f["sha256"] in already:
            ids.append(already[f["sha256"]])
            continue
        a = Attachment(experiment_id=exp.id, filename=f["name"], content_type=f["content_type"], size=f["size"],
                       sha256=f["sha256"], uploaded_by=actor.id, instrument_file_id=row.id)
        session.add(a)
        session.flush()
        audit.record(session, actor, "upload", "attachment", a.id, experiment=exp.eid, filename=a.filename,
                     sha256=a.sha256, size=a.size, instrument=inst.name, path=row.path)
        already[a.sha256] = a.id
        ids.append(a.id)
        added.append(a.id)
    if added:
        sec = instrument_section(session, exp, actor)
        _set_section_ids(session, sec, [*(sec.content or {}).get("attachment_ids", []), *added], actor, exp)
        exp.updated_at = utcnow()
        session.add(exp)
    else:
        rule = "duplicate"
        row.note = f"The same file is already in {exp.eid}, so it wasn't added again."
    row.state, row.experiment_id, row.attachment_ids, row.rule = "attached", exp.id, ids, rule
    row.handled_at = utcnow()
    session.add(row)
    audit.record(session, actor, "instrument_attach", "experiment", exp.id, experiment=exp.eid, instrument=inst.name,
                 path=row.path, revision=row.revision, rule=rule, files=[f["sha256"] for f in row.files])
    return added


def notify(session: Session, user_id: int, text: str, experiment_id: Optional[int] = None, kind: str = "instrument") -> None:
    session.add(Notification(user_id=user_id, kind=kind, actor_id=system_user(session).id,
                             experiment_id=experiment_id, text=text))


def notify_attached(session: Session, exp: Experiment, inst: Instrument, row: InstrumentFile) -> None:
    """Tell the record's owner, once: while their notification is unread, later arrivals update it rather than
    adding one per file (an LC sequence can bring hundreds)."""
    since = utcnow() - timedelta(hours=12)
    n = session.exec(select(Notification).where(Notification.user_id == exp.owner_id, Notification.kind == "instrument",
                                                Notification.experiment_id == exp.id, Notification.read_at == None,  # noqa: E711
                                                Notification.created_at >= since).order_by(Notification.id.desc())).first()
    if n is None:
        k = len(row.files)
        session.add(Notification(user_id=exp.owner_id, kind="instrument", actor_id=system_user(session).id,
                                 experiment_id=exp.id, created_at=row.handled_at or utcnow(),  # later runs count from here
                                 text=f"attached {k} file{'s' if k != 1 else ''} from {inst.name} ({row.path}) to {exp.eid}"))
        return
    count = 1 + len(session.exec(select(InstrumentFile.id).where(  # this one isn't saved yet
        InstrumentFile.experiment_id == exp.id, InstrumentFile.state == "attached", InstrumentFile.rule != "duplicate",
        InstrumentFile.handled_at >= n.created_at, InstrumentFile.id != row.id)).all())
    n.text = f"attached files from {count} instrument runs to {exp.eid} (the latest: {row.path} from {inst.name})"
    session.add(n)


# ====================================================================== who sees an Inbox item

def can_see(session: Session, user: User, row: InstrumentFile, inst: Instrument) -> bool:
    """* carries someone's sample code → that person (and admins);
       * names experiments (signed, voided, or more than one) → people who can read one of them;
       * matches nothing → the folder's audience: everyone, or members of its groups."""
    if user.role == Role.admin:
        return True
    from .access import access_for
    acc = access_for(session, user)
    if row.person_id is not None:
        return row.person_id == user.id
    if row.matched_ids:
        return any(acc.can_read(e) for e in (session.get(Experiment, i) for i in row.matched_ids) if e)
    if not inst.audience:
        return True
    return any(g in acc.groups for g in inst.audience)


# ====================================================================== the scan

def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _handle(session: Session, row: InstrumentFile, inst: Instrument, p: Path, now: datetime) -> str:
    """A settled arrival: copy it in, decide, attach or send to the Inbox, and commit.

    The copying happens with no database transaction open, so a 2 GB run never holds up anyone's saves; the
    database work afterwards is one short transaction. Unreadable files (a sync still downloading them) raise
    OSError before anything is written. Returns what happened: handled | changed."""
    max_bytes = settings.instrument_max_mb * 1024 * 1024
    if row.size > max_bytes:
        row.state, row.files = "inbox", []
        row.note = (f"It's {row.size / 1e6:,.0f} MB, more than the {settings.instrument_max_mb:,} MB limit, so it wasn't "
                    "copied into Benchtop. Raise BENCHTOP_INSTRUMENT_MAX_MB, or keep it on the instrument drive.")
        session.add(row)
        session.commit()
        return "handled"
    files = pack(row.path, p, inst.kind)
    after, size, _ = fingerprint(p)
    if after != row.fingerprint:  # it was still being written after all: take it once it's quiet again
        row.fingerprint, row.size, row.changed_at = after, size, now
        session.add(row)
        session.commit()
        return "changed"
    extra = describe_text(p, inst.kind)
    row.files = files
    d = decide(session, row.path, extra)
    row.matched_ids = d.matched_ids
    if d.action == "attach" and d.experiment is not None:
        try:
            added = attach(session, row, inst, d.experiment, system_user(session), d.rule)
            if added:
                notify_attached(session, d.experiment, inst, row)
            session.commit()
            return "handled"
        except NotOpen:
            session.rollback()
            row = session.get(InstrumentFile, row.id)
            row.files = files
            d = Decision("inbox", f"Matched {d.experiment.eid}, but it was signed just before the files arrived, so "
                                  "they weren't attached. Attach them to a follow-up experiment.", matched_ids=[d.experiment.id])
            row.matched_ids = d.matched_ids
    row.state, row.note, row.person_id = "inbox", d.note, d.person_id
    session.add(row)
    if d.person_id is not None:
        notify(session, d.person_id, f"has a file from {inst.name} ({row.path}) with your sample code waiting in your Inbox",
               kind="inbox")
    elif len(d.matched_ids) == 1:  # the record's owner hears about it; unmatched files just wait in the Inbox
        exp = session.get(Experiment, d.matched_ids[0])
        if exp:
            notify(session, exp.owner_id, f"has a file from {inst.name} ({row.path}) for {exp.eid} waiting in your Inbox",
                   kind="inbox")
    session.commit()
    return "handled"


def scan(session: Session, inst: Instrument, now: Optional[datetime] = None) -> dict[str, Any]:
    """Look at one folder once. Returns counts of what happened, and any arrivals that couldn't be read.

    Two phases, so nothing is held for long: first note what's there (committed in batches: a first look at a
    departmental drive with tens of thousands of files mustn't lock the database), then handle each arrival
    that has been quiet long enough, each in its own short transaction."""
    now = now or utcnow()
    root = Path(inst.folder)
    counts: dict[str, Any] = {"seen": 0, "new": 0, "handled": 0, "errors": 0, "problems": []}
    if not root.is_dir():
        raise FileNotFoundError(f"{root} isn't there. If it's a network or external drive, check it's connected.")
    first = inst.baseline_at is None  # the first look: whatever is there already is noted, not taken
    quiet = timedelta(seconds=inst.settle_seconds)
    inst_id, kind, since = inst.id, inst.kind, inst.import_since
    latest: dict[str, InstrumentFile] = {}
    for r in session.exec(select(InstrumentFile).where(InstrumentFile.instrument_id == inst_id)
                          .order_by(InstrumentFile.revision)):
        latest[r.path] = r
    seen: set[str] = set()
    due: list[tuple[InstrumentFile, Path]] = []
    session.autoflush = False  # new rows are written only at each commit, so the write lock is held only then
    for rel, p in discover(root, kind):
        seen.add(rel)
        counts["seen"] += 1
        if counts["seen"] % 500 == 0:  # commit as we go: a first look at a big drive mustn't hold the database
            session.commit()
        try:
            fp, size, newest = fingerprint(p)
        except OSError as exc:
            counts["errors"] += 1
            counts["problems"].append((rel, exc))
            continue
        row = latest.get(rel)
        if row is None:
            row = InstrumentFile(instrument_id=inst_id, path=rel, fingerprint=fp, size=size, first_seen_at=now,
                                 changed_at=now, state="baseline" if first else "settling")
            session.add(row)
            latest[rel] = row
            if not first:
                counts["new"] += 1
        if row.state == "baseline":
            if since and _utc(newest) >= since:
                # older data the admin asked for: already quiet, so it's taken now
                row.state, row.fingerprint, row.size, row.changed_at = "settling", fp, size, now - quiet
                counts["new"] += 1
            elif row.fingerprint != fp:  # something already there changed after Benchtop noted it: that's new data
                row.state, row.fingerprint, row.size, row.changed_at = "settling", fp, size, now
                counts["new"] += 1
                continue
            else:
                continue
        elif row.fingerprint != fp:
            if row.state in ("settling", "vanished"):  # still being written or copied (or back again)
                row.state, row.fingerprint, row.size, row.changed_at = "settling", fp, size, now
            else:  # it changed after being handled: the change arrives again, as the next revision
                nxt = InstrumentFile(instrument_id=inst_id, path=rel, revision=row.revision + 1, fingerprint=fp,
                                     size=size, first_seen_at=now, changed_at=now, state="settling")
                session.add(nxt)
                latest[rel] = nxt
                counts["new"] += 1
            continue
        if row.state == "settling" and now - row.changed_at >= quiet:
            due.append((row, p))
    for rel, row in latest.items():  # gone before it was taken (deleted, or renamed while being copied)
        if row.state == "settling" and rel not in seen:
            row.state = "vanished"
    if first:
        inst.baseline_at = now
        session.add(inst)
    session.commit()  # the bookkeeping is done; nothing is held while files are copied
    session.autoflush = True
    for row, p in due:
        rel = row.path
        try:
            if _handle(session, row, inst, p, now) == "handled":
                counts["handled"] += 1
        except OSError as exc:  # unreadable for now (a sync still downloading it, a full disk): try next time
            session.rollback()
            log.warning("Couldn't take %s from %s: %s", rel, inst.name, exc)
            counts["errors"] += 1
            counts["problems"].append((rel, exc))
        except Exception as exc:  # noqa: BLE001 - one bad arrival mustn't stop the others
            session.rollback()
            log.exception("Couldn't take %s from %s", rel, inst.name)
            counts["errors"] += 1
            counts["problems"].append((rel, exc))
    return counts


def arrival_problem(problems: list[tuple[str, BaseException]]) -> str:
    """What the Admin page says when arrivals couldn't be taken (the folder itself is fine)."""
    if not problems:
        return ""
    rel, exc = problems[0]
    more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
    from .diskspace import DiskFull, reserve_bytes, fmt
    if isinstance(exc, DiskFull):
        return (f"The disk Benchtop keeps its data on is nearly full, so new instrument files aren't copied in: Benchtop "
                f"keeps the last {fmt(reserve_bytes())} for saving. Free some space; Benchtop tries again every minute.")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return ("The disk Benchtop keeps its data on is full, so new instrument files can't be copied in. Free some "
                "space; Benchtop tries again every minute.")
    if isinstance(exc, PermissionError):
        return f"Benchtop isn't allowed to read “{rel}”{more}. Check the file's permissions on the instrument share."
    if isinstance(exc, OSError):
        return (f"Couldn't read “{rel}”{more} yet: {exc.strerror or exc}. If it's still syncing (OneDrive, a slow copy) "
                "this clears by itself; Benchtop tries again every minute.")
    return f"Something went wrong taking “{rel}”{more}: {exc}. Benchtop tries again every minute."


_scan_lock = threading.Lock()


def scan_all(now: Optional[datetime] = None) -> dict[int, dict[str, Any]]:
    """Look at every enabled folder once. Each folder is its own transaction, so one broken folder doesn't
    hold up the others; its problem shows on the Admin page."""
    from .db import engine
    out: dict[int, dict[str, Any]] = {}
    with _scan_lock:
        _clear_stale_incoming()
        with Session(engine) as s:
            ids = [i.id for i in s.exec(select(Instrument).where(Instrument.enabled == True,  # noqa: E712
                                                                 Instrument.archived == False))]  # noqa: E712
        for iid in ids:
            with Session(engine) as s:
                inst = s.get(Instrument, iid)
                t0 = time.monotonic()
                try:
                    res: dict[str, Any] = scan(s, inst, now)
                    inst = s.get(Instrument, iid)
                    inst.last_error = arrival_problem(res.pop("problems", []))
                except Exception as exc:  # noqa: BLE001 - reported on the Admin page, never stops the others
                    s.rollback()
                    inst = s.get(Instrument, iid)
                    res = {"error": folder_problem(exc)}
                    inst.last_error = res["error"]
                inst.last_scan_at = now or utcnow()
                inst.last_scan_ms = int((time.monotonic() - t0) * 1000)
                s.add(inst)
                s.commit()
                out[iid] = res
    return out


def _clear_stale_incoming() -> None:
    """Half-copied files left by an interrupted import (Benchtop stopped, the Mac turned off) are removed after a day."""
    cutoff = time.time() - 86400
    for f in _incoming().glob("*.part"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


def folder_problem(exc: BaseException) -> str:
    """A folder error in words that say what to do."""
    if isinstance(exc, PermissionError):
        return ("Benchtop isn't allowed to read this folder. On a Mac: System Settings → Privacy & Security → "
                "Files and Folders (or Full Disk Access), allow Terminal, then restart Benchtop.")
    if isinstance(exc, FileNotFoundError):
        return str(exc) if "isn't there" in str(exc) else "The folder isn't there. If it's on a network or external drive, check it's connected."
    return f"Couldn't read the folder: {exc}"


# ====================================================================== checking a folder before using it

def validate_folder(raw: str) -> Path:
    raw = (raw or "").strip()
    if not raw or len(raw) > 1024:
        raise ValueError("Give the full path of the folder, e.g. /Volumes/NMR/data/ruizlab")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("Give the full path of the folder, starting with / (e.g. /Volumes/NMR/data/ruizlab)")
    p = p.resolve()
    data = settings.data_dir.resolve()
    if p == data or data in p.parents or p in data.parents:
        raise ValueError("Choose a folder outside Benchtop's own data folder")
    try:
        if not p.is_dir():
            raise ValueError(f"{p} isn't there. If it's on a network or external drive, check it's connected.")
        next(iter(os.scandir(p)), None)
    except PermissionError as exc:
        raise ValueError(folder_problem(exc)) from None
    return p


def preview(session: Session, folder: str, kind: str, import_since: Optional[datetime] = None, limit: int = 12) -> dict[str, Any]:
    """What Benchtop would do with a folder, without doing any of it."""
    root = validate_folder(folder)
    t0 = time.monotonic()
    items = []
    for rel, p in discover(root, kind):
        try:
            _, size, newest = fingerprint(p)
        except OSError:
            continue
        items.append((newest, rel, p, size))
        if len(items) >= 50_000:
            break
    seconds = time.monotonic() - t0
    items.sort(reverse=True)
    def outcome(rel: str, p: Path) -> dict[str, Any]:
        d = decide(session, rel, describe_text(p, kind))
        return {"path": rel, "action": d.action, "note": d.note,
                "experiment": d.experiment.eid if d.experiment else None}
    latest = [{**outcome(rel, p), "size": size, "modified": _utc(t).isoformat() + "Z"}
              for t, rel, p, size in items[:limit]]
    out: dict[str, Any] = {"folder": str(root), "items": len(items), "scan_seconds": round(seconds, 2), "latest": latest,
                           "hints": _hints(root, seconds)}
    if import_since:
        old = [(rel, p) for t, rel, p, _ in items if _utc(t) >= import_since]
        res = [outcome(rel, p) for rel, p in old[:5000]]
        out["since"] = {"items": len(old), "attach": sum(r["action"] == "attach" for r in res),
                        "inbox": sum(r["action"] == "inbox" for r in res)}
    return out


def old_items(session: Session, inst: Instrument, q: str = "", offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """What was already in the folder when it was added (left alone), each with what would happen if it were taken
    now, so an admin can pick the ones to bring in. Sorted by path; `q` filters on it."""
    from sqlalchemy import func
    cond = [InstrumentFile.instrument_id == inst.id, InstrumentFile.state == "baseline"]
    if q.strip():
        cond.append(func.lower(InstrumentFile.path).contains(q.strip().lower(), autoescape=True))
    total = session.exec(select(func.count()).select_from(InstrumentFile).where(*cond)).one()
    rows = session.exec(select(InstrumentFile).where(*cond).order_by(InstrumentFile.path)
                        .offset(max(0, offset)).limit(max(1, min(limit, 200)))).all()
    root = Path(inst.folder)
    items = []
    for r in rows:
        p = root / r.path
        try:
            if not p.exists():
                raise FileNotFoundError(p)
            _, size, newest = fingerprint(p)
        except OSError:  # gone, or the drive isn't there: nothing to take
            items.append({"path": r.path, "gone": True, "size": r.size})
            continue
        d = decide(session, r.path, describe_text(p, inst.kind))
        items.append({"path": r.path, "gone": False, "size": size, "modified": _utc(newest).isoformat() + "Z",
                      "action": d.action, "note": d.note, "experiment": d.experiment.eid if d.experiment else None})
    return {"total": total, "offset": offset, "items": items}


MAX_TAKE = 1000


def take_old(session: Session, inst: Instrument, paths: list[str]) -> dict[str, Any]:
    """Bring in some of what was already there: each becomes an ordinary arrival, taken at the next look (and
    matched like any other: attached to a draft it names, or waiting in the Inbox). Unchanged since Benchtop
    noted it, it's already quiet, so it doesn't wait the quiet time again."""
    now = utcnow()
    root = Path(inst.folder)
    wanted = list(dict.fromkeys(p for p in paths if isinstance(p, str)))[:MAX_TAKE]
    taken, gone, not_old = [], [], []
    with _scan_lock:  # not while a look at the folders is half-way through its bookkeeping
        for rel in wanted:
            row = session.exec(select(InstrumentFile).where(InstrumentFile.instrument_id == inst.id, InstrumentFile.path == rel)
                               .order_by(InstrumentFile.revision.desc())).first()
            if row is None or row.state != "baseline":
                not_old.append(rel)
            elif not (root / rel).exists():
                gone.append(rel)
            else:
                row.state, row.changed_at = "settling", now - timedelta(seconds=inst.settle_seconds)
                session.add(row)
                taken.append(rel)
        session.commit()
    return {"taken": taken, "gone": gone, "not_old": not_old}


def _hints(root: Path, seconds: float) -> list[str]:
    hints = []
    s = str(root)
    if "/Library/CloudStorage/OneDrive" in s or "/OneDrive" in s:
        hints.append("OneDrive folder: in Finder, right-click it and choose “Always Keep on This Device”, "
                     "so files are really on this Mac when Benchtop reads them.")
    if "/Library/Mobile Documents/" in s:
        hints.append("iCloud Drive folder: turn off “Optimise Mac Storage” for it, or files may not be on this Mac.")
    if seconds > 20:
        hints.append(f"Looking through this folder took {seconds:.0f} s. Choose a smaller folder (for example your "
                     "group's own subfolder) so new files are noticed quickly.")
    return hints


# ====================================================================== running in the background

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    while not _stop.is_set():
        try:
            scan_all()
        except Exception:  # noqa: BLE001 - keep watching
            log.exception("Instrument folder scan failed")
        _stop.wait(max(5, settings.instrument_scan_seconds))


def start() -> None:
    global _thread
    if settings.instrument_scan_seconds <= 0 or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="benchtop-instruments", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
