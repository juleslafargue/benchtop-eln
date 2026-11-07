"""Supporting Information documents (0.33): what goes in each compound's part, and what a journal will ask for that
isn't in the record yet.

The Word file itself is put together in the browser, which is where spectra are processed and drawn. This module
gathers, for each compound, the experiment's write-up (writeup.draft), its product, and its spectra; it only reads
experiments, and only those the person may open.
"""
from __future__ import annotations

import re
from typing import Any

from sqlmodel import Session, select

from . import writeup
from .access import access_for
from .models import Attachment, Entry, EntryKind, Experiment, ExperimentStatus, SiDocument, User
from .services import live_entries

PROTON = re.compile(r"^\s*(1H|¹H)\b|^\s*H\s*NMR\b|proton", re.I)
CARBON = re.compile(r"^\s*(13C|¹³C)\b|^\s*C\s*NMR\b|carbon", re.I)
HRMS = re.compile(r"\bHRMS\b|\bhigh[- ]resolution\b|\belemental\b|\bmicroanalysis\b|^\s*(EA|CHN)\b|\banal\.?\s*calcd\b", re.I)
MP = re.compile(r"^\s*(mp|m\.p\.|melting)", re.I)
SOLID = re.compile(r"solid|crystal|powder", re.I)
NMR = re.compile(r"NMR", re.I)


def _technique(it: dict[str, Any]) -> str:
    return str(it.get("technique") or "")


def _has_data(it: dict[str, Any]) -> bool:
    return bool(str(it.get("data") or "").strip())


def checks(entries: list[Entry], draft: dict[str, Any], status: ExperimentStatus) -> list[str]:
    """What a typical journal SI asks for that this compound's record doesn't have, said plainly. Never blocks."""
    items = [it for e in entries if e.kind == EntryKind.characterization for it in (e.content or {}).get("items", [])
             if isinstance(it, dict)]
    pur = next((e.content or {} for e in entries if e.kind == EntryKind.purification), {}) or {}
    out: list[str] = []
    if status == ExperimentStatus.voided:
        out.append("the experiment is voided")
    elif status == ExperimentStatus.open:
        out.append("not signed yet")
    missing = [re.sub(r"^(a|an) ", "", m) for m in draft.get("missing", []) if m != "characterisation data"]
    out += [f"no {m}" for m in missing]
    h = [it for it in items if PROTON.search(_technique(it))]
    c = [it for it in items if CARBON.search(_technique(it))]
    if not any(_has_data(it) for it in h):
        out.append("no ¹H NMR data")
    if not any(_has_data(it) for it in c):
        out.append("no ¹³C NMR data")
    if not any(_has_data(it) and HRMS.search(_technique(it)) for it in items):
        out.append("no HRMS (or elemental analysis)")
    look = str(pur.get("appearance") or "")
    if SOLID.search(look) and not any(_has_data(it) and MP.search(_technique(it)) for it in items):
        look = look.strip().lower()
        out.append(f"no melting point (it's {look if re.search(r'[a-z]s$', look) else writeup._article(look)})")
    if not any(it.get("attachment_id") for it in h):
        out.append("no ¹H NMR spectrum file")
    if not any(it.get("attachment_id") for it in c):
        out.append("no ¹³C NMR spectrum file")
    if any(it.get("attachment_id") and not (it.get("analysis") or {}).get("integrals") for it in h):
        out.append("¹H integrals not saved (the figure uses automatic ones)")
    return out


def _spectra(session: Session, exp: Experiment, entries: list[Entry], chosen: list[int] | None) -> list[dict[str, Any]]:
    """Every characterisation item with a file, in the record's order; which ones go in the document as figures."""
    atts = {a.id: a for a in session.exec(select(Attachment).where(Attachment.experiment_id == exp.id,
                                                                   Attachment.withdrawn_at == None))}  # noqa: E711
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
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
            tech = _technique(it)
            out.append({
                "attachment_id": aid, "filename": atts[aid].filename, "size": atts[aid].size,
                "technique": tech, "conditions": str(it.get("conditions") or ""),
                "analysis": it.get("analysis") if isinstance(it.get("analysis"), dict) else None,
                "included": (aid in chosen) if chosen is not None else bool(NMR.search(tech)),
            })
    return out


def compound(session: Session, user: User, item: dict[str, Any], index: int) -> dict[str, Any]:
    """One compound of an SI document, read from its experiment, or a note that it's no longer visible."""
    exp_id = item.get("experiment_id")
    base = {"index": index, "experiment_id": exp_id, "number": item.get("number", ""), "name_set": item.get("name", "")}
    exp = session.get(Experiment, exp_id) if isinstance(exp_id, int) else None
    if exp is None or not access_for(session, user).can_read(exp):
        # nothing about it is read: someone else's record stays theirs, even if its number was saved here
        eid = f"EXP-{exp_id:05d}" if isinstance(exp_id, int) else "?"
        return {**base, "visible": False, "eid": eid, "name": item.get("name") or eid,
                "problems": [f"{eid} is no longer one you can open, so it's left out"]}
    entries = live_entries(session, exp.id)
    draft = writeup.draft(entries)
    product = draft.get("product") or {}
    name = (item.get("name") or "").strip() or product.get("name") or exp.title
    return {
        **base, "visible": True, "eid": exp.eid, "title": exp.title, "status": exp.status.value,
        "name": name, "name_default": product.get("name") or "",
        "product": product or None,
        "heading": draft["heading"], "experimental_html": draft["experimental_html"],
        "characterisation_html": draft["characterisation_html"], "missing": draft["missing"],
        "spectra": _spectra(session, exp, entries, item.get("spectra")),
        "problems": checks(entries, draft, exp.status),
    }


def document_out(session: Session, user: User, doc: SiDocument, with_compounds: bool = True) -> dict[str, Any]:
    out = {
        "id": doc.id, "title": doc.title, "general": doc.general, "page_size": doc.page_size, "version": doc.version,
        "items": doc.items or [], "created_at": doc.created_at.isoformat() + "Z", "updated_at": doc.updated_at.isoformat() + "Z",
    }
    if with_compounds:
        out["compounds"] = [compound(session, user, it, i) for i, it in enumerate(doc.items or [])]
    return out
