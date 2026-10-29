"""Full-text and structure search across experiments and the materials registry."""
from __future__ import annotations

import math

import html
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import col, or_, select

from .. import audit, chem
from ..models import AuditEvent, Entry, EntryKind, Experiment, Material
from ..security import DB, Admin, CurrentUser
from ..services import experiment_summary
from .experiments import audit_out

router = APIRouter(prefix="/api", tags=["search & audit"])

_TAG = re.compile(r"<[^>]+>")
_SKIP_KEYS = {"smiles", "molblock", "reaction_smiles", "attachment_id", "attachment_ids", "material_id", "lot_id",
              "analysis", "analyses", "snapshot", "integrals", "labels", "overlays", "basis", "role", "limiting"}


def _terms(q: str) -> list[str]:
    """Words to match; "quoted phrases" stay together. Everything is case-insensitive."""
    out = [m.group(1) or m.group(2) for m in re.finditer(r'"([^"]+)"|(\S+)', q)]
    return [t.lower() for t in out if t and t.strip()][:12]


def _strings(v: Any, key: str = "") -> list[str]:
    """Human-readable text inside section content: values only, never field names or structure codes."""
    if key in _SKIP_KEYS:
        return []
    if isinstance(v, str):
        return [html.unescape(_TAG.sub(" ", v))] if v.strip() else []
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [f"{v:g}"]
    if isinstance(v, dict):
        return [s for k, x in v.items() for s in _strings(x, k)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x, key)]
    return []


_NUM = re.compile(r"^\d+(?:[.,]\d+)?$")
_CODE = re.compile(r"[a-z][a-z0-9]{1,5}[-_ ]\d{1,6}", re.I)  # what a sample code looks like (see samples.py)


def _pattern(term: str) -> re.Pattern:
    """Numbers match whole numbers only (80 shouldn't hit 0.999808 or CAS 98-80-6); words match anywhere,
    so 'boron' finds 'boronic'."""
    if _NUM.match(term):
        return re.compile(r"(?<![\d.,-])" + re.escape(term) + r"(?![\d]|[.,]\d|-\d)")
    return re.compile(re.escape(term))


def _has(text: str, pats: list[re.Pattern]) -> bool:
    low = text.lower()
    return all(p.search(low) for p in pats)


def _count(text: str, pat: re.Pattern) -> int:
    return len(pat.findall(text.lower()))


def _num(v: Any, unit: str = "") -> str:
    try:
        return f"{float(v):g}{unit}" if v not in (None, "") else ""
    except (TypeError, ValueError):
        return ""


PURIF = {"column": "flash column", "prep_tlc": "prep TLC", "prep_hplc": "prep HPLC", "recrystallization": "recrystallisation",
         "trituration": "trituration", "distillation": "distillation", "extraction": "extraction", "none": "used crude"}


def _section_text(kind: EntryKind, c: dict[str, Any]) -> str:
    """What a chemist would search for in each kind of section, without computed numbers or field names."""
    c = c or {}
    if kind == EntryKind.reaction:
        cond = c.get("conditions") or {}
        parts = [" ".join(x for x in (r.get("name") or "", r.get("formula") or "", r.get("notes") or "") if x) for r in c.get("rows") or []]
        parts += [_num(cond.get("temperature_c"), " °C"), cond.get("temperature_label") or "", _num(cond.get("time_h"), " h"),
                  cond.get("atmosphere") or "", _num(cond.get("target_conc_m"), " M"), cond.get("notes") or ""]
        return " · ".join(p for p in parts if p)
    if kind == EntryKind.purification:
        return " · ".join(p for p in (PURIF.get(c.get("method"), c.get("method") or ""), c.get("stationary_phase") or "", c.get("eluent") or "",
                                      f"Rf {c['rf']}" if c.get("rf") else "", c.get("rf_eluent") or "", _num(c.get("isolated_mg"), " mg"),
                                      c.get("appearance") or "", html.unescape(_TAG.sub(" ", c.get("notes_html") or ""))) if p)
    if kind == EntryKind.characterization:
        return " · ".join(" ".join(x for x in (it.get("technique", ""), it.get("conditions", ""), it.get("data", "")) if x) for it in c.get("items") or [])
    if kind == EntryKind.safety:
        snap = c.get("snapshot") if isinstance(c.get("snapshot"), dict) else {}
        chems = [f"{ch.get('name', '')} {' '.join(ch.get('hazards') or [])}" for ch in snap.get("chemicals") or [] if isinstance(ch, dict)]
        return " · ".join(p for p in [*chems, c.get("controls") or "", c.get("waste") or "", c.get("emergency") or ""] if p)
    if kind == EntryKind.file:
        return str(c.get("caption") or "")
    if kind == EntryKind.references:
        from ..doi import plain
        return " · ".join(" ".join(x for x in (plain(it), it.get("note") or "") if x) for it in c.get("items") or [] if isinstance(it, dict) and (plain(it) or it.get("note")))
    if kind == EntryKind.custom:
        from ..plugins import text_of
        return text_of(c).replace("\n", " · ")
    return " ".join(_strings(c))


def _snippet(text: str, terms: list[str], width: int = 80) -> str:
    plain = re.sub(r"\s+", " ", text).strip()
    low = plain.lower()
    hits = [m.start() for t in terms for m in [_pattern(t).search(low)] if m]
    if not hits:
        return ""
    i = min(hits)
    s = max(0, i - width)
    e = min(len(plain), i + width * 2)
    return ("…" if s else "") + plain[s:e].strip() + ("…" if e < len(plain) else "")


def _mat_out(m: Material, **extra: Any) -> dict[str, Any]:
    return {"id": m.id, "mid": m.mid, "name": m.name, "smiles": m.smiles, "formula": m.formula, "mw": m.mw, "cas": m.cas,
            "kind": m.kind, **extra}


@router.get("/search")
def search(user: CurrentUser, session: DB, q: str = "", mode: str = "text", threshold: float = 0.5,
           limit: int = 100, status: str = "", notebook_id: int | None = None, mine: bool = False):
    """mode = text | substructure | similarity. Text search matches every word (or "quoted phrase")
    anywhere in an experiment: title, tags, ID, notebook, author, section text, table values, file names."""
    q = q.strip()[:500]
    limit = max(1, min(limit, 200))
    empty = {"experiments": [], "materials": [], "files": [], "terms": []}
    if not q:
        return empty

    def allowed(exp: Experiment) -> bool:
        return ((not status or exp.status.value == status) and (notebook_id is None or exp.notebook_id == notebook_id)
                and (not mine or exp.owner_id == user.id))

    from ..access import visible_experiment_filter
    exps = {e.id: e for e in session.exec(visible_experiment_filter(select(Experiment), session, user)) if allowed(e)}

    if mode == "text":
        from .. import samples
        from ..models import Attachment, Lot, Notebook, SampleCode, User
        def as_code(t: str) -> str:
            """A sample code typed as jl-13, JL_013 or "JL 013" is searched for as it's written: jl-013."""
            if _CODE.fullmatch(t.strip()):
                found = samples.find(session, t)
                if len(found) == 1:
                    return found[0].text.lower()
            return t.lower()
        terms = [as_code(q)] if _CODE.fullmatch(q) and " " in q else [as_code(t) for t in _terms(q)]
        if not terms:
            return empty
        pats = [_pattern(t) for t in terms]
        codes: dict[int, list[str]] = {}
        for sc in session.exec(select(SampleCode).where(SampleCode.retired_at == None)):  # noqa: E711
            if sc.experiment_id in exps:
                codes.setdefault(sc.experiment_id, []).append(samples.fmt(sc.prefix, sc.number))
        nbs = {n.id: n.name for n in session.exec(select(Notebook))}
        users = {u.id: (u.full_name or u.username) for u in session.exec(select(User))}
        sections: dict[int, list[tuple[str, str]]] = {}
        for en in session.exec(select(Entry).where(Entry.deleted == False)):  # noqa: E712
            if en.experiment_id in exps:
                try:
                    text = str(_section_text(en.kind, en.content) or "")
                except Exception:  # an odd section mustn't break search for the whole lab
                    text = " ".join(_strings(en.content))
                sections.setdefault(en.experiment_id, []).append((en.title, text))
        files: dict[int, list[Attachment]] = {}
        for a in session.exec(select(Attachment).where(Attachment.withdrawn_at == None)):  # noqa: E711
            files.setdefault(a.experiment_id, []).append(a)

        hits = []
        for eid, exp in exps.items():
            fields = [  # (label, text, weight)
                ("ID", exp.eid, 10), ("Title", exp.title, 6), ("Tags", " ".join(exp.tags or []), 4),
                ("Description", exp.description or "", 3), ("Notebook", nbs.get(exp.notebook_id, ""), 2),
                ("Author", users.get(exp.owner_id, ""), 2), ("Sample code", " ".join(codes.get(eid, [])), 10),
            ] + [(t, txt, 1) for t, txt in sections.get(eid, [])] + [("File", a.filename, 2) for a in files.get(eid, [])]
            if not _has(" \n ".join(f[1] for f in fields), pats):
                continue
            score = sum(f[2] * _count(f[1], p) for f in fields for p in pats)
            if any(c.lower() in terms for c in codes.get(eid, [])):
                score += 1000  # the experiment the code was handed to comes before records that only mention it
            # where it matched, for the result card: prefer a section body over metadata
            where = next(((lab, txt) for lab, txt, w in fields if w == 1 and any(p.search(txt.lower()) for p in pats)), None) \
                or next(((lab, txt) for lab, txt, w in fields if any(p.search(txt.lower()) for p in pats)), ("", ""))
            hits.append((score, exp, where))
        hits.sort(key=lambda h: h[1].updated_at, reverse=True)  # ties: most recently edited first
        hits.sort(key=lambda h: -h[0])
        # only build result cards for what's returned (a common word can match thousands of records)
        hits = [{**experiment_summary(session, exp), "score": score, "match": where[0],
                 "snippet": _snippet(where[1], terms) if where[0] not in ("ID", "Title") else ""}
                for score, exp, where in hits[:limit]]

        mats = []
        lots = {}
        for lot in session.exec(select(Lot)):
            lots.setdefault(lot.material_id, []).append(lot.barcode)
        for m in session.exec(select(Material)):
            blob = " \n ".join([m.name, m.mid, m.cas, m.synonyms, m.formula, m.kind, " ".join(lots.get(m.id, []))])
            if _has(blob, pats):
                mats.append(_mat_out(m, score=sum(5 * _count(m.name, p) + _count(blob, p) for p in pats)))
        mats.sort(key=lambda m: -m["score"])

        fl = []
        for eid, atts in files.items():
            if eid not in exps:
                continue
            for a in atts:
                if _has(a.filename, pats):
                    fl.append({"id": a.id, "filename": a.filename, "size": a.size, "experiment_id": eid,
                               "eid": exps[eid].eid, "title": exps[eid].title, "created_at": a.created_at.isoformat() + "Z"})
        return {"experiments": hits[:limit], "materials": mats[:limit], "files": fl[:limit], "terms": terms}

    try:
        if mode == "substructure":
            fn = chem.substructure_matcher(q)
            scorer = lambda s: 1.0 if fn(s) else 0.0  # noqa: E731
            cutoff = 0.5
        elif mode == "similarity":
            scorer = chem.similarity_scorer(q)
            cutoff = max(0.0, min(threshold, 1.0)) if math.isfinite(threshold) else 0.5
        else:
            raise HTTPException(400, "mode must be text, substructure or similarity")
    except chem.ChemError as exc:
        raise HTTPException(422, str(exc))

    mats = []
    for m in session.exec(select(Material).where(Material.smiles != "")):
        s = scorer(m.smiles)
        if s >= cutoff:
            mats.append(_mat_out(m, score=round(s, 3)))
    out: dict[int, dict[str, Any]] = {}
    for en in session.exec(select(Entry).where(Entry.kind == EntryKind.reaction, Entry.deleted == False)):  # noqa: E712
        if en.experiment_id not in exps:
            continue
        best, best_row = 0.0, None
        rows = (en.content or {}).get("rows")
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("smiles"), str):
                continue
            sc = scorer(r["smiles"])
            if sc > best:
                best, best_row = sc, r
        if best_row is not None and best >= cutoff and (en.experiment_id not in out or out[en.experiment_id]["score"] < best):
            exp = exps[en.experiment_id]
            out[exp.id] = {**experiment_summary(session, exp), "score": round(best, 3),
                           "match": f"{best_row.get('role')}: {best_row.get('name') or best_row.get('formula') or ''}".strip(),
                           "smiles": best_row.get("smiles")}
    key = lambda d: -d["score"]  # noqa: E731
    return {"experiments": sorted(out.values(), key=key)[:limit], "materials": sorted(mats, key=key)[:limit], "files": [], "terms": []}


@router.get("/audit")
def audit_log(_: Admin, session: DB, limit: int = 200, before_id: int | None = None,
              object_type: str | None = None, username: str | None = None):
    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    if object_type:
        q = q.where(AuditEvent.object_type == object_type)
    if username:
        q = q.where(AuditEvent.username == username)
    return [audit_out(ev) for ev in session.exec(q)]


@router.get("/audit/verify")
def audit_verify(_: CurrentUser, session: DB):
    return audit.verify_chain(session)
