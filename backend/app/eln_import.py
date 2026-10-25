"""Import .eln files (the ELN Consortium's format) from eLabFTW, RSpace, Kadi4Mat, SampleDB, another Benchtop…

Two steps, so nothing lands in the notebook unseen:
  1. preview: check the file is safe to open and describe what's inside (each experiment, its files, who wrote it,
     whether it was signed or locked there, whether it was imported before)
  2. import the experiments the person ticks, into a notebook they choose

Every experiment becomes a new Benchtop record owned by the person importing, with a new EXP number. Where it came
from (the other ELN, the original ID, dates and authors) is kept on the record and shown as a banner. Records that were
signed or locked in the other ELN arrive read-only ("locked"); "Make a follow-up" continues the work. Their signatures
are never turned into Benchtop signatures: Benchtop can't verify another system's signature, so they're kept as history.

Nothing in the original is thrown away: what Benchtop has no place for is kept in an attached original-record.json,
and every file's SHA-256 is checked against the one the other ELN recorded.

Safety: the zip is read member by member, never extracted as a whole. Absolute paths, "..", links, encrypted members,
and members that expand absurdly (zip bombs) are refused before anything is stored.
"""
from __future__ import annotations

import bisect
import hashlib
import html
import json
import os
import re
import secrets
import stat
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import unquote

from sqlmodel import Session, select

from . import audit, diskspace
from .config import settings
from .eln import RECORD_FILE, RECORD_FORMAT, scratch_dir
from .models import (Attachment, Entry, EntryKind, EntryVersion, Experiment, ExperimentStatus, Notebook, User, utcnow)

MAX_MEMBERS = 200_000
MAX_METADATA = 200 * 1024 * 1024
BOMB_RATIO = 400           # a member expanding more than this many times…
BOMB_MIN = 100 * 1024**2   # …and to more than 100 MB is treated as a zip bomb
LOCK_WORDS = {"signed", "witnessed", "locked", "timestamped", "final", "finalised", "finalized", "voided"}
TRUE = {"1", "true", "yes", "on", "y"}
KEEP_HOURS = 24


class ElnImportError(ValueError):
    """Something about the file, said so the person can act on it."""


MAX_DEPTH = 100  # nesting in a description; real exports stay under 20


def _too_deep(doc: Any, limit: int = MAX_DEPTH) -> bool:
    """Is this JSON nested more than `limit` levels? (Checked without recursion, so it can't overflow itself.)"""
    stack = [(doc, 1)]
    while stack:
        v, d = stack.pop()
        if d > limit:
            return True
        if isinstance(v, dict):
            stack.extend((x, d + 1) for x in v.values() if isinstance(x, (dict, list)))
        elif isinstance(v, list):
            stack.extend((x, d + 1) for x in v if isinstance(x, (dict, list)))
    return False


def _load_json(raw: bytes) -> Any:
    """JSON from inside the file, refused plainly when it's nested absurdly deep (it would crash the reader)."""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except RecursionError:
        doc = None
    else:
        if not _too_deep(doc):
            return doc
    raise ElnImportError("The description inside is nested thousands of levels deep; that isn't a normal export.")


def content_type(v: Any) -> str:
    """A file type as the other ELN gave it, if it's a plain "type/subtype"; otherwise a generic one."""
    t = v.split(";")[0].strip().lower() if isinstance(v, str) else ""
    return t if re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}", t) else "application/octet-stream"


# ---------------------------------------------------------------------------------------------------- reading a crate

def _types(node: dict[str, Any]) -> set[str]:
    t = node.get("@type", [])
    return {t} if isinstance(t, str) else {x for x in t if isinstance(x, str)} if isinstance(t, list) else set()


def _refs(v: Any) -> list[str]:
    items = v if isinstance(v, list) else [v] if v is not None else []
    out = []
    for x in items:
        if isinstance(x, dict) and isinstance(x.get("@id"), str):
            out.append(x["@id"])
        elif isinstance(x, str) and x.startswith(("./", "#")):
            out.append(x)
    return out


def _str(v: Any, limit: int = 2000) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        v = v.get("@value", v.get("name", v.get("@id", "")))
        if isinstance(v, dict):
            return ""  # an object inside an object: nothing readable to show
    if isinstance(v, list):
        return ", ".join(_str(x, limit) for x in v if x is not None)[:limit]
    return str(v).strip()[:limit]


def _date(v: Any) -> Optional[str]:
    s = _str(v, 64)
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):  # not a date, or one past the calendar's ends (year 9999 with a time zone)
        return s


@dataclass
class CrateFile:
    node_id: str
    name: str
    info: Optional[zipfile.ZipInfo]
    size: int
    sha256: str = ""       # what the other ELN recorded
    folder: str = ""       # sub-folder inside the experiment, if any
    described: bool = True  # listed in the metadata (False: found in the experiment's folder only)
    content_type: str = "application/octet-stream"
    inner: str = ""        # path inside the experiment's own folder ("files/x.pdf"), when it's in one


@dataclass
class Item:
    key: str
    node_id: str
    title: str
    node: dict[str, Any]
    files: list[CrateFile] = field(default_factory=list)
    children: list[str] = field(default_factory=list)  # other importable experiments inside this one
    warnings: list[str] = field(default_factory=list)
    record: Optional[dict[str, Any]] = None              # Benchtop's own full record, when it came from Benchtop
    links: list[tuple[str, str]] = field(default_factory=list)  # files the other ELN only linked to (name, web address)
    lock_reason: str = ""


class Crate:
    """An opened .eln file. Raises ElnImportError when it isn't one, or isn't safe to read."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.z = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as e:
            raise ElnImportError("This isn't a .eln file: it can't be opened as a zip archive.") from e
        infos = self.z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ElnImportError(f"This file holds more than {MAX_MEMBERS:,} items; split the export into smaller ones.")
        total = 0
        metas = []
        for i in infos:
            name = re.sub(r"/{2,}", "/", i.filename.replace("\\", "/"))
            p = PurePosixPath(name)
            if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", name):
                raise ElnImportError(f"This file contains a path that points outside it ({i.filename!r}), so it wasn't opened.")
            if i.flag_bits & 0x1:
                raise ElnImportError("This file is encrypted (password-protected); export it again without a password.")
            if not i.is_dir() and i.file_size > BOMB_MIN and i.file_size > BOMB_RATIO * max(i.compress_size, 1):
                raise ElnImportError(f"{i.filename!r} would expand to {i.file_size / 1e9:.1f} GB from {i.compress_size / 1e6:.1f} MB; "
                                     "that looks like a zip bomb, so the file wasn't opened.")
            total += i.file_size
            if p.name == "ro-crate-metadata.json" and len(p.parts) <= 2:
                metas.append(i)
        if total > settings.import_max_mb * 1024 * 1024 * 4:
            raise ElnImportError("This file would expand to more than four times the import limit; that isn't a normal export.")
        if not metas:
            raise ElnImportError("This zip has no ro-crate-metadata.json, so it isn't a .eln file (or it was re-zipped "
                                 "with an extra folder around it).")
        meta = min(metas, key=lambda i: len(PurePosixPath(i.filename).parts))
        self.root = str(PurePosixPath(meta.filename).parent) if len(PurePosixPath(meta.filename).parts) > 1 else ""
        if meta.file_size > MAX_METADATA:
            raise ElnImportError("The description file inside is over 200 MB; that isn't a normal export.")
        try:
            doc = _load_json(self.z.read(meta))
        except ElnImportError:
            raise
        except (ValueError, UnicodeDecodeError) as e:
            raise ElnImportError(f"The description inside (ro-crate-metadata.json) isn't valid JSON: {e}") from e
        graph = doc.get("@graph") if isinstance(doc, dict) else None
        if not isinstance(graph, list):
            raise ElnImportError("The description inside has no list of contents (@graph).")
        self.by_id: dict[str, dict[str, Any]] = {}
        for n in graph:
            if isinstance(n, dict) and isinstance(n.get("@id"), str):
                self.by_id.setdefault(n["@id"], n)
        desc = self.by_id.get("ro-crate-metadata.json") or {}
        about = (_refs(desc.get("about")) or ["./"])[0]
        self.root_node = self.by_id.get(about) or self.by_id.get("./")
        if not self.root_node:
            raise ElnImportError("The description inside doesn't say what the export contains (no root entry).")
        prefix = f"{self.root}/" if self.root else ""
        self.members: dict[str, zipfile.ZipInfo] = {}
        for i in infos:
            name = re.sub(r"/{2,}", "/", i.filename.replace("\\", "/"))
            if i.is_dir() or not name.startswith(prefix) or stat.S_ISLNK(i.external_attr >> 16):
                continue
            rel = name[len(prefix):]
            if rel and rel not in ("ro-crate-metadata.json", "ro-crate-metadata.json.minisig", "ro-crate-preview.html"):
                self.members[rel] = i
        # every folder in the archive, and the files in sorted order: big exports are looked up, never searched through
        self.folders = {str(PurePosixPath(rel).parents[k]) + "/" for rel in self.members
                        for k in range(len(PurePosixPath(rel).parents) - 1)}
        self.sorted = sorted(self.members)
        pub = self.by_id.get((_refs(desc.get("sdPublisher")) or [""])[0]) or {}
        software = next((n for n in self.by_id.values() if "SoftwareApplication" in _types(n)), {})
        self.source = _str(pub.get("name") or software.get("name") or "", 120) or "another ELN"
        self.source_url = _str(pub.get("url") or software.get("url"), 300)
        self.exported = _date(desc.get("dateCreated") or self.root_node.get("datePublished"))

    def close(self) -> None:
        self.z.close()

    # ---------------------------------------------------------------- paths
    def rel(self, info: zipfile.ZipInfo) -> str:
        name = re.sub(r"/{2,}", "/", info.filename.replace("\\", "/"))
        return name[len(self.root) + 1:] if self.root else name

    def member(self, node_id: str) -> Optional[zipfile.ZipInfo]:
        if re.match(r"^[a-z][a-z0-9+.-]*:", node_id, re.I) or node_id.startswith("#"):
            return None  # a web address or an internal name, not a file in the archive
        rel = re.sub(r"/{2,}", "/", node_id[2:] if node_id.startswith("./") else node_id.lstrip("/"))
        for cand in (rel, unquote(rel)):
            if cand in self.members:
                return self.members[cand]
        return None

    def folder(self, node_id: str) -> Optional[str]:
        """The folder a Dataset stands for ("exp-1/"), if the archive has one."""
        if not node_id.startswith("./") or node_id == "./":
            return None
        f = re.sub(r"/{2,}", "/", unquote(node_id[2:])).rstrip("/") + "/"
        return f if node_id.endswith("/") or f in self.folders else None

    def under(self, folder: str) -> list[str]:
        """The files inside a folder (and its sub-folders)."""
        i = bisect.bisect_left(self.sorted, folder)
        out = []
        while i < len(self.sorted) and self.sorted[i].startswith(folder):
            out.append(self.sorted[i])
            i += 1
        return out

    def name_of(self, node_id: str) -> str:
        n = self.by_id.get(node_id) or {}
        if not n:
            return node_id
        if "Person" in _types(n):
            full = _str(n.get("name")) or " ".join(x for x in (_str(n.get("givenName")), _str(n.get("familyName"))) if x)
            return full or "Someone"
        return _str(n.get("name") or n.get("identifier") or node_id, 300)

    # ---------------------------------------------------------------- what's inside
    def items(self) -> list[Item]:
        top = _refs(self.root_node.get("hasPart"))
        datasets = [i for i in top if "Dataset" in _types(self.by_id.get(i, {}))]
        importable = set(datasets)
        claimed: set[str] = set()
        items: list[Item] = []
        for k, ds in enumerate(datasets):
            node = self.by_id[ds]
            title = (_str(node.get("name"), 300) or _str(node.get("identifier"), 300)
                     or PurePosixPath((self.folder(ds) or "").rstrip("/")).name or f"Record {k + 1}")
            it = Item(key=str(k), node_id=ds, title=title, node=node)
            self._collect(it, ds, "", importable, claimed, set())
            items.append(it)
        # files found in an experiment's folder that the description doesn't list
        for it in items:
            base = self.folder(it.node_id)
            if not base:
                continue
            nested = [n for n in (self.folder(c) for c in it.children) if n]
            for rel in self.under(base):
                info = self.members[rel]
                if info.filename in claimed or any(rel.startswith(n) for n in nested):
                    continue
                sub = str(PurePosixPath(rel[len(base):]).parent)
                it.files.append(CrateFile(node_id=rel, name=PurePosixPath(rel).name, info=info, size=info.file_size,
                                          folder="" if sub == "." else sub, described=False))
                claimed.add(info.filename)
        for it in items:  # where each file sits inside its experiment's folder
            base = self.folder(it.node_id) or ""
            for f in it.files:
                rel = self.rel(f.info) if f.info else ""
                f.inner = rel[len(base):] if base and rel.startswith(base) else ""
        loose = []
        for f in [i for i in top if i not in importable]:
            info = self.member(f)
            if info is not None and info.filename not in claimed and "File" in _types(self.by_id.get(f, {})):
                loose.append(self._file(f, ""))
        if loose:
            it = Item(key=str(len(items)), node_id="./", title=f"Files from {self.source} not in any experiment", node={}, files=loose)
            items.append(it)
        for it in items:
            self._benchtop_record(it)
            it.lock_reason = self._lock_reason(it)
            for f in it.files:
                if f.info is None:
                    it.warnings.append(f"{f.name} is listed but isn't in the file; it can't be imported.")
                elif f.size > settings.instrument_max_mb * 1024 * 1024:
                    it.warnings.append(f"{f.name} ({f.size / 1e9:.1f} GB) is over the {settings.instrument_max_mb / 1024:g} GB limit "
                                       "for one file and will be left out.")
        return items

    def _file(self, node_id: str, folder: str) -> CrateFile:
        n = self.by_id.get(node_id, {})
        info = self.member(node_id)
        name = _str(n.get("name"), 200) or PurePosixPath(unquote(node_id.rstrip("/"))).name or "file"
        if info is not None and PurePosixPath(info.filename).suffix and not PurePosixPath(name).suffix:
            name += PurePosixPath(info.filename).suffix  # a title without the extension: keep the real one
        return CrateFile(node_id=node_id, name=name, info=info, size=info.file_size if info else 0,
                         sha256=_str(n.get("sha256"), 64).lower() if re.fullmatch(r"[0-9a-fA-F]{64}", _str(n.get("sha256"), 64)) else "",
                         folder=folder, content_type=content_type(_str(n.get("encodingFormat"), 200)))

    def _collect(self, it: Item, node_id: str, folder: str, importable: set[str], claimed: set[str], seen: set[str]) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        for child in _refs(self.by_id.get(node_id, {}).get("hasPart")):
            types = _types(self.by_id.get(child, {}))
            if "Dataset" in types and child in importable:
                it.children.append(child)
            elif "Dataset" in types:
                sub = self.name_of(child) if not self.folder(child) else PurePosixPath(self.folder(child).rstrip("/")).name
                self._collect(it, child, f"{folder}/{sub}".strip("/"), importable, claimed, seen)
            elif re.match(r"^https?://", child, re.I):
                it.links.append((_str(self.by_id.get(child, {}).get("name"), 200) or PurePosixPath(child).name, child))
            else:
                f = self._file(child, folder)
                if f.info is not None:
                    claimed.add(f.info.filename)
                it.files.append(f)

    def _benchtop_record(self, it: Item) -> None:
        rec = next((f for f in it.files if f.info is not None and PurePosixPath(f.info.filename).name == RECORD_FILE
                    and f.folder == ""), None)
        if rec is None or rec.size > 100 * 1024 * 1024:
            return
        try:
            data = _load_json(self.z.read(rec.info))
        except (ValueError, OSError, zipfile.BadZipFile):
            it.warnings.append("Its Benchtop record couldn't be read, so it's imported like any other ELN's.")
            return
        if isinstance(data, dict) and data.get("format") == RECORD_FORMAT and isinstance(data.get("entries"), list):
            it.record = _tidy_record(data)

    def _lock_reason(self, it: Item) -> str:
        if it.record:
            e = it.record.get("experiment") or {}
            st = e.get("status")
            if st in ("signed", "witnessed", "voided"):
                return f"it was {st} in {self.source}"
            if st == "locked":
                return "it was already a read-only import there"
            return ""
        status = _str(it.node.get("creativeWorkStatus"), 80)
        if status.lower() in LOCK_WORDS:
            return f"its status in {self.source} was “{status}”"
        for pv in _refs(it.node.get("variableMeasured")):
            n = self.by_id.get(pv, {})
            key = (_str(n.get("propertyID"), 80) or _str(n.get("name"), 80)).lower()
            if key in ("locked", "is_locked", "signed", "is_signed", "timestamped") and _str(n.get("value"), 20).lower() in TRUE:
                return f"it was marked “{key}” in {self.source}"
        return ""

    def origin_key(self, it: Item) -> str:
        n, rec = it.node, (it.record or {}).get("experiment") or {}
        parts = [self.source, _str(n.get("url"), 500) or _str(n.get("identifier"), 300), _str(rec.get("created_at") or n.get("dateCreated"), 64),
                 it.title, it.node_id if not n.get("url") else ""]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def authors(self, it: Item) -> list[str]:
        if it.record and (it.record.get("experiment") or {}).get("owner"):
            return [it.record["experiment"]["owner"].get("name") or "Someone"]
        return [self.name_of(a) for a in _refs(it.node.get("author"))][:20]


def _tidy_record(data: dict[str, Any]) -> dict[str, Any]:
    """A record.benchtop.json as Benchtop wrote it, whatever was edited or damaged since: each part the right kind of
    thing (text, a list, an object) or left out, so a bad part costs that part, never the whole import."""
    def text(v: Any) -> Optional[str]:
        return v if isinstance(v, str) else None

    def person(v: Any) -> Optional[dict[str, Any]]:
        return {"name": text(v.get("name")), "username": text(v.get("username"))} if isinstance(v, dict) else None
    exp = data.get("experiment") if isinstance(data.get("experiment"), dict) else {}
    exp = {**exp, "owner": person(exp.get("owner"))}
    for k in ("eid", "follows", "status", "title", "description", "created_at", "updated_at", "notebook"):
        exp[k] = text(exp.get(k))
    exp["tags"] = [t for t in exp.get("tags") or [] if isinstance(t, str)] if isinstance(exp.get("tags"), list) else []
    atts = []
    for a in data.get("attachments") or [] if isinstance(data.get("attachments"), list) else []:
        if isinstance(a, dict):
            atts.append({**a, "filename": text(a.get("filename")), "content_type": content_type(a["content_type"]) if a.get("content_type") else None,
                         "sha256": text(a.get("sha256"))})
    comments = []
    for c in data.get("comments") or [] if isinstance(data.get("comments"), list) else []:
        if isinstance(c, dict) and isinstance(c.get("body"), str):
            comments.append({**c, "author": person(c.get("author")), "created_at": text(c.get("created_at"))})
    lists = {k: [x for x in data.get(k) if isinstance(x, dict)] if isinstance(data.get(k), list) else []
             for k in ("entries", "signatures")}
    return {**data, "experiment": exp, "attachments": atts, "comments": comments, **lists}


# ---------------------------------------------------------------------------------------------------- the preview

def _brought_in(it: Item, f: CrateFile) -> bool:
    """Is this file imported as an attachment? (Not when missing or too big; nor a Benchtop record's own report,
    record file and chemistry files, which Benchtop draws again from the sections.)"""
    if f.info is None or f.size > settings.instrument_max_mb * 1024 * 1024:
        return False
    return not (it.record and (f.inner in (RECORD_FILE, "report.html") or f.inner.startswith("chemistry/")))


def _staging() -> Path:
    d = scratch_dir() / "imports"
    d.mkdir(parents=True, exist_ok=True)
    for p in d.iterdir():
        try:
            if p.stat().st_mtime < time.time() - KEEP_HOURS * 3600:
                p.unlink()
        except OSError:
            pass
    return d


def stage(src, filename: str, user: User) -> str:
    """Save an uploaded file for previewing; returns its token. `src` is a binary file object."""
    token = secrets.token_urlsafe(18)
    d = _staging()
    dst = d / f"{token}.eln"
    limit = settings.import_max_mb * 1024 * 1024
    n = 0
    from .storage import _size
    size = _size(src)
    if size is not None and size <= limit:
        diskspace.require_room(size, what="The .eln file")
    try:
        with open(dst, "wb") as out:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                n += len(chunk)
                if n > limit:
                    raise ElnImportError(f"This file is larger than the import limit ({settings.import_max_mb / 1024:g} GB).")
                out.write(chunk)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    (d / f"{token}.json").write_text(json.dumps({"user_id": user.id, "filename": filename[:300], "size": n,
                                                  "created": time.time()}))
    return token


def staged(token: str, user: User) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", token or ""):
        raise ElnImportError("That import has expired; choose the file again.")
    d = _staging()
    try:
        meta = json.loads((d / f"{token}.json").read_text())
    except (OSError, ValueError):
        raise ElnImportError("That import has expired; choose the file again.") from None
    if meta.get("user_id") != user.id or not (d / f"{token}.eln").exists():
        raise ElnImportError("That import has expired; choose the file again.")
    return d / f"{token}.eln", meta


def discard(token: str, user: User) -> None:
    try:
        path, _ = staged(token, user)
    except ElnImportError:
        return
    path.unlink(missing_ok=True)
    path.with_suffix(".json").unlink(missing_ok=True)


def preview(session: Session, user: User, token: str) -> dict[str, Any]:
    from .access import access_for
    path, meta = staged(token, user)
    crate = Crate(path)
    try:
        items = crate.items()
        acc = access_for(session, user)
        out = []
        for it in items:
            prior = session.exec(select(Experiment).where(Experiment.origin_key == crate.origin_key(it))).all()
            seen = next((e for e in prior if acc.can_read(e)), None)
            usable = [f for f in it.files if _brought_in(it, f)]
            out.append({
                "key": it.key, "title": it.title, "identifier": _str(it.node.get("identifier"), 120),
                "created": _date((it.record or {}).get("experiment", {}).get("created_at") or it.node.get("dateCreated")),
                "authors": crate.authors(it), "files": len(usable), "bytes": sum(f.size for f in usable),
                "locked": bool(it.lock_reason), "lock_reason": it.lock_reason, "from_benchtop": it.record is not None,
                "warnings": it.warnings, "already": {"id": seen.id, "eid": seen.eid} if seen else None,
            })
        return {"token": token, "filename": meta.get("filename"), "size": meta.get("size"), "source": crate.source,
                "source_url": crate.source_url, "exported": crate.exported, "items": out}
    finally:
        crate.close()


# ---------------------------------------------------------------------------------------------------- importing

def _store(crate: Crate, f: CrateFile) -> tuple[str, int]:
    """Copy one file out of the archive into the attachment store, without holding the database. Returns (sha256, size)."""
    from .instruments import _store as store_tmp
    diskspace.require_room(f.info.file_size, what=f"“{f.name}”")
    tmp = scratch_dir() / f"import-{os.getpid()}-{secrets.token_hex(6)}.part"
    limit = settings.instrument_max_mb * 1024 * 1024
    n = 0
    try:
        with crate.z.open(f.info) as src, open(tmp, "wb") as out:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                n += len(chunk)
                if n > limit:
                    raise ElnImportError(f"{f.name} is larger than it said")
                out.write(chunk)
        return store_tmp(tmp)
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise ElnImportError(f"{f.name} is damaged inside the .eln file (it doesn't match the zip's own check), "
                             "so this experiment wasn't imported. Export it again, or copy the file over again.") from None
    finally:
        tmp.unlink(missing_ok=True)


def _clean_name(name: str) -> str:
    from .routers.experiments import _clean_filename
    return _clean_filename(name)


def _text_to_html(text: str, fmt: str = "") -> str:
    """Another ELN's write-up as the HTML Benchtop's editor uses (cleaned; plain text and simple Markdown become paragraphs)."""
    from .routers.experiments import MAX_NESTING, clean_html, nesting_depth
    text = text or ""
    looks_html = "html" in fmt.lower() or re.search(r"<(p|div|h[1-6]|ul|ol|table|br|span|strong|em)\b", text, re.I)
    if looks_html:
        h = clean_html(text)
        if nesting_depth(h) <= MAX_NESTING:
            return h
        text = re.sub(r"<[^<>]+>", " ", text)  # stops at the next "<": one pass, whatever the text
    out, para, items = [], [], []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline_md(x) for x in para) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline_md(x)}</li>" for x in items) + "</ul>")
            items.clear()
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if not s:
            flush()
        elif m:
            flush()
            out.append(f"<h{min(len(m.group(1)) + 1, 4)}>{_inline_md(m.group(2))}</h{min(len(m.group(1)) + 1, 4)}>")
        elif re.match(r"^[-*•]\s+", s):
            if para:
                flush()
            items.append(re.sub(r"^[-*•]\s+", "", s))
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return clean_html("".join(out))


def _inline_md(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)


def _link_images(h: str, files: dict[str, int]) -> str:
    """A picture in another ELN's write-up points at that ELN (eLabFTW: app/download.php?name=x.jpg…). When a file of the
    same name came across, show that file there; otherwise say what the picture was rather than leave a blank."""
    by_len = sorted(((n.lower(), i) for n, i in files.items() if n), key=lambda x: -len(x[0]))

    def sub(m: re.Match) -> str:
        tag = m.group(0)
        src = re.search(r'\bsrc="([^"]*)"', tag)
        alt = re.search(r'\balt="([^"]*)"', tag)
        s = unquote(html.unescape(src.group(1))) if src else ""
        if s.startswith("data:image/") or "data-attachment-id" in tag:
            return tag
        hit = next(((n, i) for n, i in by_len if n in s.lower()), None)
        if hit:
            return f'<img data-attachment-id="{hit[1]}" alt="{html.escape(hit[0])}">'
        label = (alt.group(1) if alt and alt.group(1) else "") or PurePosixPath(s.split("?")[0]).name or "picture"
        return f"<em>[picture: {html.escape(label)}]</em>"
    return re.sub(r"<img\b[^>]*>", sub, h)


def _value(crate: Crate, v: Any) -> str:
    if isinstance(v, dict) and "@id" in v and len(v) == 1:
        return crate.name_of(v["@id"])
    if isinstance(v, list):
        return ", ".join(_value(crate, x) for x in v)[:2000]
    return _str(v)


def _details(crate: Crate, it: Item, titles: dict[str, str]) -> list[list[str]]:
    """Everything descriptive about the record that has no other place, as rows of a table."""
    n = it.node
    rows: list[list[str]] = []

    def add(name, value, unit=""):
        value = _str(value, 2000) if not isinstance(value, str) else value[:2000]
        if value:
            rows.append([name, value, unit])
    add("Original ID", n.get("identifier"))
    add("Original link", n.get("url"))
    add(f"Status in {crate.source}", n.get("creativeWorkStatus"))
    add("Kind", n.get("genre"))
    for ref in _refs(n.get("about")):
        add("Category", crate.name_of(ref))
    add("Created", _date(n.get("dateCreated")))
    add("Last changed", _date(n.get("dateModified")))
    add("Date", _date(n.get("temporal")) if n.get("temporal") else None)
    add("Authors", ", ".join(crate.authors(it)))
    for ref in _refs(n.get("mentions")):
        target = crate.by_id.get(ref, {})
        if "Dataset" in _types(target):
            add("Linked record", titles.get(ref) or crate.name_of(ref))
        elif target:
            label = crate.name_of(ref)
            smiles = _str(target.get("smiles"), 500)
            add("Mentions", f"{label} ({smiles})" if smiles else label)
    for ref in it.children:
        add("Contains", titles.get(ref) or crate.name_of(ref))
    for name, url in it.links:
        add("Linked file (on the web)", f"{name}: {url}" if name != url else url)
    for ref in _refs(n.get("variableMeasured")):
        pv = crate.by_id.get(ref, {})
        if not pv:
            continue
        key = _str(pv.get("propertyID"), 300)
        val = _value(crate, pv.get("value"))
        if key.endswith("_metadata") and val.startswith("{"):
            continue  # a whole JSON document: kept in original-record.json, unreadable in a table
        add(_str(pv.get("name"), 300) or key or "Value", val, _str(pv.get("unitText") or pv.get("unitCode"), 40))
    return rows


def _steps_html(crate: Crate, node: dict[str, Any]) -> str:
    steps = [crate.by_id.get(r, {}) for r in _refs(node.get("step"))]
    steps = sorted([s for s in steps if s], key=lambda s: s.get("position") if isinstance(s.get("position"), (int, float)) else 0)
    lis = []
    for s in steps:
        txt = " ".join(_str(crate.by_id.get(r, {}).get("text")) for r in _refs(s.get("itemListElement"))) or _str(s.get("text") or s.get("name"))
        done = _str(s.get("creativeWorkStatus")).lower() in ("finished", "done", "complete", "completed")
        lis.append(f"<li>{'✓ ' if done else ''}{html.escape(txt)}</li>")
    return f"<ol>{''.join(lis)}</ol>" if lis else ""


def _comments_html(crate: Crate, node: dict[str, Any]) -> str:
    out = []
    for r in _refs(node.get("comment")):
        c = crate.by_id.get(r, {})
        text = _str(c.get("text") or c.get("description"), 20000)
        if not text:
            continue
        who = ", ".join(crate.name_of(a) for a in _refs(c.get("author"))) or "Someone"
        when = (_date(c.get("dateCreated")) or "")[:10]
        out.append(f"<p><strong>{html.escape(who)}</strong>{' · ' + when if when else ''}<br>{html.escape(text)}</p>")
    return "".join(out)


def _original(crate: Crate, it: Item) -> bytes:
    """The record's own entry and everything it points at (people, comments, fields, steps), as the other ELN wrote it."""
    keep, todo = [], [it.node_id] if it.node else []
    seen: set[str] = set()
    while todo:
        i = todo.pop()
        if i in seen or i not in crate.by_id:
            continue
        seen.add(i)
        node = crate.by_id[i]
        keep.append(node)
        for k, v in node.items():
            if k in ("hasPart",) and i != it.node_id:
                continue
            for r in _refs(v):
                if r in crate.by_id and ("Dataset" not in _types(crate.by_id[r]) or r == it.node_id):
                    todo.append(r)
    doc = {"source": crate.source, "source_url": crate.source_url, "exported": crate.exported, "@graph": keep}
    return json.dumps(doc, ensure_ascii=False, indent=1).encode()


def _remap(content: Any, ids: dict[int, int]) -> Any:
    """A Benchtop section, re-pointed at the new attachments; links to the other lab's inventory and records dropped."""
    if isinstance(content, dict):
        out = {}
        for k, v in content.items():
            if k in ("material_id", "lot_id", "experiment_id"):
                out[k] = None
            elif k == "attachment_id":
                out[k] = ids.get(v) if isinstance(v, int) else None
            elif k == "attachment_ids" and isinstance(v, list):
                out[k] = [ids[x] for x in v if isinstance(x, int) and x in ids]
            elif k == "analyses" and isinstance(v, dict):
                out[k] = {str(ids[int(a)]): _remap(b, ids) for a, b in v.items() if str(a).isdigit() and int(a) in ids}
            else:
                out[k] = _remap(v, ids)
        return out
    if isinstance(content, list):
        return [_remap(x, ids) for x in content]
    if isinstance(content, str) and "data-attachment-id" in content:
        return re.sub(r'data-attachment-id="(\d+)"', lambda m: f'data-attachment-id="{ids.get(int(m.group(1)), 0)}"', content)
    return content


def _benchtop_checks(it: Item, stored: dict[str, str]) -> list[dict[str, Any]]:
    """For a record from another Benchtop: does the exported content match the fingerprint each signature carries?"""
    from .audit import canonical
    rec = it.record or {}
    snap = rec.get("snapshot")
    out = []
    if not isinstance(snap, dict):
        return out
    h = hashlib.sha256(canonical(snap).encode()).hexdigest()
    files_ok = all(isinstance(a, dict) and a.get("sha256") in stored.values() for a in snap.get("attachments") or [])
    entries_ok = [(e.get("kind"), e.get("title"), e.get("content")) for e in snap.get("entries") or [] if isinstance(e, dict)] == \
        [(e.get("kind"), e.get("title"), e.get("content")) for e in rec.get("entries") or [] if isinstance(e, dict)]
    for s in rec.get("signatures") or []:
        if not isinstance(s, dict):
            continue
        out.append({"meaning": _str(s.get("meaning"), 20), "signer": _str(s.get("signer_name"), 200),
                    "signed_at": _str(s.get("signed_at"), 40), "comment": _str(s.get("comment"), 500),
                    "matches": bool(s.get("content_hash") == h and files_ok and entries_ok),
                    "timestamp": bool((s.get("timestamp") or {}).get("status") == "ok") if isinstance(s.get("timestamp"), dict) else False})
    return out


def _new_entry(session: Session, exp: Experiment, kind: EntryKind, title: str, content: dict, pos: int, user: User) -> Entry:
    e = Entry(experiment_id=exp.id, kind=kind, title=title[:300], content=content, position=pos, updated_by=user.id)
    session.add(e)
    session.flush()
    session.add(EntryVersion(entry_id=e.id, version=1, title=e.title, content=content, created_by=user.id))
    return e


def import_items(session: Session, user: User, token: str, notebook: Notebook, keys: list[str]) -> dict[str, Any]:
    path, meta = staged(token, user)
    # one import per upload at a time: a double click, a second tab or a retried request mustn't make the records twice
    busy = path.with_suffix(".importing")
    try:
        fd = os.open(busy, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ElnImportError("This file is already being imported; its experiments will appear in the notebook when it's done.") from None
    os.close(fd)
    try:
        path, meta = staged(token, user)  # the import just before this one may have finished (and tidied the file away)
        return _import_items(session, user, path, meta, token, notebook, keys)
    finally:
        busy.unlink(missing_ok=True)


def _import_items(session: Session, user: User, path: Path, meta: dict[str, Any], token: str, notebook: Notebook,
                  keys: list[str]) -> dict[str, Any]:
    crate = Crate(path)
    created, failed = [], []
    try:
        everything = crate.items()
        items = [it for it in everything if it.key in set(keys)]
        titles = {it.node_id: it.title for it in everything}
        new_of: dict[str, Experiment] = {}
        for it in items:
            try:
                exp = _import_one(session, user, crate, it, notebook, titles, meta)
                new_of[it.node_id] = exp
                created.append({"id": exp.id, "eid": exp.eid, "title": exp.title, "locked": exp.status == ExperimentStatus.locked,
                                "warnings": (exp.origin or {}).get("warnings", [])})
            except Exception as e:  # noqa: BLE001 - one bad record mustn't stop the others; say which and why
                session.rollback()
                msg = str(e) if isinstance(e, (ElnImportError, OSError)) else f"{type(e).__name__}: {e}"
                if isinstance(e, OSError) and e.errno == 28 and not isinstance(e, diskspace.DiskFull):
                    msg = "the disk is full"
                failed.append({"title": it.title, "error": msg[:300]})
        # a Benchtop follow-up whose original came in the same import is linked to it again
        by_eid = {it.record["experiment"]["eid"]: new_of[it.node_id]
                  for it in items if it.record and it.node_id in new_of and it.record["experiment"]["eid"]}
        for it in items:
            follows = ((it.record or {}).get("experiment") or {}).get("follows")
            if it.node_id in new_of and follows and follows in by_eid:
                new_of[it.node_id].follows_id = by_eid[follows].id
                session.add(new_of[it.node_id])
        session.commit()
    finally:
        crate.close()
    if created and not failed:
        discard(token, user)
    return {"created": created, "failed": failed}


def _import_one(session: Session, user: User, crate: Crate, it: Item, notebook: Notebook, titles: dict[str, str],
                meta: dict[str, Any]) -> Experiment:
    from fastapi import HTTPException

    from .routers.experiments import _clean_content
    # 1. files into storage first, with nothing held in the database (big files take a while)
    stored: dict[str, str] = {}
    sizes: dict[str, int] = {}
    checks: list[list[str]] = []
    warnings = list(it.warnings)
    mismatched = 0
    for f in it.files:
        if not _brought_in(it, f):
            continue
        sha, size = _store(crate, f)
        stored[f.node_id], sizes[f.node_id] = sha, size
        if f.sha256 and f.sha256 != sha:
            mismatched += 1
            verdict = f"✗ doesn't match: {crate.source} recorded {f.sha256[:12]}…, the file is {sha[:12]}…"
        elif f.sha256:
            verdict = "✓ matches"
        else:
            verdict = "not given by the source" if f.described else "not listed by the source (found in the folder)"
        checks.append([f"{f.folder}/{f.name}" if f.folder else f.name, f"{size:,} bytes", verdict])
    if mismatched:
        warnings.append(f"{mismatched} file{'s' if mismatched != 1 else ''} didn't match the fingerprint {crate.source} recorded: "
                        "they may have been changed or damaged after export. They're imported and marked in the Files table.")
    original = _original(crate, it) if it.node and not it.record else b""  # a Benchtop record maps across completely
    orig_sha = None
    if original:
        from .instruments import _store as store_tmp
        tmp = scratch_dir() / f"import-{os.getpid()}-{secrets.token_hex(6)}.part"
        tmp.write_bytes(original)
        orig_sha, _ = store_tmp(tmp)

    # 2. one short transaction for the record itself
    audit._take_write_lock(session)
    rec = (it.record or {}).get("experiment") or {}
    node = it.node
    desc = _str(rec.get("description") if it.record else node.get("description"), 20000)
    keywords = rec.get("tags") if it.record and isinstance(rec.get("tags"), list) else \
        [k.strip() for k in re.split(r"[,;]", _str(node.get("keywords"), 4000)) if k.strip()]
    lock = it.lock_reason
    sig_checks = _benchtop_checks(it, stored) if it.record else []
    origin = {
        "source": crate.source, "source_url": crate.source_url, "file": meta.get("filename"), "exported": crate.exported,
        "original_id": _str(rec.get("eid") or node.get("identifier"), 200), "original_url": _str(node.get("url"), 500),
        "original_title": it.title, "original_created": _date(rec.get("created_at") or node.get("dateCreated")),
        "original_modified": _date(rec.get("updated_at") or node.get("dateModified")), "authors": crate.authors(it),
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "imported_by": user.full_name or user.username,
        "locked_reason": lock, "signatures": sig_checks, "warnings": warnings,
        "files": {"imported": len(stored), "matched": sum(1 for c in checks if c[2].startswith("✓")), "mismatched": mismatched},
    }
    exp = Experiment(title=it.title[:300], notebook_id=notebook.id, description=desc[:300] if len(desc) <= 300 else "",
                     owner_id=user.id, tags=[str(k)[:60] for k in keywords][:40], origin=origin, origin_key=crate.origin_key(it))
    session.add(exp)
    session.flush()

    by_node: dict[str, int] = {}
    old_ids: dict[int, int] = {}
    names_used: set[str] = set()
    rec_atts = {a.get("sha256"): a for a in (it.record or {}).get("attachments") or [] if isinstance(a, dict)}
    for f in it.files:
        sha = stored.get(f.node_id)
        if sha is None:
            continue
        name = _clean_name(f.name)
        if name.lower() in names_used and f.folder:
            name = _clean_name(f"{f.folder.replace('/', ' - ')} - {f.name}")
        names_used.add(name.lower())
        ra = rec_atts.get(sha)
        a = Attachment(experiment_id=exp.id, filename=_clean_name(ra.get("filename")) if ra and ra.get("filename") else name,
                       content_type=(ra or {}).get("content_type") or f.content_type, size=sizes[f.node_id], sha256=sha, uploaded_by=user.id)
        session.add(a)
        session.flush()
        by_node[f.node_id] = a.id
        if ra and isinstance(ra.get("id"), int):
            old_ids[ra["id"]] = a.id
        audit.record(session, user, "upload", "attachment", a.id, experiment=exp.eid, filename=a.filename, sha256=sha,
                     size=a.size, imported_from=crate.source)
    if orig_sha:
        oa = Attachment(experiment_id=exp.id, filename="original-record.json", content_type="application/json",
                        size=len(original), sha256=orig_sha, uploaded_by=user.id)
        session.add(oa)
        session.flush()
        by_node["#original"] = oa.id

    pos = 0
    if it.record:
        for e in it.record.get("entries") or []:
            if not isinstance(e, dict):
                continue
            try:
                kind = EntryKind(e.get("kind"))
                content = _clean_content(kind, _remap(e.get("content") or {}, old_ids))
            except (ValueError, HTTPException):
                kind, content = EntryKind.text, {"html": "<p><em>This section couldn't be brought across; its original is in "
                                                          "original-record.json.</em></p>"}
            _new_entry(session, exp, kind, _str(e.get("title"), 300), content, pos, user)
            pos += 1
        if len(desc) > 300:
            _new_entry(session, exp, EntryKind.text, "Description", {"html": _text_to_html(desc)}, pos, user)
            pos += 1
        comments = [c for c in it.record.get("comments") or [] if isinstance(c, dict) and c.get("body")]
        if comments:
            body = "".join(f"<p><strong>{html.escape(_str((c.get('author') or {}).get('name')) or 'Someone')}</strong> · "
                           f"{html.escape(_str(c.get('created_at'))[:10])}<br>{html.escape(_str(c.get('body'), 20000))}</p>" for c in comments)
            _new_entry(session, exp, EntryKind.text, f"Comments from {crate.source}", {"html": _text_to_html(body, "html")}, pos, user)
            pos += 1
    else:
        text = node.get("text")
        fmt = _str(node.get("encodingFormat"), 80)
        body = ""
        if len(desc) > 300:
            body += _text_to_html(desc)
        if text:
            body += _text_to_html(_str(text, 5_000_000), fmt)
        for ref in _refs(node.get("hasPart")):  # some ELNs (Kadi4Mat) keep the write-up as a TextObject
            t = crate.by_id.get(ref, {})
            if "TextObject" in _types(t) and t.get("text"):
                body += _text_to_html(_str(t["text"], 1_000_000), _str(t.get("encodingFormat"), 80))
        if body:
            names = {session.get(Attachment, i).filename: i for k, i in by_node.items() if k != "#original"}
            body = _text_to_html(_link_images(body, names), "html")
            _new_entry(session, exp, EntryKind.text, f"Notes from {crate.source}", {"html": body}, pos, user)
            pos += 1
        steps = _steps_html(crate, node)
        if steps:
            _new_entry(session, exp, EntryKind.text, "Steps", {"html": _text_to_html(steps, "html")}, pos, user)
            pos += 1
        details = _details(crate, it, titles)
        if details:
            _new_entry(session, exp, EntryKind.table, f"Details from {crate.source}",
                       _clean_content(EntryKind.table, {"columns": [{"name": "Property"}, {"name": "Value"}, {"name": "Unit"}], "rows": details}),
                       pos, user)
            pos += 1
        comments = _comments_html(crate, node)
        if comments:
            _new_entry(session, exp, EntryKind.text, f"Comments from {crate.source}", {"html": _text_to_html(comments, "html")}, pos, user)
            pos += 1
    files = [i for k, i in by_node.items() if k != "#original"]
    if not it.record and (files or by_node.get("#original")):
        if checks:
            _new_entry(session, exp, EntryKind.table, "Imported files: fingerprint check",
                       _clean_content(EntryKind.table, {"columns": [{"name": "File"}, {"name": "Size"}, {"name": "SHA-256 check"}], "rows": checks}),
                       pos, user)
            pos += 1
        caption = (f"Imported from {crate.source}" + (f" ({meta.get('filename')})" if meta.get("filename") else "") +
                   ". original-record.json holds the record as the other ELN described it.")
        _new_entry(session, exp, EntryKind.file, "Files", {"attachment_ids": files + ([by_node["#original"]] if "#original" in by_node else []),
                                                           "caption": caption}, pos, user)
        pos += 1
    if lock:
        exp.status = ExperimentStatus.locked
    exp.updated_at = utcnow()
    session.add(exp)
    audit.record(session, user, "import_eln", "experiment", exp.id, eid=exp.eid, title=exp.title, source=crate.source,
                 original_id=origin["original_id"], file=meta.get("filename"), files=len(stored), locked=bool(lock),
                 mismatched=mismatched)
    session.commit()
    session.refresh(exp)
    return exp
