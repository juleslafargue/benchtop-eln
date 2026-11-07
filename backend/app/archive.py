"""The archive PDF (0.34): one archival PDF (PDF/A-3b) of the records signed over a span of days, made to be printed
and kept: a year, a month, a week ("one week of records from one notebook").

What goes in: records signed from one day to another (both included, in the person's own calendar days: the browser
sends the instants its days start and end) that the person may open, optionally from one notebook and/or by one
author, in the order they were signed. Witnessed and awaiting-witness records both count. Voided records go in,
marked VOIDED with the reason and date (one voided before it was ever signed goes by the date it was voided). Drafts,
templates and records imported read-only from other notebooks are left out.

How it runs, as a job kept in a scratch folder of Benchtop's data folder (archive-tmp/, cleaned up after a day):
1. The page starts a job; the answer lists the records and every characterisation file to draw.
2. The browser draws each spectrum as the viewer does (spectrumDraw.ts) and uploads the PNG to the job. Each upload is
   checked (a file of a record in this job, a real PNG, not too big) and only ever used for this archive.
3. The page asks for the PDF; it's built in the background (one archive at a time), then downloaded.
Only the person who started a job can see, feed or download it. If Benchtop stops while a job is open, the job says
it stopped and can be started again.

Every record is only read. The PDF carries, inside it, each record's data (its snapshot, fingerprint and signatures)
and its timestamp proofs, as PDF/A-3 associated files.
"""
from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlmodel import Session, select

from . import __version__, pdfa
from .access import access_for
from .config import settings
from .models import (Attachment, EntryKind, Experiment, ExperimentStatus, Notebook, Signature, SignatureTimestamp, User,
                     utcnow)
from .services import content_hash, experiment_summary, live_entries, signatures_out, snapshot

log = logging.getLogger("benchtop.archive")

KEEP_SECONDS = 86400          # a job and its PDF are deleted a day after they were last touched
MAX_PNG_BYTES = 8_000_000     # one uploaded spectrum picture
MAX_SIDE = 6000               # pixels, either way
ACTIVE = ("drawing", "queued", "building")
_RUN = secrets.token_hex(6)   # this run of Benchtop: a job left open by an earlier run has stopped
_LOCK = threading.RLock()
_BUILD = threading.Semaphore(1)  # one archive is laid out at a time; others wait their turn
_ID = re.compile(r"^[0-9a-f]{16}$")
PAGE_SIZES = {"letter": "US Letter", "a4": "A4"}


class ArchiveError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ====================================================================== the days it covers

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December")
MAX_DAYS = 366 * 50


def _day(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _naive_utc(v: datetime) -> datetime:
    return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v


@dataclass(frozen=True)
class Span:
    """From one day to another, both included. `start` and `end` are when the first day starts and the day after the
    last starts, in UTC (as signatures are stored), as the person's own clock has them; `tz` names that clock."""
    first: date
    last: date
    start: datetime
    end: datetime
    tz: str = "UTC"

    @classmethod
    def make(cls, first: date, last: date, start: datetime | None = None, end: datetime | None = None, tz: str = "") -> "Span":
        if first > last:
            raise ArchiveError(422, "“From” is later than “To”: choose a “From” day on or before the “To” day")
        if (last - first).days > MAX_DAYS:
            raise ArchiveError(422, "That's more than 50 years: choose a shorter span")
        midnight = lambda d: datetime(d.year, d.month, d.day)  # noqa: E731
        try:
            s = _naive_utc(start) if start else midnight(first)
            e = _naive_utc(end) if end else midnight(last + timedelta(days=1))
            # the browser's own midnights are within a day of UTC's (time zones run from −12 to +14 hours)
            ok = abs(s - midnight(first)) <= timedelta(hours=15) and abs(e - midnight(last + timedelta(days=1))) <= timedelta(hours=15)
        except (OverflowError, ValueError):
            ok = False
        if not ok or s >= e:
            raise ArchiveError(422, "The start and end times don't match the days chosen")
        tz = re.sub(r"[^A-Za-z0-9_+\-/]", "", tz or "")[:64] or ("UTC" if not start else "local time")
        return cls(first, last, s, e, tz)

    @classmethod
    def year(cls, y: int) -> "Span":
        return cls.make(date(y, 1, 1), date(y, 12, 31))

    def holds(self, when: datetime) -> bool:
        return self.start <= when < self.end

    @property
    def whole_year(self) -> bool:
        return self.first == date(self.first.year, 1, 1) and self.last == date(self.first.year, 12, 31)

    def label(self) -> str:
        """2026; November 2026; 3 November 2026; 3–9 November 2026; 28 October – 3 November 2026;
        15 December 2025 – 4 January 2026."""
        a, b = self.first, self.last
        if self.whole_year:
            return str(a.year)
        if a.day == 1 and a.year == b.year and a.month == b.month and b.day == calendar.monthrange(b.year, b.month)[1]:
            return f"{MONTHS[a.month - 1]} {a.year}"
        if a == b:
            return _day(a)
        if a.year == b.year and a.month == b.month:
            return f"{a.day}–{b.day} {MONTHS[a.month - 1]} {a.year}"
        if a.year == b.year:
            return f"{a.day} {MONTHS[a.month - 1]} – {_day(b)}"
        return f"{_day(a)} – {_day(b)}"

    def title(self) -> str:
        return f"Benchtop records {self.label()}" if self.whole_year else f"Benchtop records, {self.label()}"

    def phrase(self) -> str:
        """For a sentence: "in 2026", "on 3 November 2026", "from 3 to 9 November 2026"."""
        if self.whole_year:
            return f"in {self.first.year}"
        if self.first == self.last:
            return f"on {_day(self.first)}"
        return f"from {_day(self.first)} to {_day(self.last)}"

    def params(self) -> dict[str, str]:
        return {"from_date": self.first.isoformat(), "to_date": self.last.isoformat(), "start": self.start.isoformat() + "Z",
                "end": self.end.isoformat() + "Z", "tz": self.tz}

    @classmethod
    def from_params(cls, p: dict[str, Any]) -> "Span":
        return cls.make(date.fromisoformat(p["from_date"]), date.fromisoformat(p["to_date"]),
                        datetime.fromisoformat(p["start"].replace("Z", "+00:00")), datetime.fromisoformat(p["end"].replace("Z", "+00:00")),
                        p.get("tz", ""))


# ====================================================================== a record's proof, carried inside the PDF

def record_proof(session: Session, exp: Experiment) -> dict[str, Any]:
    """The record as GET /api/experiments/{id}/report?format=json gives it: what its signatures cover (snapshot), its
    fingerprint (content_hash, the SHA-256 of the snapshot) and its signatures."""
    return {"experiment": experiment_summary(session, exp), "snapshot": snapshot(session, exp),
            "content_hash": content_hash(session, exp), "signatures": signatures_out(session, exp.id)}


def proof_attachments(session: Session, exp: Experiment) -> list[pdfa.Attachment]:
    """The files a record's PDF carries: its data (EXP-00012.json) and, for each signature with a trusted timestamp,
    the same Proof file the record page offers (the token, the exact text timestamped, how to check it with openssl)."""
    from . import timestamps

    out = [pdfa.Attachment(f"{exp.eid}.json", json.dumps(record_proof(session, exp), indent=2, ensure_ascii=False).encode(),
                           "application/json",
                           f"{exp.eid}: the record's data — what its signatures cover (snapshot), its fingerprint "
                           "(content_hash) and its signatures", "Source")]
    sigs = list(session.exec(select(Signature).where(Signature.experiment_id == exp.id).order_by(Signature.id)))
    rows = {t.signature_id: t for t in session.exec(select(SignatureTimestamp).where(
        SignatureTimestamp.signature_id.in_([s.id for s in sigs] or [-1]), SignatureTimestamp.status == "ok"))}
    for s in sigs:
        row = rows.get(s.id)
        if row is None or not row.token:
            continue
        try:
            data = timestamps.proof_zip(session, s, row)
        except Exception:  # noqa: BLE001 - the signature itself is still in the record's data
            log.exception("Couldn't put the timestamp proof of signature %s in the PDF", s.id)
            continue
        out.append(pdfa.Attachment(f"{exp.eid}-signature-{s.id}-timestamp.zip", data, "application/zip",
                                   f"{exp.eid}: trusted timestamp (RFC 3161) of the {s.meaning} signature by "
                                   f"{s.signer_name or s.signer_username or ''}, checkable with openssl", "Supplement"))
    return out


# ====================================================================== which records

def _name(u: User | None) -> str:
    return (u.full_name or u.username) if u else "?"


def _when(d: datetime | None) -> str:
    return f"{d:%Y-%m-%d %H:%M} UTC" if d else ""


def record_dates(sigs: list[Signature], status: ExperimentStatus) -> dict[str, Any] | None:
    """The signatures that place a record in the archive: the author signature in force, its witness, the void.
    None for a record that doesn't go in (a draft)."""
    author = witness = void = None
    for s in sigs:  # in the order they were made
        if s.meaning == "author":
            author, witness = s, None
        elif s.meaning in ("withdrawn", "rejected"):  # back to a draft: that signature no longer stands
            author = witness = None
        elif s.meaning == "witness" and author is not None:
            witness = s
        elif s.meaning == "void":
            void = s
    if status == ExperimentStatus.voided and void is not None:
        return {"author": author, "witness": witness, "void": void, "date": (author or void).signed_at}
    if status in (ExperimentStatus.signed, ExperimentStatus.witnessed) and author is not None:
        return {"author": author, "witness": witness if status == ExperimentStatus.witnessed else None, "void": None,
                "date": author.signed_at}
    return None


def candidates(session: Session, user: User, notebook_id: int | None = None, author_id: int | None = None
               ) -> list[tuple[Experiment, dict[str, Any]]]:
    """Every record this person may open that is signed, witnessed or voided (any year), with its dates, in the order
    they were signed."""
    q = select(Experiment).where(Experiment.is_template == False,  # noqa: E712
                                 Experiment.status.in_([ExperimentStatus.signed, ExperimentStatus.witnessed, ExperimentStatus.voided]))
    if notebook_id is not None:
        q = q.where(Experiment.notebook_id == notebook_id)
    if author_id is not None:
        q = q.where(Experiment.owner_id == author_id)
    acc = access_for(session, user)
    exps = [e for e in session.exec(acc.filter(q)) if acc.can_read(e)]
    sigs: dict[int, list[Signature]] = {}
    ids = [e.id for e in exps]
    for k in range(0, len(ids), 500):
        for s in session.exec(select(Signature).where(Signature.experiment_id.in_(ids[k:k + 500])).order_by(Signature.id)):
            sigs.setdefault(s.experiment_id, []).append(s)
    out = []
    for e in exps:
        d = record_dates(sigs.get(e.id, []), e.status)
        if d is not None:
            out.append((e, d))
    out.sort(key=lambda t: (t[1]["date"], t[0].id))
    return out


def select_records(session: Session, user: User, span: Span, notebook_id: int | None = None, author_id: int | None = None
                   ) -> list[tuple[Experiment, dict[str, Any]]]:
    """The records signed within the span."""
    return [(e, d) for e, d in candidates(session, user, notebook_id, author_id) if span.holds(d["date"])]


def counts(rows: list[tuple[Experiment, dict[str, Any]]]) -> dict[str, int]:
    return {"records": len(rows),
            "witnessed": sum(1 for e, _ in rows if e.status == ExperimentStatus.witnessed),
            "awaiting_witness": sum(1 for e, _ in rows if e.status == ExperimentStatus.signed),
            "voided": sum(1 for e, _ in rows if e.status == ExperimentStatus.voided)}


def spectra_of(session: Session, exp: Experiment) -> list[dict[str, Any]]:
    """Every characterisation file of a record (once each, in the record's order), with what the browser needs to
    draw it as the viewer does: the saved analysis, and the product (for the automatic analysis when none is saved)."""
    from . import writeup

    entries = live_entries(session, exp.id)
    atts = {a.id: a for a in session.exec(select(Attachment).where(Attachment.experiment_id == exp.id,
                                                                   Attachment.withdrawn_at == None))}  # noqa: E711
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    product = None
    for e in entries:
        if e.kind != EntryKind.characterization:
            continue
        for it in (e.content or {}).get("items", []):
            if not isinstance(it, dict):
                continue
            aid = it.get("attachment_id")
            if not isinstance(aid, int) or aid not in atts or aid in seen:
                continue
            seen.add(aid)
            if product is None:
                product = writeup.draft(entries).get("product") or {}
            a = atts[aid]
            out.append({"attachment_id": aid, "experiment_id": exp.id, "eid": exp.eid, "filename": a.filename, "size": a.size,
                        "sha256": a.sha256, "technique": str(it.get("technique") or ""), "conditions": str(it.get("conditions") or ""),
                        "analysis": it.get("analysis") if isinstance(it.get("analysis"), dict) else None,
                        "product": product or None})
    return out


def scope_text(session: Session, notebook_id: int | None, author_id: int | None) -> str:
    bits = []
    if notebook_id is not None:
        nb = session.get(Notebook, notebook_id)
        bits.append(f"notebook “{nb.name}”" if nb else f"notebook {notebook_id}")
    if author_id is not None:
        bits.append(f"records by {_name(session.get(User, author_id))}")
    text = ", ".join(bits)
    return text[:1].upper() + text[1:] if bits else "All records you can open"


def options(session: Session, user: User) -> dict[str, Any]:
    """For the page: the years with records, and the notebooks and authors they come from (any date)."""
    rows = candidates(session, user)
    years: dict[int, int] = {}
    nbs: dict[int, str] = {}
    authors: dict[int, str] = {}
    for e, d in rows:
        years[d["date"].year] = years.get(d["date"].year, 0) + 1
        if e.notebook_id is not None and e.notebook_id not in nbs:
            nb = session.get(Notebook, e.notebook_id)
            nbs[e.notebook_id] = nb.name if nb else f"Notebook {e.notebook_id}"
        if e.owner_id not in authors:
            authors[e.owner_id] = _name(session.get(User, e.owner_id))
    return {"years": [{"year": y, "records": n} for y, n in sorted(years.items(), reverse=True)],
            "notebooks": [{"id": i, "name": n} for i, n in sorted(nbs.items(), key=lambda t: t[1].lower())],
            "authors": [{"id": i, "name": n} for i, n in sorted(authors.items(), key=lambda t: t[1].lower())]}


def preview(session: Session, user: User, span: Span, notebook_id: int | None, author_id: int | None) -> dict[str, Any]:
    rows = select_records(session, user, span, notebook_id, author_id)
    files = sum(len(spectra_of(session, e)) for e, _ in rows) if len(rows) <= 400 else None
    return {**counts(rows), "files": files, "scope": scope_text(session, notebook_id, author_id), "label": span.label(),
            "title": span.title(), "phrase": span.phrase(), **span.params()}


# ====================================================================== jobs, kept on disk

def scratch_dir() -> Path:
    d = settings.data_dir / "archive-tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sweep(now: float | None = None) -> None:
    """Delete jobs (and their PDFs) not touched for a day."""
    now = now or time.time()
    for p in scratch_dir().iterdir():
        try:
            j = p / "job.json"
            stamp = j.stat().st_mtime if j.exists() else p.stat().st_mtime
            if stamp < now - KEEP_SECONDS:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
        except OSError:
            continue


def _dir(job_id: str) -> Path:
    if not _ID.match(job_id or ""):
        raise ArchiveError(404, "No such archive")
    return scratch_dir() / job_id


def _save(job: dict[str, Any]) -> None:
    d = _dir(job["id"])
    if not d.is_dir():
        raise ArchiveError(404, "This archive was deleted")
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = d / f"job.json.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(job))
    tmp.replace(d / "job.json")


def _load(job_id: str) -> dict[str, Any] | None:
    try:
        job = json.loads((_dir(job_id) / "job.json").read_text())
    except (OSError, ValueError):
        return None
    if job.get("state") in ACTIVE and job.get("run") != _RUN:
        # Benchtop stopped while this job was open: what the browser had drawn is kept, but the job has to start again
        job["state"] = "stopped"
        job["error"] = "Benchtop stopped while this archive was being made. Start it again."
        _save(job)
    return job


def get_job(user: User, job_id: str) -> dict[str, Any]:
    """The job, if it's this person's (404 otherwise: someone else's job isn't confirmed to exist)."""
    with _LOCK:
        job = _load(job_id)
    if job is None or job.get("user_id") != user.id:
        raise ArchiveError(404, "No such archive")
    return job


def current_job(user: User) -> dict[str, Any] | None:
    """This person's latest job, if any is left."""
    sweep()
    with _LOCK:
        jobs = [j for p in scratch_dir().iterdir() if p.is_dir() and _ID.match(p.name)
                for j in [_load(p.name)] if j and j.get("user_id") == user.id]
    return max(jobs, key=lambda j: j["created_at"]) if jobs else None


def _update(job_id: str, **changes: Any) -> dict[str, Any]:
    with _LOCK:
        job = _load(job_id)
        if job is None:
            raise ArchiveError(404, "This archive was deleted")
        job.update(changes)
        _save(job)
        return job


def start_job(session: Session, user: User, span: Span, notebook_id: int | None, author_id: int | None,
              page_size: str) -> dict[str, Any]:
    if page_size not in PAGE_SIZES:
        raise ArchiveError(422, "The page size is US Letter or A4")
    # a notebook the person can't open may still hold a record shared with them: the records are what's checked
    if notebook_id is not None and session.get(Notebook, notebook_id) is None:
        raise ArchiveError(404, "Notebook not found")
    if author_id is not None and session.get(User, author_id) is None:
        raise ArchiveError(404, "Person not found")
    rows = select_records(session, user, span, notebook_id, author_id)
    if not rows:
        raise ArchiveError(422, f"No records you can open were signed {span.phrase()}; nothing to archive")
    sweep()
    with _LOCK:  # one job per person: starting again replaces the last one
        for p in scratch_dir().iterdir():
            j = _load(p.name) if p.is_dir() and _ID.match(p.name) else None
            if j and j.get("user_id") == user.id:
                shutil.rmtree(p, ignore_errors=True)
        job_id = secrets.token_hex(8)
        d = scratch_dir() / job_id
        (d / "pictures").mkdir(parents=True)
        spectra = [s for e, _ in rows for s in spectra_of(session, e)]
        job = {"id": job_id, "user_id": user.id, "run": _RUN, "created_at": datetime.now(timezone.utc).isoformat(),
               "state": "drawing", "params": {**span.params(), "notebook_id": notebook_id, "author_id": author_id, "page_size": page_size},
               "scope": scope_text(session, notebook_id, author_id), "counts": counts(rows),
               "records": [e.id for e, _ in rows], "spectra": spectra, "drawn": {},
               "progress": {"done": 0, "total": len(rows), "message": ""}, "error": "", "pages": None, "bytes": None}
        _save(job)
    return job


def job_out(job: dict[str, Any], with_spectra: bool = True) -> dict[str, Any]:
    drawn = job.get("drawn", {})
    out = {k: job[k] for k in ("id", "state", "params", "scope", "counts", "progress", "error", "pages", "bytes", "created_at")}
    out["title"] = Span.from_params(job["params"]).title()
    out["file_name"] = file_name(job)
    out["spectra_total"] = len(job["spectra"])
    out["spectra_done"] = sum(1 for s in job["spectra"] if str(s["attachment_id"]) in drawn)
    out["spectra_drawn"] = sum(1 for s in job["spectra"] if drawn.get(str(s["attachment_id"])) == "ok")
    if with_spectra:
        out["spectra"] = [{**s, "done": str(s["attachment_id"]) in drawn} for s in job["spectra"]]
    return out


def file_name(job: dict[str, Any]) -> str:
    return pdf_file_name(Span.from_params(job["params"]), job["scope"])


def pdf_file_name(span: Span, scope: str) -> str:
    """Benchtop records 2026.pdf, or Benchtop records, 3–9 November 2026 – Notebook Suzuki couplings.pdf."""
    scope = "" if scope == "All records you can open" else f" – {scope}"
    name = re.sub(r'[\\/:*?"<>|\u0000-\u001f“”]+', " ", f"{span.title()}{scope}").strip()
    return re.sub(r"\s+", " ", name)[:150] + ".pdf"


def _spectrum(job: dict[str, Any], att_id: int) -> dict[str, Any]:
    s = next((s for s in job["spectra"] if s["attachment_id"] == att_id), None)
    if s is None:
        raise ArchiveError(404, "That file isn't one of this archive's spectra")
    if job["state"] != "drawing":
        raise ArchiveError(409, "This archive isn't taking pictures any more")
    return s


def add_picture(user: User, job_id: str, att_id: int, data: bytes) -> dict[str, Any]:
    """A spectrum drawn by the browser: checked, flattened onto white and kept for this archive only."""
    job = get_job(user, job_id)
    _spectrum(job, att_id)
    if len(data) > MAX_PNG_BYTES:
        raise ArchiveError(413, f"A spectrum picture can be up to {MAX_PNG_BYTES // 1_000_000} MB")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ArchiveError(415, "A spectrum picture must be a PNG")
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        if im.format != "PNG" or not (0 < im.width <= MAX_SIDE and 0 < im.height <= MAX_SIDE):
            raise ArchiveError(415, f"A spectrum picture must be a PNG of at most {MAX_SIDE} × {MAX_SIDE} pixels")
        im.load()
        flat = Image.new("RGB", im.size, "white")
        rgba = im.convert("RGBA")
        flat.paste(rgba, mask=rgba.getchannel("A"))
    except ArchiveError:
        raise
    except Exception:  # noqa: BLE001 - whatever is wrong with it, it isn't a picture we can use
        raise ArchiveError(415, "That isn't a PNG picture that can be read") from None
    buf = io.BytesIO()
    flat.save(buf, "PNG")
    with _LOCK:
        job = get_job(user, job_id)
        _spectrum(job, att_id)
        (_dir(job_id) / "pictures" / f"{att_id}.png").write_bytes(buf.getvalue())
        job["drawn"][str(att_id)] = "ok"
        _save(job)
    return job


def skip_picture(user: User, job_id: str, att_id: int, reason: str) -> dict[str, Any]:
    """A file the browser couldn't draw: the archive lists it by name and fingerprint, with the reason."""
    with _LOCK:
        job = get_job(user, job_id)
        _spectrum(job, att_id)
        (_dir(job_id) / "pictures" / f"{att_id}.png").unlink(missing_ok=True)
        job["drawn"][str(att_id)] = (reason or "couldn't be drawn").strip()[:200]
        _save(job)
    return job


def request_build(user: User, job_id: str) -> dict[str, Any]:
    with _LOCK:
        job = get_job(user, job_id)
        if job["state"] != "drawing":
            raise ArchiveError(409, {"queued": "This archive is already waiting to be made", "building": "This archive is being made",
                                     "done": "This archive is ready", "stopped": job.get("error") or "This archive stopped; start it again",
                                     "failed": "This archive failed; start it again"}.get(job["state"], "This archive can't be made now"))
        job["state"] = "queued"
        job["progress"] = {"done": 0, "total": len(job["records"]), "message": "Waiting for another archive to finish"}
        _save(job)
    threading.Thread(target=_run, args=(job_id,), name=f"benchtop-archive-{job_id}", daemon=True).start()
    return job


def delete_job(user: User, job_id: str) -> None:
    get_job(user, job_id)
    with _LOCK:
        shutil.rmtree(_dir(job_id), ignore_errors=True)


def pdf_path(user: User, job_id: str) -> Path:
    job = get_job(user, job_id)
    p = _dir(job_id) / "archive.pdf"
    if job["state"] != "done" or not p.is_file():
        raise ArchiveError(409, "This archive isn't ready yet")
    os.utime(_dir(job_id) / "job.json")  # downloading it keeps it another day
    return p


class _Gone(Exception):
    """The job was deleted (or replaced) while it was being made."""


def _run(job_id: str) -> None:
    from .db import engine

    with _BUILD:
        with _LOCK:
            job = _load(job_id)
            if job is None or job["state"] != "queued":  # deleted or replaced while it waited
                return
            job["state"] = "building"
            job["progress"] = {"done": 0, "total": len(job["records"]), "message": "Starting"}
            _save(job)
        d = _dir(job_id)

        def progress(done: int, total: int, message: str) -> None:
            if not d.is_dir():
                raise _Gone
            _update(job_id, progress={"done": done, "total": total, "message": message})

        try:
            with Session(engine) as session:
                user = session.get(User, job["user_id"])
                if user is None:
                    raise ArchiveError(404, "The account that started this archive is gone")
                pictures = _pictures(job, d / "pictures")
                prm = job["params"]
                data, info = build_pdf(session, user, Span.from_params(prm), prm["notebook_id"], prm["author_id"], prm["page_size"],
                                       pictures=pictures, progress=progress)
            tmp = d / "archive.pdf.part"
            tmp.write_bytes(data)
            tmp.replace(d / "archive.pdf")
            _update(job_id, state="done", pages=info["pages"], bytes=len(data), counts=info["counts"],
                    progress={"done": info["counts"]["records"], "total": info["counts"]["records"], "message": "Ready"},
                    finished_at=datetime.now(timezone.utc).isoformat())
        except (_Gone, FileNotFoundError):
            return
        except Exception as exc:  # noqa: BLE001 - said on the page; the job can be started again
            log.exception("Archive PDF %s failed", job_id)
            msg = str(exc) if isinstance(exc, (ArchiveError, pdfa.PdfError)) else "Something went wrong while laying out the PDF"
            try:
                _update(job_id, state="failed", error=f"{msg}. Start it again; if it fails again, the server log says why.")
            except ArchiveError:
                pass


def _pictures(job: dict[str, Any], folder: Path) -> dict[int, dict[str, Any]]:
    """attachment id → the PNG drawn for it, or why there is none."""
    from PIL import Image

    out: dict[int, dict[str, Any]] = {}
    for s in job["spectra"]:
        aid = s["attachment_id"]
        state = job["drawn"].get(str(aid))
        p = folder / f"{aid}.png"
        if state == "ok" and p.is_file():
            data = p.read_bytes()
            with Image.open(io.BytesIO(data)) as im:
                out[aid] = {"png": data, "width": im.width, "height": im.height}
        else:
            out[aid] = {"error": state or "not drawn"}
    return out


# ====================================================================== the PDF

def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _banner(session: Session, exp: Experiment, d: dict[str, Any], current_hash: str) -> str:
    a, w, v = d["author"], d["witness"], d["void"]
    bits = []
    if a is not None:
        bits.append(f"Signed by <b>{_esc(a.signer_name or a.signer_username)}</b> on {_when(a.signed_at)}")
        if w is not None:
            bits.append(f"witnessed by <b>{_esc(w.signer_name or w.signer_username)}</b> on {_when(w.signed_at)}")
        elif exp.status == ExperimentStatus.signed:
            bits.append("<b>awaiting witness</b> (not countersigned when this archive was made)")
        elif exp.status == ExperimentStatus.voided:
            bits.append("not witnessed")
    else:
        bits.append("Never signed by its author")
    text = "; ".join(bits) + "."
    if a is not None and a.content_hash != current_hash:
        text += (" <b>Warning: the record's content no longer matches its author signature</b> (checked when this archive "
                 "was made); open it in Benchtop and use Verify integrity.")
    if v is not None:
        return (f"<div class='banner void'><b>VOIDED</b> on {_when(v.signed_at)} by <b>{_esc(v.signer_name or v.signer_username)}</b>. "
                f"Reason: “{_esc(v.comment)}”<br>{text}</div>")
    return f"<div class='banner'>{text}</div>"


_PAGE_CSS = """@page {{size: {size}; margin: 1.6cm 1.5cm 1.8cm 1.5cm}}
body {{font-size: 9.5pt; line-height: 1.35; color: #000}} {fonts}
h1 {{font-size: 22pt; margin: 0 0 .3em 0}} h2 {{font-size: 14pt; margin: .8em 0 .3em 0}} h3 {{font-size: 11pt; margin: .9em 0 .2em 0}}
p {{margin: .25em 0 .45em 0}} .meta {{color: #333}} .small {{font-size: 8pt}}
table {{border-collapse: collapse; width: 100%}} td, th {{border: .5pt solid #444; padding: 2px 3px; vertical-align: top; text-align: left}}
td, th {{overflow-wrap: break-word}} th {{background: #e6e6e6; font-weight: bold}} .contents td, .contents th {{font-size: 7.3pt}} .fp {{font-size: 6pt}}
pre {{font-size: 7.5pt; border: .5pt solid #444; padding: 4px; margin: .3em 0 .5em 0; white-space: pre-wrap}}
.cover {{padding-top: 5cm}} .rule {{border-top: 2pt solid #000; margin: .6em 0 1em 0}} .voidmark {{font-weight: bold}}
"""


def _page(body: str, size: str, title: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)}</title><style>"
            + _PAGE_CSS.format(size=size, fonts=pdfa.font_css()) + f"</style></head><body>{body}</body></html>")


def _cover(title: str, scope: str, span: Span, c: dict[str, int], user: User, made: datetime, size: str,
           archival: bool = True) -> str:
    witnessed, awaiting, voided = c["witnessed"], c["awaiting_witness"], c["voided"]
    parts = [f"{witnessed} witnessed", f"{awaiting} awaiting witness"] + ([f"{voided} voided"] if voided else [])
    return _page(f"""<div class='cover'><h1>{_esc(title)}</h1><div class='rule'></div>
<p style='font-size:13pt'>{_esc(scope)}</p>
<p style='font-size:11pt'><b>{c['records']} record{'' if c['records'] == 1 else 's'}</b> signed {span.phrase()}
({'days in UTC' if span.tz == 'UTC' else 'days in the ' + _esc(span.tz) + ' time zone'}), in the order they were signed: {', '.join(parts)}.</p>
<p>Made by {_esc(_name(user))} on {_when(made)} with Benchtop ELN {__version__}.</p>
<div style='margin-top:2.5cm' class='meta'>
<p><b>What is in it.</b> Each record as Benchtop prints it, starting on a new page, with its spectra drawn as Benchtop's
spectrum viewer draws them (saved processing, referencing, integrals and labels). Witnessed records and records awaiting
a witness are both included, and each says which. Voided records are included and marked <b>VOIDED</b>, with the reason
and date; a record voided before it was ever signed is placed by the date it was voided.</p>
<p><b>What is left out.</b> Drafts (records not signed), templates, records imported read-only from other notebooks, and
records you can't open.</p>
{INSIDE if archival else NOT_INSIDE}</div></div>""", size, title)


INSIDE = """<p><b>Inside this PDF.</b> Each record's own data (what its signatures cover, its fingerprint and its signatures) and the
proofs of its trusted timestamps are attached to this file; the last page says how to open and check them. The
instrument files themselves are not attached: they are in Benchtop, and in the record's .eln export, each with the
SHA-256 fingerprint printed under its spectrum here.</p>
<p>This file is a PDF/A-3 document, the archival form of PDF: its fonts and colour profile are inside it.</p>"""
# where the PDF/A package isn't installed (0.34): an ordinary PDF, with its fonts inside it but nothing attached
NOT_INSIDE = """<p><b>The proofs.</b> On the computer that made it, Benchtop couldn't attach each record's data and the
proofs of its trusted timestamps to this file (the PDF/A package isn't installed there). They are in Benchtop: each
signature's <i>Proof</i> download on the record's page, and the record's .eln export, which also holds the instrument
files, each with the SHA-256 fingerprint printed under its spectrum here. The last page says how to check them.</p>"""


def _contents(rows: list[dict[str, Any]], size: str, title: str) -> str:
    trs = []
    for r in rows:
        fp = r["hash"]
        mark = f"<br><span class='voidmark'>VOIDED</span><br>{r['voided']}" if r["voided"] else ""
        trs.append(f"<tr><td>{r['eid']}{mark}</td><td>{_esc(r['title'])}<br><span class='mono fp'>{fp[:32]}<br>{fp[32:]}</span></td>"
                   f"<td>{_esc(r['author'])}</td><td>{_esc(r['notebook'])}</td><td>{_esc(r['signed_by'])}<br>{r['signed_at']}</td>"
                   f"<td>{_esc(r['witness'])}</td><td style='text-align:right'>{r['page']}</td></tr>")
    return _page(f"""<h2>Contents</h2>
<p class='small'>In the order they were signed. Times are UTC. Under each title, the record's SHA-256 fingerprint.</p>
<table class='contents' repeat='1'><tr><th width='11%'>Record</th><th width='30%'>Title and fingerprint</th><th width='12%'>Author</th>
<th width='13%'>Notebook</th><th width='13%'>Signed</th><th width='15%'>Witnessed</th><th width='6%'>Page</th></tr>{''.join(trs)}</table>""",
                 size, title)


# printed on the last page, and run as printed by the tests (tests/test_v34_archive.py)
HOW_TO_RECOMPUTE = """python3 -c "import json, hashlib, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
s = json.dumps(d['snapshot'], sort_keys=True, separators=(',', ':'))
print(hashlib.sha256(s.encode()).hexdigest())" EXP-00012.json"""


def _how_to(n_records: int, n_proofs: int, size: str, title: str, pdf_name: str = "archive.pdf", archival: bool = True) -> str:
    f = _esc(f'"{pdf_name}"' if " " in pdf_name else pdf_name)
    if not archival:
        return _how_to_plain(size, title)
    return _page(f"""<div style='font-size:8.8pt'><h2>How to check this archive</h2>
<h3>What is attached inside this PDF</h3>
<p>{n_records + n_proofs} files are attached to this PDF (PDF/A-3 associated files). For each record:</p>
<p>• <b>EXP-00012.json</b> (with the record's own number): the record's data as Benchtop holds it. <i>snapshot</i> is everything
its signatures cover (title, description, author, every section with its content, and each attached file's name and
SHA-256); <i>content_hash</i> is its fingerprint; <i>signatures</i> lists who signed, witnessed or voided it, and when.<br>
• <b>EXP-00012-signature-34-timestamp.zip</b>, one for each signature with a trusted timestamp: the same <i>Proof</i> file
the record's page in Benchtop offers (the timestamp token, the exact text that was timestamped, the service's
certificates and a README).</p>
<p>The instrument files (spectra and other data) are not attached; they are in Benchtop and in each record's .eln export
(<i>Export (.eln)</i> on a notebook's page). Each characterisation file's SHA-256 is printed under its spectrum (or in its place)
here, and every attached file's is in the record's <i>snapshot</i>; check a file against it with <span class='mono'>shasum -a 256 &lt;file&gt;</span> (macOS) or
<span class='mono'>sha256sum &lt;file&gt;</span> (Linux).</p>
<h3>Opening the attachments</h3>
<p>In a PDF reader with an attachments panel (Adobe Acrobat Reader: the paper-clip icon; Firefox's PDF viewer; Okular):
open the panel and save the file. Some readers don't show attachments. From a terminal:</p>
<pre class='mono'>qpdf --list-attachments {f}
qpdf --show-attachment=EXP-00012.json {f} &gt; EXP-00012.json
pdfdetach -list {f}
pdfdetach -saveall {f}</pre>
<p class='small'>(qpdf and pdfdetach, from poppler-utils, are free command-line tools; on a Mac, install them with Homebrew.)</p>
<h3>Checking a signature's trusted timestamp, without Benchtop</h3>
<p>Each signature was also certified by an independent RFC 3161 timestamp service; only a fingerprint of the signature
was sent to it, never the record. Unzip the signature's timestamp file, then (macOS / Linux):</p>
<pre class='mono'>openssl ts -verify -data signature.json -in token.tst -token_in \\
    -CAfile /etc/ssl/cert.pem -untrusted tsa-certs.pem</pre>
<p>(On Debian and Ubuntu the trusted certificates are in <span class='mono'>/etc/ssl/certs/ca-certificates.crt</span>.)
"Verification: OK" means the service certifies that this signature (record, meaning, signer, time and the SHA-256 of
the record's content, <i>content_sha256</i>) existed at the time shown. To link it to the record itself, compare
<i>content_sha256</i> with the record's fingerprint: printed in the contents and at the foot of the record here, and
<i>content_hash</i> in its .json file. To read the token: <span class='mono'>openssl ts -reply -token_in -in token.tst -text</span>.
Years from now the service's certificate may have expired or no longer be on your computer's trusted list; openssl then
can't verify the token until you give it the service's root certificate (from the service's own website).</p>
<h3>Checking a record's fingerprint</h3>
<p>The fingerprint is the SHA-256 of the record's <i>snapshot</i> written as compact JSON, with keys in sorted order, the
way Python's json module writes it (sort_keys=True, separators=(',', ':'), non-ASCII characters escaped). With Python 3,
from the record's .json file:</p>
<pre class='mono'>{_esc(HOW_TO_RECOMPUTE)}</pre>
<p>The result should equal <i>content_hash</i> in the same file, the fingerprint printed here, and <i>content_sha256</i>
in each of its timestamps. Other JSON tools may write numbers or characters differently and give another SHA-256;
Benchtop's <i>Verify integrity</i> recomputes it this way. A match shows the attached data is the data that was signed;
the printed pages are a rendering of it, for reading.</p></div>""", size, title)


def _how_to_plain(size: str, title: str) -> str:
    """The last page of an archive made where the PDF/A package isn't installed: nothing is attached, so it points to
    where the same files are."""
    return _page(f"""<div style='font-size:8.8pt'><h2>How to check this archive</h2>
<h3>Where each record's data and proofs are</h3>
<p>This PDF was made on a computer where Benchtop couldn't attach files to it (the PDF/A package isn't installed there),
so each record's data and its timestamp proofs are not inside it. They are in Benchtop:</p>
<p>• the record's <b>.eln export</b> (<i>Export (.eln)</i> on a notebook's page, or the record's own export): its
<span class='mono'>record.benchtop.json</span> holds the record's data (<i>snapshot</i>, everything its signatures cover,
and <i>content_hash</i>, its fingerprint), with every attached file and, in its signatures folder, each timestamp proof;<br>
• each signature's <b>Proof</b> download on the record's page (the timestamp token, the exact text that was timestamped,
the service's certificates and a README).</p>
<p>Each characterisation file's SHA-256 is printed under its spectrum (or in its place) here; check a file from the .eln
export against it with <span class='mono'>shasum -a 256 &lt;file&gt;</span> (macOS) or
<span class='mono'>sha256sum &lt;file&gt;</span> (Linux).</p>
<h3>Checking a signature's trusted timestamp, without Benchtop</h3>
<p>Each signature was also certified by an independent RFC 3161 timestamp service; only a fingerprint of the signature
was sent to it, never the record. Unzip the signature's Proof, then (macOS / Linux):</p>
<pre class='mono'>openssl ts -verify -data signature.json -in token.tst -token_in \\
    -CAfile /etc/ssl/cert.pem -untrusted tsa-certs.pem</pre>
<p>(On Debian and Ubuntu the trusted certificates are in <span class='mono'>/etc/ssl/certs/ca-certificates.crt</span>.)
"Verification: OK" means the service certifies that this signature (record, meaning, signer, time and the SHA-256 of
the record's content, <i>content_sha256</i>) existed at the time shown. To link it to the record itself, compare
<i>content_sha256</i> with the record's fingerprint, printed in the contents and at the foot of the record here.
Years from now the service's certificate may have expired or no longer be on your computer's trusted list; openssl then
can't verify the token until you give it the service's root certificate (from the service's own website).</p>
<h3>Checking a record's fingerprint</h3>
<p>The fingerprint is the SHA-256 of the record's <i>snapshot</i> written as compact JSON, with keys in sorted order, the
way Python's json module writes it (sort_keys=True, separators=(',', ':'), non-ASCII characters escaped). With Python 3,
from the record's <span class='mono'>record.benchtop.json</span>:</p>
<pre class='mono'>{_esc(HOW_TO_RECOMPUTE.replace("EXP-00012.json", "record.benchtop.json"))}</pre>
<p>The result should equal <i>content_hash</i> in the same file, the fingerprint printed here, and <i>content_sha256</i>
in each of its timestamps. Other JSON tools may write numbers or characters differently and give another SHA-256;
Benchtop's <i>Verify integrity</i> recomputes it this way.</p></div>""", size, title)


def _one_record_html(session: Session, exp: Experiment, d: dict[str, Any], size: str, figures: dict[int, dict]) -> str:
    from .routers.experiments import report_html
    return report_html(session, exp, pdf=True, page_size=size, footer=False, banner=_banner(session, exp, d, content_hash(session, exp)),
                       figures=figures)


def _fallback_html(exp: Experiment, why: str, size: str) -> str:
    return _page(f"<h1>{_esc(exp.eid)} {_esc(exp.title)}</h1><p>This record couldn't be laid out as PDF pages ({_esc(why)}). "
                 f"Its full content is attached inside this PDF as {exp.eid}.json, and Benchtop prints it with "
                 "<i>Print</i> on the record's page.</p>", size, exp.eid)


def _footer_pdf(sizes: list[tuple[float, float]], texts: list[str | None]) -> bytes:
    """One page per page of the archive with its footer (none where the text is None), in the embedded font."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=sizes[0], initialFontName=pdfa.SANS, initialFontSize=7.5)
    for (w, h), t in zip(sizes, texts):
        c.setPageSize((w, h))
        if t:
            c.setFont(pdfa.SANS, 7.5)
            c.setFillGray(0.2)
            c.drawCentredString(w / 2, 0.9 * 28.3465, t)
        c.showPage()
    c.save()
    return buf.getvalue()


def _page_count(raw: bytes) -> int:
    import pypdf  # xhtml2pdf's own dependency: always there
    return len(pypdf.PdfReader(io.BytesIO(raw)).pages)


def _footers(marks: list[str], title: str) -> list[str | None]:
    total = len(marks)
    return [None if k == 0 else f"{title} · {mk + ' · ' if mk.startswith(('EXP', 'TPL')) else ''}page {k + 1} of {total}"
            for k, mk in enumerate(marks)]


def _assemble_pikepdf(sections: list[tuple[str, str, bytes]], title: str):
    """The sections (bookmark, footer mark, PDF) as one pikepdf.Pdf: bookmarks, footers, font subsets named apart."""
    import pikepdf
    from pikepdf import Array, Name, OutlineItem

    out = pikepdf.new()
    keep: list[Any] = []
    starts, marks = [], []
    for label, mark, raw in sections:
        src = pikepdf.open(io.BytesIO(raw))
        keep.append(src)
        starts.append((label, len(out.pages)))
        out.pages.extend(src.pages)
        marks += [mark] * len(src.pages)
    # links inside a record pointed at places in its own file; in the archive they'd point nowhere
    for page in out.pages:
        annots = page.obj.get("/Annots")
        if annots is None:
            continue
        page.obj.Annots = Array([a for a in annots if not ("/Dest" in a or (a.get("/A") is not None and a.A.get("/S") == Name.GoTo))])
    sizes = [(float(p.mediabox[2]) - float(p.mediabox[0]), float(p.mediabox[3]) - float(p.mediabox[1])) for p in out.pages]
    texts = _footers(marks, title)
    over = pikepdf.open(io.BytesIO(_footer_pdf(sizes, texts)))
    keep.append(over)
    for k, page in enumerate(out.pages):
        if texts[k]:
            page.add_overlay(over.pages[k])
    pdfa.rename_subsets(out)
    with out.open_outline() as outline:
        outline.root.extend([OutlineItem(label, page) for label, page in starts])
    out.Root.PageMode = Name.UseOutlines
    return out, keep, starts, len(out.pages)


def _assemble_pypdf(sections: list[tuple[str, str, bytes]], title: str, meta: dict[str, str]) -> tuple[bytes, list, int]:
    """The same, with pypdf, where the PDF/A package isn't installed: an ordinary PDF, nothing attached."""
    import pypdf
    from pypdf.generic import ArrayObject, NameObject

    out = pypdf.PdfWriter()
    starts, marks = [], []
    for label, mark, raw in sections:
        src = pypdf.PdfReader(io.BytesIO(raw))
        starts.append((label, len(out.pages)))
        for page in src.pages:
            out.add_page(page)
        marks += [mark] * len(src.pages)
    texts = _footers(marks, title)
    sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in out.pages]
    over = pypdf.PdfReader(io.BytesIO(_footer_pdf(sizes, texts)))
    n = 0
    for k, page in enumerate(out.pages):
        annots = page.get("/Annots")
        if annots is not None:
            page[NameObject("/Annots")] = ArrayObject([a for a in annots.get_object() if not (
                "/Dest" in a.get_object() or (a.get_object().get("/A") is not None and a.get_object()["/A"].get("/S") == "/GoTo"))])
        if texts[k]:
            page.merge_page(over.pages[k])
        fonts = page.get("/Resources", {}).get("/Font", {})
        for _, ref in (fonts.items() if fonts else []):  # each font subset its own name, as with pikepdf
            f = ref.get_object()
            if "/BaseFont" in f:
                tag = "".join(chr(65 + (n // 26 ** j) % 26) for j in range(6))
                n += 1
                f[NameObject("/BaseFont")] = NameObject("/" + tag + "+" + str(f["/BaseFont"])[1:].split("+", 1)[-1])
    for label, page in starts:
        out.add_outline_item(label, page)
    out.page_mode = "/UseOutlines"
    out.add_metadata(meta)
    buf = io.BytesIO()
    out.write(buf)
    return buf.getvalue(), starts, len(out.pages)


def build_pdf(session: Session, user: User, span: Span, notebook_id: int | None = None, author_id: int | None = None,
              page_size: str = "letter", pictures: dict[int, dict] | None = None,
              progress: Callable[[int, int, str], None] | None = None) -> tuple[bytes, dict[str, Any]]:
    """The archive as PDF/A-3b bytes (an ordinary PDF where the PDF/A package isn't installed), and what is in it.
    Records are chosen again now, so one withdrawn or hidden from this person since the job started is left out."""
    pdfa._setup()
    archival = pdfa.available()
    pictures = pictures or {}
    progress = progress or (lambda *a: None)
    size = "a4" if page_size == "a4" else "letter"
    title = span.title()
    rows = select_records(session, user, span, notebook_id, author_id)
    if not rows:
        raise ArchiveError(422, f"No records you can open were signed {span.phrase()} any more; nothing to archive")
    made = datetime.now(timezone.utc)
    n = len(rows)
    parts: list[bytes] = []
    listed: list[dict[str, Any]] = []
    files: list[pdfa.Attachment] = []
    proofs = 0
    for i, (exp, d) in enumerate(rows, 1):
        progress(i - 1, n, f"Adding record {i} of {n}")
        figs = {s["attachment_id"]: pictures.get(s["attachment_id"]) or {"error": "not drawn"} for s in spectra_of(session, exp)}
        try:
            raw = pdfa.html_to_pdf(_one_record_html(session, exp, d, size, figs))
        except Exception as exc:  # noqa: BLE001 - one record that can't be laid out doesn't stop the archive
            log.warning("Archive PDF: %s couldn't be laid out: %s", exp.eid, exc)
            raw = pdfa.html_to_pdf(_fallback_html(exp, str(exc) if isinstance(exc, pdfa.PdfError) else "an error", size))
        parts.append(raw)
        a, w, v = d["author"], d["witness"], d["void"]
        nb = session.get(Notebook, exp.notebook_id) if exp.notebook_id else None
        listed.append({"eid": exp.eid, "title": exp.title, "author": _name(session.get(User, exp.owner_id)),
                       "notebook": nb.name if nb else "", "signed_by": (a.signer_name or a.signer_username) if a else "never signed",
                       "signed_at": f"{a.signed_at:%Y-%m-%d %H:%M}" if a else "",
                       "witness": f"{w.signer_name or w.signer_username} {w.signed_at:%Y-%m-%d %H:%M}" if w
                       else ("awaiting witness" if exp.status == ExperimentStatus.signed else "—"),
                       "voided": f"{v.signed_at:%Y-%m-%d}" if v else "", "hash": content_hash(session, exp),
                       "pages": _page_count(raw), "status": exp.status.value})
        if archival:
            att = proof_attachments(session, exp)
            proofs += len(att) - 1
            files.extend(att)
    progress(n, n, "Putting the archive together")
    c = counts(rows)
    scope = scope_text(session, notebook_id, author_id)
    cover = pdfa.html_to_pdf(_cover(title, scope, span, c, user, made, size, archival))
    how = pdfa.html_to_pdf(_how_to(n, proofs, size, title, pdf_file_name(span, scope), archival))
    # the contents give each record's first page, which depends on how long the contents are: lay them out with
    # placeholders first (the same width), then with the numbers
    for r in listed:
        r["page"] = "0000"
    contents = pdfa.html_to_pdf(_contents(listed, size, title))
    for _ in range(3):
        at = _page_count(cover) + _page_count(contents) + 1
        for r in listed:
            r["page"], at = at, at + r["pages"]
        again = pdfa.html_to_pdf(_contents(listed, size, title))
        same = _page_count(again) == _page_count(contents)
        contents = again
        if same:
            break
    sections = [("Cover", "Cover", cover), ("Contents", "Contents", contents)]
    sections += [(f"{r['eid']} {r['title']}{' (VOIDED)' if r['voided'] else ''}", r["eid"] + (" (VOIDED)" if r["voided"] else ""), raw)
                 for r, raw in zip(listed, parts)]
    sections.append(("How to check this archive", "How to check this archive", how))
    doc_title = f"{title}: {scope}" if scope != "All records you can open" else title
    subject = f"{scope}; {c['records']} records signed {span.phrase()}"
    if archival:
        out, keep, starts, total = _assemble_pikepdf(sections, title)
        progress(n, n, "Making it archival (PDF/A)")
        data = pdfa.make_pdfa(b"", pdf=out, title=doc_title, author=_name(user), subject=subject,
                              keywords=f"Benchtop ELN, lab notebook, {span.label()}", created=made, attachments=files)
        for k in keep:
            k.close()
    else:
        data, starts, total = _assemble_pypdf(sections, title, {"/Title": doc_title, "/Author": _name(user), "/Subject": subject,
                                                                  "/Creator": f"Benchtop ELN {__version__}"})
    return data, {"pages": total, "counts": c, "records": [r["eid"] for r in listed], "starts": starts,
                  "attachments": [f.name for f in files], "archival": archival}
