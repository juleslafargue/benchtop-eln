"""The .eln file format (the ELN Consortium's shared format): export Benchtop records so any ELN can read them.

A .eln file is a zip with one folder in it. In that folder, ro-crate-metadata.json describes each experiment (title,
people, dates, the write-up, comments, tags, extra fields) and lists the files beside it, each with its SHA-256.
Specification: https://github.com/TheELNConsortium/TheELNFileFormat (RO-Crate 1.2, ELN format 1.2+20260923).

Each experiment gets a folder, "EXP-00042 - Suzuki coupling/", holding:
  report.html            the printable record, readable in any browser without Benchtop
  record.benchtop.json   everything Benchtop knows about it, so a Benchtop can import it back without losing anything
  files/…                every attached file, unchanged (files sent back with "Not mine" are left out)
  chemistry/…            the reaction (.rxn) and each compound (.mol), for ChemDraw, RDKit and chemistry ELNs
  signatures/…           each signature's trusted-timestamp proof, checkable with openssl, when there is one

Importing is in eln_import.py.
"""
from __future__ import annotations

import html
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from sqlmodel import Session, select

from . import __version__
from .config import settings
from .models import Attachment, Comment, EntryKind, Experiment, Notebook, SampleCode, User
from .services import content_hash, experiment_summary, live_entries, signatures_out, snapshot

CONTEXT = "https://w3id.org/ro/crate/1.2/context"
CRATE = "https://w3id.org/ro/crate/1.2"
ELN_SPEC = "https://purl.archive.org/purl/elnconsortium/eln-spec/1.2+20260923"
PUBLISHER = "#benchtop-eln"
SOFTWARE = "https://github.com/juleslafargue/benchtop-eln"
RECORD_FILE = "record.benchtop.json"
RECORD_FORMAT = "benchtop-record"
# already-compressed formats are stored as they are: squeezing them again only costs time
STORED_EXT = re.compile(r"\.(zip|gz|tgz|bz2|xz|7z|rar|jpe?g|png|gif|webp|heic|mp4|mov|avi|mkv|pdf|docx|xlsx|pptx|d|raw|wiff2?)$", re.I)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\.|$)", re.I)  # names Windows keeps for itself
MAX_NAME_BYTES = 200  # every system allows 255 bytes per name; a character can take up to 4 of them (Chinese, emoji)


def _cut(s: str, limit: int) -> str:
    """At most `limit` characters and MAX_NAME_BYTES bytes, never splitting a character."""
    s = s[:limit]
    while len(s.encode()) > MAX_NAME_BYTES:
        s = s[:-1]
    return s


def _safe(name: str, limit: int = 80) -> str:
    """A file or folder name that works on Mac, Windows and Linux: no characters they refuse, not too long (the file
    extension is kept when a long name is shortened), and none of the names Windows reserves."""
    s = re.sub(r'[\\/:*?"<>|#^\[\]{}%\x00-\x1f\x7f]+', " ", name or "").strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    stem, dot, ext = s.rpartition(".")
    if dot and stem and 0 < len(ext) <= 10 and " " not in ext:
        ext = "." + ext
        s = _cut(stem, max(1, limit - len(ext))).strip(" .") + ext
    else:
        s = _cut(s, limit).strip(" .")
    if _RESERVED.match(s):
        s = "_" + s
    return s or "untitled"


def _ref(i: str) -> dict[str, str]:
    return {"@id": i}


def _id(path: str) -> str:
    """The @id of something in the crate: its path, percent-encoded as RO-Crate asks (spaces become %20)."""
    return "./" + quote(path, safe="/-_.~()'!*,;=+@")


def _clean(node: dict[str, Any]) -> dict[str, Any]:
    """No empty values: the format's checker warns about them, and they say nothing."""
    return {k: v for k, v in node.items() if v not in (None, "", [], {})}


class _Unique:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def __call__(self, name: str) -> str:
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        candidate, n = name, 1
        while candidate.lower() in self.seen:
            n += 1
            candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        self.seen.add(candidate.lower())
        return candidate


def _blob(a: Attachment) -> Path:
    return settings.attachments_dir / a.sha256[:2] / a.sha256


class _Crate:
    def __init__(self, z: zipfile.ZipFile, root: str):
        self.z, self.root = z, root
        self.graph: dict[str, dict[str, Any]] = {}
        self.people: dict[int, str] = {}

    def add(self, node: dict[str, Any]) -> str:
        node = _clean(node)
        self.graph.setdefault(node["@id"], node)
        return node["@id"]

    def write_bytes(self, path: str, data: bytes) -> None:
        self.z.writestr(zipfile.ZipInfo(f"{self.root}/{path}", datetime.now().timetuple()[:6]), data,
                        compress_type=zipfile.ZIP_DEFLATED)

    def write_file(self, path: str, src: Path, name: str) -> None:
        self.z.write(src, f"{self.root}/{path}",
                     compress_type=zipfile.ZIP_STORED if STORED_EXT.search(name) else zipfile.ZIP_DEFLATED)

    def person(self, session: Session, uid: Optional[int]) -> Optional[str]:
        if uid is None:
            return None
        if uid in self.people:
            return self.people[uid]
        u = session.get(User, uid)
        if u is None:
            return None
        orcid = getattr(u, "orcid", None)
        pid = f"https://orcid.org/{orcid}" if orcid else f"#person-{u.id}"
        given, _, family = (u.full_name or "").strip().rpartition(" ")
        self.add({"@id": pid, "@type": "Person", "name": u.full_name or u.username,
                  "givenName": given or None, "familyName": family if given else None})
        self.people[uid] = pid
        return pid

    def named_person(self, name: str) -> str:
        """Someone known only by name (e.g. who signed, as printed at the time)."""
        pid = f"#person-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'unknown'}"
        self.add({"@id": pid, "@type": "Person", "name": name})
        return pid


def _pv(crate: _Crate, base: str, key: str, name: str, value: Any, unit: str = "") -> Optional[dict[str, str]]:
    if value in (None, "", []):
        return None
    pid = f"#{base}-{re.sub(r'[^a-z0-9.]+', '-', key.lower())}"
    crate.add({"@id": pid, "@type": "PropertyValue", "propertyID": key, "name": name,
               "value": value if isinstance(value, (int, float, str, bool)) else json.dumps(value), "unitText": unit or None})
    return _ref(pid)


def _yield(entries) -> Optional[float]:
    for e in entries:
        if e.kind == EntryKind.reaction:
            for r in (e.content or {}).get("rows") or []:
                if r.get("role") == "product" and isinstance(r.get("yield_pct"), (int, float)):
                    return round(float(r["yield_pct"]), 1)
    return None


def _text_html(session: Session, exp: Experiment, entries, atts: dict[int, Attachment]) -> str:
    """The write-up as an HTML fragment for other ELNs (pictures are in files/, not inlined into the metadata)."""
    from .routers.experiments import _render_entry_safe
    body = "".join(_render_entry_safe(e, atts, False) for e in entries)

    def picture(m: re.Match) -> str:
        alt = re.search(r'\balt="([^"]*)"', m.group(0))
        return f"<em>[picture: {alt.group(1) if alt else 'see the attached files'}]</em>"
    return re.sub(r'<img\b[^>]*\bsrc="data:[^"]*"[^>]*>', picture, body).strip()


def _chemistry(crate: _Crate, folder: str, base: str, entries) -> tuple[list[dict], list[dict]]:
    """Reaction and compounds as MDL files (and as MolecularEntity nodes others can search)."""
    from rdkit import Chem
    from rdkit.Chem import rdChemReactions
    parts: list[dict] = []
    mentions: list[dict] = []
    names = _Unique()
    n = 0
    for e in entries:
        if e.kind != EntryKind.reaction:
            continue
        c = e.content or {}
        rxn = c.get("reaction_smiles") or c.get("rxn_smiles") or ""
        if rxn and ">" in rxn:
            try:
                block = rdChemReactions.ReactionToRxnBlock(rdChemReactions.ReactionFromSmarts(rxn, useSmiles=True))
                path = f"{folder}/chemistry/{names('reaction.rxn')}"
                crate.write_bytes(path, block.encode())
                parts.append(_ref(crate.add({"@id": _id(path), "@type": "File", "name": path.rsplit("/", 1)[1],
                                              "encodingFormat": "chemical/x-mdl-rxnfile", "contentSize": str(len(block.encode())),
                                              "description": f"Reaction scheme ({e.title or 'Reaction'}), MDL RXN",
                                              "sha256": _sha(block.encode())})))
            except Exception:  # noqa: BLE001 - a scheme RDKit can't write is still in report.html and the record
                pass
        for r in c.get("rows") or []:
            smi = (r.get("smiles") or "").strip()
            if not smi:
                continue
            mol = Chem.MolFromSmiles(smi)
            if mol is None:
                continue
            n += 1
            label = r.get("name") or r.get("formula") or f"compound {n}"
            block = Chem.MolToMolBlock(mol).encode()
            path = f"{folder}/chemistry/{names(f'{n} {_safe(label, 60)}.mol')}"
            crate.write_bytes(path, block)
            mol_id = f"#{base}-compound-{n}"
            try:
                key = Chem.MolToInchiKey(mol)
            except Exception:  # noqa: BLE001
                key = ""
            crate.add({"@id": mol_id, "@type": "MolecularEntity", "name": label, "smiles": smi,
                       "molecularFormula": r.get("formula") or None,
                       "molecularWeight": f"{r['mw']:.3f} g/mol" if isinstance(r.get("mw"), (int, float)) else None,
                       "inChIKey": key or None, "description": r.get("role") or None})
            mentions.append(_ref(mol_id))
            parts.append(_ref(crate.add({"@id": _id(path), "@type": "File", "name": path.rsplit("/", 1)[1],
                                          "encodingFormat": "chemical/x-mdl-molfile", "contentSize": str(len(block)),
                                          "description": f"Structure of {label} ({r.get('role') or 'compound'}), MDL molfile",
                                          "sha256": _sha(block), "about": _ref(mol_id)})))
    return parts, mentions


def _sha(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


def _record(session: Session, exp: Experiment, entries, atts: list[Attachment], att_paths: dict[int, str],
            comments: list[Comment], sigs: list[dict], codes: list[SampleCode]) -> dict[str, Any]:
    """Benchtop's own full description of the record, for a lossless import into another Benchtop."""
    def who(uid):
        u = session.get(User, uid) if uid else None
        return {"name": u.full_name or u.username, "username": u.username, "orcid": getattr(u, "orcid", None)} if u else None
    follows = session.get(Experiment, exp.follows_id) if exp.follows_id else None
    nb = session.get(Notebook, exp.notebook_id) if exp.notebook_id else None
    return {
        "format": RECORD_FORMAT, "format_version": 1, "benchtop_version": __version__, "exported_at": _iso(datetime.now(timezone.utc)),
        "experiment": {**experiment_summary(session, exp), "owner": who(exp.owner_id), "notebook": nb.name if nb else None,
                       "follows": follows.eid if follows else None, "origin": getattr(exp, "origin", None)},
        "entries": [{"id": e.id, "kind": e.kind.value, "title": e.title, "position": e.position, "version": e.version,
                     "content": e.content, "updated_at": _iso(e.updated_at), "updated_by": who(e.updated_by)} for e in entries],
        "attachments": [{"id": a.id, "filename": a.filename, "content_type": a.content_type, "size": a.size, "sha256": a.sha256,
                         "path": att_paths.get(a.id), "uploaded_by": who(a.uploaded_by), "created_at": _iso(a.created_at)} for a in atts],
        "comments": [{"id": c.id, "parent_id": c.parent_id, "entry_id": c.entry_id, "author": who(c.user_id), "body": c.body,
                      "created_at": _iso(c.created_at), "resolved": c.resolved_at is not None} for c in comments],
        "signatures": sigs,
        "samples": [f"{c.prefix}-{c.number:03d}" for c in codes],
        # what each signature attests to: re-hashing this gives the content_hash the signatures carry
        "snapshot": snapshot(session, exp),
        "content_hash": content_hash(session, exp),
    }


def _experiment(session: Session, crate: _Crate, exp: Experiment, folders: _Unique,
                folder_of: dict[int, str], with_comments: bool = True) -> str:
    from . import timestamps
    from .models import Signature, SignatureTimestamp
    from .routers.experiments import report_html
    folder = folder_of[exp.id]
    ds_id = _id(folder + "/")
    base = exp.eid.lower()
    entries = live_entries(session, exp.id)
    atts = list(session.exec(select(Attachment).where(Attachment.experiment_id == exp.id, Attachment.withdrawn_at == None)  # noqa: E711
                             .order_by(Attachment.id)))
    parts: list[dict] = []

    report = report_html(session, exp).encode()
    crate.write_bytes(f"{folder}/report.html", report)
    parts.append(_ref(crate.add({"@id": _id(f"{folder}/report.html"), "@type": "File", "name": "report.html",
                                  "encodingFormat": "text/html", "contentSize": str(len(report)), "sha256": _sha(report),
                                  "description": "The printable record: every section, signatures and the content fingerprint"})))

    names = _Unique()
    att_paths: dict[int, str] = {}
    for a in atts:
        src = _blob(a)
        if not src.is_file():
            continue  # ./benchtop check reports missing files; the record still says what was attached
        path = f"{folder}/files/{names(_safe(a.filename, 150))}"
        crate.write_file(path, src, a.filename)
        att_paths[a.id] = path
        parts.append(_ref(crate.add({"@id": _id(path), "@type": "File", "name": a.filename, "encodingFormat": a.content_type,
                                      "contentSize": str(a.size), "sha256": a.sha256, "dateCreated": _iso(a.created_at),
                                      "author": _ref(crate.person(session, a.uploaded_by)) if a.uploaded_by else None,
                                      "description": "Attached file"})))

    chem_parts, mentions = _chemistry(crate, folder, base, entries)
    cited, other_refs = _citations(crate, base, entries)
    mentions += other_refs
    parts += chem_parts

    comments = list(session.exec(select(Comment).where(Comment.experiment_id == exp.id, Comment.deleted == False)  # noqa: E712
                                 .order_by(Comment.id))) if with_comments else []
    comment_refs = []
    for c in comments:
        cid = f"#{base}-comment-{c.id}"
        crate.add({"@id": cid, "@type": "Comment", "text": c.body, "dateCreated": _iso(c.created_at),
                   "author": _ref(crate.person(session, c.user_id))})
        comment_refs.append(_ref(cid))

    sigs = signatures_out(session, exp.id)
    rows = {s.id: s for s in session.exec(select(Signature).where(Signature.experiment_id == exp.id))}
    stamps = {t.signature_id: t for t in session.exec(select(SignatureTimestamp).where(
        SignatureTimestamp.signature_id.in_(list(rows)), SignatureTimestamp.status == "ok"))} if rows else {}
    variables: list[Optional[dict]] = []
    for i, s in enumerate(sigs):
        t = stamps.get(s["id"])
        if t is not None:
            try:
                proof = timestamps.proof_zip(session, rows[s["id"]], t)
                path = f"{folder}/signatures/{exp.eid}-signature-{s['id']}-timestamp.zip"
                crate.write_bytes(path, proof)
                parts.append(_ref(crate.add({"@id": _id(path), "@type": "File", "name": path.rsplit("/", 1)[1],
                                              "encodingFormat": "application/zip", "contentSize": str(len(proof)), "sha256": _sha(proof),
                                              "description": f"Trusted timestamp (RFC 3161) for the {s['meaning']} signature by "
                                                             f"{s['signer_name']}: check it with openssl (README inside)"})))
            except Exception:  # noqa: BLE001 - the signature itself is still in the record and the report
                pass
        variables += [
            _pv(crate, base, f"signature.{i}.meaning", "Signature", s["meaning"]),
            _pv(crate, base, f"signature.{i}.signer", "Signed by", s["signer_name"]),
            _pv(crate, base, f"signature.{i}.signed_at", "Signed at", s["signed_at"]),
            _pv(crate, base, f"signature.{i}.content_sha256", "Content fingerprint signed (SHA-256)", s["content_hash"]),
        ]

    codes = list(session.exec(select(SampleCode).where(SampleCode.experiment_id == exp.id, SampleCode.retired_at == None)  # noqa: E711
                              .order_by(SampleCode.prefix, SampleCode.number)))
    record = json.dumps(_record(session, exp, entries, atts, att_paths, comments, sigs, codes),
                        ensure_ascii=False, indent=1, default=str).encode()
    crate.write_bytes(f"{folder}/{RECORD_FILE}", record)
    parts.append(_ref(crate.add({"@id": _id(f"{folder}/{RECORD_FILE}"), "@type": "File", "name": RECORD_FILE,
                                  "encodingFormat": "application/json", "contentSize": str(len(record)), "sha256": _sha(record),
                                  "description": "Benchtop's full record (sections, tables, reaction data, signatures), for importing "
                                                 "into another Benchtop without losing anything"})))

    nb = session.get(Notebook, exp.notebook_id) if exp.notebook_id else None
    cond = next(((e.content or {}).get("conditions") or {} for e in entries if e.kind == EntryKind.reaction), {})
    variables += [
        _pv(crate, base, "status", "Status", exp.status.value),
        _pv(crate, base, "notebook", "Notebook", nb.name if nb else None),
        _pv(crate, base, "yield", "Yield", _yield(entries), "%"),
        _pv(crate, base, "temperature", "Temperature", cond.get("temperature_c"), "°C"),
        _pv(crate, base, "time", "Reaction time", cond.get("time_h"), "h"),
        _pv(crate, base, "atmosphere", "Atmosphere", cond.get("atmosphere")),
        _pv(crate, base, "content_sha256", "Content fingerprint (SHA-256)", content_hash(session, exp)),
        *[_pv(crate, base, f"sample.{i}", "Sample code", f"{c.prefix}-{c.number:03d}") for i, c in enumerate(codes)],
    ]
    follows = session.get(Experiment, exp.follows_id) if exp.follows_id else None
    if follows is not None:
        if follows.id in folder_of:
            mentions.append(_ref(_id(folder_of[follows.id] + "/")))
        variables.append(_pv(crate, base, "follows", "Follows up", follows.eid))

    crate.add({
        "@id": ds_id, "@type": "Dataset", "genre": "experiment", "name": exp.title, "identifier": exp.eid,
        "alternateName": exp.eid, "description": exp.description or None,
        "author": _ref(crate.person(session, exp.owner_id)),
        "dateCreated": _iso(exp.created_at), "dateModified": _iso(exp.updated_at),
        "keywords": ", ".join(t.replace(",", " ") for t in (exp.tags or []) if t) or None,
        "creativeWorkStatus": exp.status.value, "text": _text_html(session, exp, entries, {a.id: a for a in atts}),
        "encodingFormat": "text/html",
        "url": f"{settings.public_url}/experiments/{exp.id}" if settings.public_url.startswith("http") else None,
        "comment": comment_refs, "mentions": mentions, "isBasedOn": _ref(_id(folder_of[follows.id] + "/"))
        if follows is not None and follows.id in folder_of else None,
        "variableMeasured": [v for v in variables if v], "hasPart": parts, "citation": cited,
    })
    return ds_id


def _author(crate: _Crate, written: str) -> str:
    """A paper's author ("Gottlieb, Hugo E.", as a References section stores them) as a Person node."""
    family, _, given = written.strip().partition(",")
    family, given = family.strip(), given.strip()
    name = f"{given} {family}".strip() if given else family
    pid = f"#author-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'unknown'}"
    crate.add({"@id": pid, "@type": "Person", "name": name, "familyName": family if given else None, "givenName": given or None})
    return pid


def _citations(crate: _Crate, base: str, entries) -> tuple[list[dict], list[dict]]:
    """Papers in References sections, as ScholarlyArticle nodes. Those with a DOI are the record's citations, with the
    DOI as @id (RO-Crate: a citation must be an absolute URI); the rest (typed in, no DOI) are only mentioned."""
    from . import doi
    refs, others, n = [], [], 0
    for e in entries:
        if e.kind != EntryKind.references:
            continue
        for it in (e.content or {}).get("items") or []:
            if doi.is_blank(it):
                continue
            n += 1
            c = doi.cite(it)
            node_id = c["link"] or f"#{base}-reference-{n}"
            authors = [_ref(_author(crate, a)) for a in it.get("authors") or [] if isinstance(a, str) and a.strip() and a.strip() != "et al."]
            crate.add({"@id": node_id, "@type": "ScholarlyArticle", "name": c["title"] or None, "identifier": c["link"] or None,
                       "author": authors or None, "isPartOf": c["journal"] or None, "datePublished": c["year"] or None,
                       "volumeNumber": c["volume"] or None, "issueNumber": c["issue"] or None, "pagination": c["pages"] or None,
                       "description": c["note"] or None})
            into = refs if c["link"] else others
            if {"@id": node_id} not in into:
                into.append(_ref(node_id))
    return refs, others


def _preview(title: str, exps: list[Experiment], folder_of: dict[int, str]) -> bytes:
    rows = "".join(f'<li><a href="{html.escape(quote(folder_of[e.id]))}/report.html">{html.escape(e.eid)}</a> '
                   f"{html.escape(e.title)} <small>({html.escape(e.status.value)})</small></li>" for e in exps)
    return (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}}</style>"
            f"<h1>{html.escape(title)}</h1><p>Exported from Benchtop ELN {__version__} on "
            f"{datetime.now(timezone.utc):%d %b %Y}. Open any record's report to read it; each folder also holds its files.</p>"
            f"<ul>{rows}</ul>").encode()


def export(session: Session, user: User, exps: Iterable[Experiment], out: Path, name: str,
           description: str = "", with_comments: bool = True) -> dict[str, Any]:
    """Write a .eln file with these experiments to `out`. The caller has checked the user may read each of them.
    with_comments=False leaves the discussion out (publishing on Zenodo, unless the author chooses to include it)."""
    exps = list(exps)
    root = _safe(name, 120)
    folders = _Unique()
    folder_of = {e.id: folders(f"{e.eid} - {_safe(e.title, 70)}") for e in exps}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as z:
        crate = _Crate(z, root)
        now = _iso(datetime.now(timezone.utc))
        crate.add({"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": _ref("./"),
                   "conformsTo": _ref(CRATE), "dateCreated": now, "sdPublisher": _ref(PUBLISHER), "version": "1.0"})
        crate.add({"@id": CRATE, "@type": ["CreativeWork", "Profile"], "name": "RO-Crate 1.2 Specification"})
        crate.add({"@id": ELN_SPEC, "@type": ["CreativeWork", "Profile"], "name": "ELN-File Format 1.2+20260923 Specification"})
        crate.add({"@id": PUBLISHER, "@type": "Organization", "name": "Benchtop ELN", "url": SOFTWARE,
                   "description": "Open-source electronic lab notebook for chemistry"})
        crate.add({"@id": f"{SOFTWARE}/releases/tag/v{__version__}", "@type": "SoftwareApplication",
                   "name": "Benchtop ELN", "version": __version__, "url": SOFTWARE})
        parts = [_ref(_experiment(session, crate, e, folders, folder_of, with_comments)) for e in exps]
        crate.add({"@id": "#export", "@type": "CreateAction", "name": "Export from Benchtop", "endTime": now,
                   "agent": _ref(crate.person(session, user.id)), "instrument": _ref(f"{SOFTWARE}/releases/tag/v{__version__}"),
                   "result": _ref("./")})
        preview = _preview(root, exps, folder_of)
        crate.write_bytes("ro-crate-preview.html", preview)
        root_node = {"@id": "./", "@type": "Dataset", "conformsTo": [_ref(CRATE), _ref(ELN_SPEC)], "name": f"{root}.eln",
                     "description": description or f"{len(exps)} experiment{'s' if len(exps) != 1 else ''} exported from Benchtop ELN",
                     "datePublished": now, "license": None, "hasPart": parts,
                     "mentions": _ref("#export")}  # how this export was made (RO-Crate: the root mentions its actions)
        graph = [crate.graph.pop("ro-crate-metadata.json"), _clean(root_node), *crate.graph.values()]
        meta = json.dumps({"@context": CONTEXT, "@graph": graph}, ensure_ascii=False, indent=1).encode()
        crate.write_bytes("ro-crate-metadata.json", meta)
    return {"file": out, "name": f"{root}.eln", "experiments": len(exps), "size": out.stat().st_size}


def scratch_dir() -> Path:
    """Where exports are built (beside the data, so a big one doesn't fill a small system disk); old ones are swept."""
    d = settings.data_dir / "eln-tmp"
    d.mkdir(parents=True, exist_ok=True)
    import time
    for p in d.iterdir():
        try:
            if p.stat().st_mtime < time.time() - 86400:
                p.unlink()
        except OSError:
            pass
    return d
