from __future__ import annotations

import copy
import json
import math
import re
import threading
import hashlib
import html
from typing import Annotated, Any

import nh3

# Rich text allowed in procedure/notes sections: nh3's safe defaults, plus what the editor needs for
# checklists, alignment, text colour and links. Styles are limited to colour and alignment.
_ATTRS = {k: set(v) for k, v in nh3.ALLOWED_ATTRIBUTES.items()}
for _t in ("p", "h1", "h2", "h3", "h4", "span", "mark"):
    _ATTRS.setdefault(_t, set()).add("style")
_ATTRS.setdefault("ul", set()).add("data-type")
_ATTRS.setdefault("li", set()).update({"data-type", "data-checked"})
_ATTRS.setdefault("a", set()).update({"href", "target"})
_ATTRS.setdefault("th", set()).update({"colspan", "rowspan"})
_ATTRS.setdefault("img", set()).update({"data-attachment-id", "alt", "width", "data-align", "data-width"})
_ATTRS.setdefault("td", set()).update({"colspan", "rowspan"})
_ATTRS.setdefault("mark", set()).add("data-hl")  # highlighter colour by name: green, blue, pink, orange, purple (none = yellow)
HIGHLIGHT_COLOURS = {"yellow": "#fff3a3", "green": "#c8f2d6", "blue": "#cfe6ff", "pink": "#ffd3e6", "orange": "#ffe0bf", "purple": "#e6d9ff"}
_FONT_SIZE = re.compile(r"font-size\s*:\s*([^;\"']*);?", re.I)


def _font_size(m: re.Match) -> str:
    """Keep font sizes between 6 and 72 pt (in pt or px); drop anything else."""
    v = re.fullmatch(r"\s*([\d.]+)\s*(pt|px)\s*", m.group(1), re.I)
    if not v:
        return ""
    pt = float(v.group(1)) * (0.75 if v.group(2).lower() == "px" else 1)
    return f"font-size: {round(pt)}pt;" if 6 <= pt <= 72 else ""


def _marks_inline(h: str) -> str:
    """Reports and PDFs: give highlights a solid, print-friendly colour inline."""
    def sub(m):
        hl = re.search(r'data-hl="(\w+)"', m.group(0))
        return f'<mark style="background-color:{HIGHLIGHT_COLOURS.get(hl.group(1) if hl else "yellow", HIGHLIGHT_COLOURS["yellow"])}">'
    return re.sub(r"<mark\b[^>]*>", sub, h)


_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*?(/?)>")
_VOID = {"br", "img", "hr", "wbr", "col", "input"}
MAX_NESTING = 60


def nesting_depth(html: str) -> int:
    """How deeply tags are nested; the editor stays well under 20, the PDF renderer gives up in the hundreds."""
    depth = deepest = 0
    for close, tag, selfclose in _TAG_RE.findall(html or ""):
        if tag.lower() in _VOID or selfclose:
            continue
        depth = max(0, depth - 1) if close else depth + 1
        deepest = max(deepest, depth)
    return deepest


# elements a browser closes by itself when the next one starts: they never pile up, so they aren't counted
_SELF_CLOSING = {"p", "li", "dt", "dd", "tr", "td", "th", "thead", "tbody", "tfoot", "option", "optgroup", "rb", "rt", "rp", "colgroup"}
MAX_OPEN = 1000  # far deeper than any real document; the HTML cleaner slows down sharply past it
# a tag in raw HTML; it ends at the next "<" too, so text full of unfinished tags is still read in one pass
_RAW_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^<>]*?(/?)>")


def open_depth(html: str) -> int:
    """How deeply tags are left open in raw HTML, before any cleaning. A closing tag only counts when something of
    that name is open (a stray one, like a browser, changes nothing). Quick on any input."""
    open_: dict[str, int] = {}
    depth = deepest = 0
    for close, tag, selfclose in _RAW_TAG_RE.findall(html or ""):
        t = tag.lower()
        if t in _VOID or t in _SELF_CLOSING or selfclose:
            continue
        if close:
            if open_.get(t):
                open_[t] -= 1
                depth -= 1
        else:
            open_[t] = open_.get(t, 0) + 1
            depth += 1
            deepest = max(deepest, depth)
    return deepest


def clean_html(html: str) -> str:
    if open_depth(html) > MAX_OPEN:  # thousands of levels: no document is like that, and cleaning it would take minutes
        html = _RAW_TAG_RE.sub(" ", html)
    out = nh3.clean(html or "", tags=nh3.ALLOWED_TAGS | {"label"}, attributes=_ATTRS,
                    filter_style_properties={"color", "text-align", "background-color", "font-size"}, link_rel="noopener noreferrer")
    return _FONT_SIZE.sub(_font_size, out) if "font-size" in out else out
from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import audit, chem, doi, mailer, nmrpredict, plugins, publishing, storage, timestamps, witness
from ..config import settings
from ..models import (
    Attachment, Entry, EntryKind, EntryVersion, Experiment, ExperimentStatus, Notebook, Role,
    Signature, User, utcnow,
)
from ..security import DB, CurrentUser, Writer, verify_password
from ..patching import cleaned
from ..services import (
    content_hash, entry_out, experiment_summary, get_or_404, live_entries, read_experiment, require_editable,
    signatures_out, snapshot, user_brief,
)

import logging
log = logging.getLogger("benchtop.experiments")
router = APIRouter(prefix="/api", tags=["notebooks & experiments"])

# ====================================================================== notebooks


class NotebookIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=5000)
    group_id: int | None = None
    visibility: str | None = None


def _check_audience(session, user: User, group_id: int | None, visibility: str | None) -> tuple[int | None, str]:
    """Validate a notebook's group/visibility: you can only give a notebook to a group you belong to."""
    from ..access import VISIBILITIES, access_for
    from ..models import LabGroup
    vis = visibility or ("group" if group_id else "everyone")
    if vis not in VISIBILITIES:
        raise HTTPException(422, f"visibility must be one of {', '.join(VISIBILITIES)}")
    if group_id is not None:
        get_or_404(session, LabGroup, group_id)
        if user.role != Role.admin and group_id not in access_for(session, user).groups:
            raise HTTPException(403, "You can only put a notebook in a group you belong to")
    elif vis == "group":
        raise HTTPException(422, "Choose a group for a group notebook")
    return group_id, vis


def _holds_experiments(session, nb_id: int) -> bool:
    """Whether any experiment at all is in this notebook now (drafts, signed, witnessed, voided, locked, templates),
    whoever can see it. Experiments moved out to another notebook don't count."""
    return session.exec(select(Experiment.id).where(Experiment.notebook_id == nb_id).limit(1)).first() is not None


def notebook_out(session, nb: Notebook, exps: list[Experiment] | None = None, empty: bool | None = None) -> dict[str, Any]:
    from ..models import LabGroup
    g = session.get(LabGroup, nb.group_id) if nb.group_id else None
    out = {"id": nb.id, "name": nb.name, "description": nb.description, "archived": nb.archived,
           "owner": user_brief(session, nb.owner_id), "created_at": nb.created_at.isoformat() + "Z",
           "visibility": nb.visibility or "everyone", "group": {"id": g.id, "name": g.name} if g else None,
           # only an empty notebook can be deleted (DELETE /api/notebooks/{id}); others can be archived
           "empty": (not _holds_experiments(session, nb.id)) if empty is None else empty}
    if exps is not None:
        out["experiment_count"] = len(exps)
        out["status_counts"] = {s.value: sum(1 for e in exps if e.status == s) for s in ExperimentStatus}
    return out


@router.get("/notebooks")
def list_notebooks(user: CurrentUser, session: DB, include_archived: bool = False):
    from ..access import access_for
    acc = access_for(session, user)
    q = acc.filter_notebooks(select(Notebook).order_by(Notebook.name))
    if not include_archived:
        q = q.where(Notebook.archived == False)  # noqa: E712
    from sqlalchemy import func
    held = {nid for nid, _ in session.exec(select(Experiment.notebook_id, func.count()).group_by(Experiment.notebook_id))}
    out = []
    for nb in session.exec(q):
        exps = session.exec(acc.filter(select(Experiment).where(Experiment.notebook_id == nb.id))).all()
        out.append(notebook_out(session, nb, exps, empty=nb.id not in held))
    return out


@router.post("/notebooks", status_code=201)
def create_notebook(body: NotebookIn, user: Writer, session: DB):
    gid, vis = _check_audience(session, user, body.group_id, body.visibility)
    nb = Notebook(name=body.name, description=body.description, owner_id=user.id, group_id=gid, visibility=vis)
    session.add(nb)
    session.flush()
    audit.record(session, user, "create", "notebook", nb.id, name=nb.name, group_id=gid, visibility=vis)
    session.commit()
    return notebook_out(session, nb, [], empty=True)


class NotebookPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    archived: bool | None = None
    group_id: int | None = None
    visibility: str | None = None


@router.patch("/notebooks/{nb_id}")
def update_notebook(nb_id: int, body: NotebookPatch, user: Writer, session: DB):
    nb = get_or_404(session, Notebook, nb_id)
    if nb.owner_id != user.id and user.role != Role.admin:
        raise HTTPException(403, "Only the notebook owner can change it")
    changes = cleaned(nb, body.model_dump(exclude_unset=True))
    if "group_id" in changes or "visibility" in changes:
        gid = changes["group_id"] if "group_id" in changes else nb.group_id
        vis = changes.get("visibility") or (nb.visibility if gid else None)
        if vis == "group" and not gid:
            vis = None
        changes["group_id"], changes["visibility"] = _check_audience(session, user, gid, vis)
    before = {k: getattr(nb, k) for k in changes}
    for k, v in changes.items():
        setattr(nb, k, v)
    session.add(nb)
    audit.record(session, user, "update", "notebook", nb.id, name=nb.name, before=before, changes=changes)
    session.commit()
    return notebook_out(session, nb)


@router.delete("/notebooks/{nb_id}")
def delete_notebook(nb_id: int, user: Writer, session: DB):
    """Delete a notebook that holds no experiments (a test notebook, one made by mistake). Records are never deleted,
    so a notebook with any experiment in it, of any status, can only be archived. The audit trail keeps its history."""
    from ..access import access_for
    nb = get_or_404(session, Notebook, nb_id)
    if not access_for(session, user).can_read_notebook(nb):
        raise HTTPException(404, f"Notebook {nb_id} not found")  # don't confirm that a hidden notebook exists
    if nb.owner_id != user.id and user.role != Role.admin:
        raise HTTPException(403, "Only the notebook's owner or an admin can delete it")
    # hold the write lock before looking, so no experiment can be added between the check and the delete
    audit._take_write_lock(session)
    if _holds_experiments(session, nb.id):
        session.rollback()
        raise HTTPException(409, "This notebook has experiments, so it can't be deleted. Archive it instead.")
    # nothing else may point at it: people's default notebook for new experiments is cleared
    cleared = []
    for u in session.exec(select(User)):
        prefs = u.preferences or {}
        if prefs.get("default_notebook_id") == nb.id:
            u.preferences = {k: v for k, v in prefs.items() if k != "default_notebook_id"}
            session.add(u)
            cleared.append(u.username)
    audit.record(session, user, "delete", "notebook", nb.id, name=nb.name, group_id=nb.group_id,
                 visibility=nb.visibility or "everyone", archived=nb.archived,
                 **({"default_cleared_for": cleared} if cleared else {}))
    session.delete(nb)
    session.commit()
    return {"ok": True, "id": nb_id}


# ====================================================================== experiments


Tags = Annotated[list[Annotated[str, Field(max_length=60)]], Field(max_length=50)]


class ExperimentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notebook_id: int | None = None
    description: str = Field("", max_length=20_000)
    template_id: int | None = None
    is_template: bool = False
    tags: Tags = []


DEFAULT_CONTENT: dict[EntryKind, dict] = {
    EntryKind.text: {"html": ""},
    EntryKind.reaction: {"reaction_smiles": "", "rows": [], "totals": {}, "conditions": {}},
    EntryKind.table: {"columns": [{"name": "Sample"}, {"name": "Value"}], "rows": [["", ""]]},
    EntryKind.file: {"attachment_ids": [], "caption": ""},
    EntryKind.purification: {"method": "column", "stationary_phase": "", "eluent": "", "rf": "",
                             "rf_eluent": "", "isolated_mg": None, "appearance": "", "notes_html": ""},
    EntryKind.characterization: {"items": [{"technique": "1H NMR", "conditions": "", "data": "", "attachment_id": None}]},
    EntryKind.safety: {"controls": "", "waste": "", "emergency": "", "notes_html": "", "snapshot": None},
    EntryKind.screen: {"x_name": "Temperature (°C)", "y_name": "Catalyst loading (mol%)", "x_levels": ["25", "50", "80"],
                       "y_levels": ["1", "2.5", "5"], "response": "Yield (%)", "higher_is_better": True, "cells": {}},
    EntryKind.references: {"items": []},
}

DEFAULT_SECTIONS = [
    (EntryKind.text, "Aim", DEFAULT_CONTENT[EntryKind.text]),
    (EntryKind.reaction, "Reaction", DEFAULT_CONTENT[EntryKind.reaction]),
    (EntryKind.safety, "Safety & risk assessment", DEFAULT_CONTENT[EntryKind.safety]),
    (EntryKind.text, "Procedure", DEFAULT_CONTENT[EntryKind.text]),
    (EntryKind.purification, "Work-up & purification", DEFAULT_CONTENT[EntryKind.purification]),
    (EntryKind.characterization, "Characterisation", DEFAULT_CONTENT[EntryKind.characterization]),
    (EntryKind.text, "Observations & results", DEFAULT_CONTENT[EntryKind.text]),
]

METRIC_LABELS = (("atom_economy_pct", "atom economy", "%"), ("rme_pct", "RME", "%"), ("e_factor", "E-factor", ""),
                 ("pmi", "PMI", ""), ("solvent_l_per_kg", "solvent", " L/kg"))

CONDITION_FIELDS = ("temperature_c", "temperature_label", "time_h", "atmosphere", "target_conc_m", "notes")


def _clean_analysis(a) -> dict | None:
    """Saved spectrum integrations: [[ppm_a, ppm_b], ...] plus reference and peak threshold."""
    if not isinstance(a, dict):
        return None
    ints = []
    for pair in (a.get("integrals") or [])[:200]:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(_num(x) is not None for x in pair):
            ints.append([_num(pair[0]), _num(pair[1])])
    labels = []
    for lab in (a.get("labels") or [])[:len(ints)]:
        lab = lab if isinstance(lab, dict) else {}
        labels.append({"mult": str(lab.get("mult") or "")[:8], "j": str(lab.get("j") or "")[:40]})
    offset = _num(a.get("offset")) or 0.0
    out = {"integrals": ints, "ref_idx": int(_num(a.get("ref_idx")) or 0),
           "ref_value": _num(a.get("ref_value")) or 1.0, "threshold": _num(a.get("threshold")) or 0.05}
    if labels and any(lab["mult"] or lab["j"] for lab in labels):
        out["labels"] = labels + [{"mult": "", "j": ""}] * (len(ints) - len(labels))
    if offset and abs(offset) <= 20:
        out["offset"] = offset
    imps = []
    for imp in (a.get("impurities") or [])[:20]:
        if not isinstance(imp, dict):
            continue
        imps.append({"short": str(imp.get("short", ""))[:20], "name": str(imp.get("name", ""))[:60],
                     "molPct": _num(imp.get("molPct")), "wtPct": _num(imp.get("wtPct")),
                     "signals": [v for v in (_num(x) for x in (imp.get("signals") or [])[:6]) if v is not None]})
    if imps:
        out["impurities"] = imps
    fid = a.get("fid")
    if isinstance(fid, dict):  # phase/line-broadening chosen for a raw FID processed in the browser
        ph0, ph1, lb = _num(fid.get("ph0")), _num(fid.get("ph1")), _num(fid.get("lb"))
        if ph0 is not None and ph1 is not None:
            out["fid"] = {"ph0": round(ph0 % 360, 2), "ph1": round(max(-1440.0, min(1440.0, ph1)), 2),
                          "lb": round(max(0.0, min(50.0, lb)), 2) if lb is not None else None}
    asg = a.get("assignments")
    if isinstance(asg, dict) and asg.get("nucleus") in ("1H", "13C"):
        lo, hi = (-3.0, 17.0) if asg["nucleus"] == "1H" else (-20.0, 240.0)
        items = []
        for it in (asg.get("items") or [])[:200]:
            if not isinstance(it, dict) or _num(it.get("shift")) is None or not lo <= _num(it.get("shift")) <= hi:
                continue
            atoms = [int(i) for i in (it.get("atoms") or [])[:40] if isinstance(i, (int, float)) and 0 <= i < 500]
            if atoms:
                items.append({"atoms": atoms, "shift": round(_num(it.get("shift")), 3)})
        if items:
            out["assignments"] = {"nucleus": asg["nucleus"], "smiles": str(asg.get("smiles") or "")[:500], "items": items,
                                  "solvent": str(asg.get("solvent") or "")[:20], "confirmed_by": str(asg.get("confirmed_by") or "")[:80],
                                  "confirmed_at": str(asg.get("confirmed_at") or "")[:30]}
    overlays = [int(v) for v in (a.get("overlays") or [])[:4] if isinstance(v, (int, float))]
    if overlays:
        out["overlays"] = overlays
        out["overlay_mode"] = "overlay" if a.get("overlay_mode") == "overlay" else "stack"
    return out


def _num(v):
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


@router.get("/experiments")
def list_experiments(
    user: CurrentUser, session: DB,
    notebook_id: int | None = None, status_: ExperimentStatus | None = Query(None, alias="status"),
    owner_id: int | None = None, templates: bool = False,
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
):
    from ..access import visible_experiment_filter
    q = visible_experiment_filter(select(Experiment).where(Experiment.is_template == templates), session, user)
    if notebook_id is not None:
        q = q.where(Experiment.notebook_id == notebook_id)
    if status_ is not None:
        q = q.where(Experiment.status == status_)
    if owner_id is not None:
        q = q.where(Experiment.owner_id == owner_id)
    q = q.order_by(Experiment.updated_at.desc(), Experiment.id.desc()).offset(offset).limit(limit)
    exps = session.exec(q).all()
    # each one's sample codes (JL-013), so the ⌘K box finds an experiment by the code on its vial
    from .. import samples
    from ..models import SampleCode
    codes: dict[int, list[str]] = {}
    if exps:
        for sc in session.exec(select(SampleCode).where(SampleCode.experiment_id.in_([e.id for e in exps]),
                                                        SampleCode.retired_at == None).order_by(SampleCode.id)):  # noqa: E711
            codes.setdefault(sc.experiment_id, []).append(samples.fmt(sc.prefix, sc.number))
    return [{**experiment_summary(session, e), "sample_codes": codes.get(e.id, [])} for e in exps]


def _new_entry(session, exp_id: int, kind: EntryKind, title: str, content: dict, pos: int, user: User):
    e = Entry(experiment_id=exp_id, kind=kind, title=title, content=content, position=pos,
              updated_by=user.id)
    session.add(e)
    session.flush()
    session.add(EntryVersion(entry_id=e.id, version=1, title=title, content=content, created_by=user.id))
    return e


def _require_notebook(session, user: User, nb_id: int) -> Notebook:
    """A notebook this person can put a new experiment in: one they can open, and not archived."""
    from ..access import access_for
    nb = get_or_404(session, Notebook, nb_id)
    if not access_for(session, user).can_read_notebook(nb):
        raise HTTPException(404, f"Notebook {nb_id} not found")
    if nb.archived:
        raise HTTPException(409, archived_message(nb))
    return nb


def archived_message(nb: Notebook) -> str:
    return f"“{nb.name}” is archived, so no new experiments can go into it. Choose another notebook, or unarchive it first."


@router.post("/experiments", status_code=201)
def create_experiment(body: ExperimentIn, user: Writer, session: DB):
    if body.notebook_id is not None:
        _require_notebook(session, user, body.notebook_id)
    elif not body.is_template:
        raise HTTPException(400, "notebook_id is required")
    # checked before anything is written; 0 is an ID too (one that never exists), not "no template"
    tpl = read_experiment(session, user, body.template_id) if body.template_id is not None else None
    exp = Experiment(title=body.title, notebook_id=body.notebook_id, description=body.description,
                     owner_id=user.id, is_template=body.is_template, template_id=body.template_id,
                     tags=body.tags)
    session.add(exp)
    session.flush()
    if tpl is not None:
        sections = [(e.kind, e.title, copy.deepcopy(e.content)) for e in live_entries(session, tpl.id)]
    else:
        sections = DEFAULT_SECTIONS
    for i, (kind, title, content) in enumerate(sections):
        _new_entry(session, exp.id, kind, title, copy.deepcopy(content), i, user)
    audit.record(session, user, "create", "experiment", exp.id, eid=exp.eid, title=exp.title,
                 template_id=body.template_id)
    session.commit()
    return experiment_summary(session, exp)


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    atts = session.exec(select(Attachment).where(Attachment.experiment_id == exp.id, Attachment.withdrawn_at == None))  # noqa: E711
    return {
        **experiment_summary(session, exp),
        "entries": [entry_out(e) for e in live_entries(session, exp.id)],
        "attachments": [attachment_out(a) for a in atts],
        "signatures": signatures_out(session, exp.id),
        "witness_request": witness.summary(session, exp),
        "origin": exp.origin,  # imported from another ELN (0.20): shown as a banner
        "publication": publishing.summary(session, exp),  # its DOI, once published on Zenodo (0.28)
        **_access_info(session, user, exp),
        **_links_and_samples(session, user, exp),
    }


def _links_and_samples(session, user: User, exp: Experiment) -> dict[str, Any]:
    from .. import samples
    from ..models import SampleCode
    from .instruments import follow_info
    codes = session.exec(select(SampleCode).where(SampleCode.experiment_id == exp.id, SampleCode.retired_at == None)  # noqa: E711
                         .order_by(SampleCode.prefix, SampleCode.number))
    return {**follow_info(session, user, exp), "samples": [samples.out(session, c) for c in codes]}


def _access_info(session, user: User, exp: Experiment) -> dict[str, Any]:
    from ..access import access_for
    from ..models import Comment, LabGroup
    acc = access_for(session, user)
    nb = session.get(Notebook, exp.notebook_id) if exp.notebook_id else None
    g = session.get(LabGroup, nb.group_id) if nb and nb.group_id else None
    open_threads = session.exec(select(Comment.id).where(Comment.experiment_id == exp.id, Comment.parent_id == None,  # noqa: E711
                                                         Comment.deleted == False, Comment.resolved_at == None)).all()  # noqa: E712
    return {
        "can_edit": acc.can_edit(exp),
        "shared_with_me": acc.shares.get(exp.id),
        "audience": {"visibility": (nb.visibility or "everyone") if nb else ("everyone" if exp.is_template else "private"),
                     "group": {"id": g.id, "name": g.name} if g else None},
        "open_comments": len(open_threads),
        # its notebook is archived: it stays readable and editable (a draft can still be signed), but copies of it
        # can't go there
        "notebook_archived": bool(nb and nb.archived),
    }


class ExperimentPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = Field(None, max_length=20_000)
    tags: Tags | None = None
    notebook_id: int | None = None


@router.patch("/experiments/{exp_id}")
def update_experiment(exp_id: int, body: ExperimentPatch, user: Writer, session: DB):
    exp = read_experiment(session, user, exp_id)
    require_editable(exp, user)
    changes = cleaned(exp, body.model_dump(exclude_unset=True))
    if changes.get("notebook_id") not in (None, exp.notebook_id):
        if exp.owner_id != user.id and user.role != Role.admin:
            raise HTTPException(403, "Only the owner can move an experiment to another notebook")
        _require_notebook(session, user, changes["notebook_id"])
    elif "notebook_id" in changes:
        changes.pop("notebook_id")
    before = {k: getattr(exp, k) for k in changes}
    for k, v in changes.items():
        setattr(exp, k, v)
    exp.updated_at = utcnow()
    session.add(exp)
    audit.record(session, user, "update", "experiment", exp.id, before=before, after=changes)
    session.commit()
    return experiment_summary(session, exp)


@router.post("/experiments/{exp_id}/clone", status_code=201)
def clone_experiment(exp_id: int, user: Writer, session: DB, as_template: bool = False, scale: float | None = None):
    src = read_experiment(session, user, exp_id)
    if scale is not None and not (0.001 <= scale <= 1000):
        raise HTTPException(400, "Scale factor must be between 0.001 and 1000")
    from ..access import access_for
    nb = session.get(Notebook, src.notebook_id) if src.notebook_id else None
    if not as_template and src.notebook_id is not None and (nb is None or not access_for(session, user).can_read_notebook(nb)):
        raise HTTPException(403, "You can copy this experiment as a template, but not into a notebook you can't open")
    if not as_template and nb is not None and nb.archived:  # the copy would go into the archived notebook
        raise HTTPException(409, f"{src.eid} is in “{nb.name}”, which is archived, so a copy can't go there. "
                                 "Save it as a template and start a new experiment from that, or unarchive the notebook.")
    prefix = "Template: " if as_template else (f"×{scale:g} scale: " if scale else "Copy of ")
    exp = Experiment(
        title=prefix + src.title,
        notebook_id=None if as_template else src.notebook_id, description=src.description,
        owner_id=user.id, is_template=as_template, tags=list(src.tags or []),
    )
    session.add(exp)
    session.flush()
    for e in live_entries(session, src.id):
        content = copy.deepcopy(e.content)
        if e.kind == EntryKind.file:
            content = {"attachment_ids": [], "caption": content.get("caption", "")}
        if scale:
            content = _scaled(e.kind, content, scale)
        _new_entry(session, exp.id, e.kind, e.title, content, e.position, user)
    audit.record(session, user, "clone", "experiment", exp.id, source=src.eid, as_template=as_template, scale=scale)
    session.commit()
    return experiment_summary(session, exp)


def _scaled(kind: EntryKind, content: dict[str, Any], f: float) -> dict[str, Any]:
    """Content for a repeat at f× scale: typed amounts scale, equivalents and concentrations don't,
    fill-to-concentration solvents recalculate, and results (masses, spectra) start empty."""
    if kind == EntryKind.reaction:
        rows = []
        for r in content.get("rows") or []:
            r = dict(r)
            if r.get("role") == "product":
                r["actual_mass_mg"] = r["yield_pct"] = None
            else:
                b = r.get("basis")
                key = {"mass": "mass_mg", "volume": "volume_ml", "mmol": "mmol"}.get(b)
                if r.get("role") == "solvent" and b != "fill":
                    key = "volume_ml"
                if key and _num(r.get(key)) is not None:
                    r[key] = round(_num(r[key]) * f, 6)
            r["lot_id"] = None  # pick lots again for the new batch
            rows.append(r)
        content = {**content, "rows": rows}
        return _clean_content(kind, content)
    if kind == EntryKind.purification:
        return {**content, "isolated_mg": None}
    if kind == EntryKind.characterization:
        return {"items": [{"technique": it.get("technique", ""), "conditions": it.get("conditions", ""), "data": "", "attachment_id": None}
                          for it in content.get("items") or []]}
    if kind == EntryKind.safety:
        return {**content, "snapshot": None}  # amounts changed: reassess
    return content


# ====================================================================== entries


class EntryIn(BaseModel):
    kind: EntryKind
    title: str = Field("", max_length=300)
    content: dict[str, Any] = {}
    position: int | None = None
    plugin_section: str | None = None  # "plugin-id/section-id" for kind == custom


def _str(v: Any, n: int = 4000) -> str:
    """Text fields from the API: numbers become text, lists/objects become nothing."""
    if v is None or isinstance(v, (dict, list)):
        return ""
    return str(v)[:n]


_ROW_TEXT = ("role", "name", "smiles", "formula", "basis", "notes")
_ROW_NUMS = ("mw", "equiv", "mmol", "mass_mg", "volume_ml", "density", "conc_m", "purity",
             "actual_mass_mg", "theoretical_mass_mg", "yield_pct")


def _clean_row(r: dict[str, Any]) -> dict[str, Any]:
    """One reagent row as the app writes it: plain values only, numbers as numbers, ids as ints."""
    out = {k: v for k, v in r.items() if isinstance(k, str) and (v is None or isinstance(v, (str, int, float, bool)))}
    for k in _ROW_TEXT:
        if k in out:
            out[k] = _str(out[k], 2000)
    for k in _ROW_NUMS:
        if k in out:
            out[k] = _num(out[k])
    if out.get("purity") is None:
        out.pop("purity", None)
    for k in ("material_id", "lot_id"):
        if k in out and (isinstance(out[k], bool) or not isinstance(out[k], int)):
            out[k] = None
    out["limiting"] = bool(out.get("limiting"))
    return out


def _clean_content(kind: EntryKind, content: dict[str, Any]) -> dict[str, Any]:
    """Sanitise a section before storing it. Anything the API is sent (by the app, a script or a
    plugin) is brought to the shape the app writes, so one odd record can't break search or export."""
    content = dict(content or {})
    if kind == EntryKind.text:
        raw = _str(content.get("html"), 5_000_000)
        if open_depth(raw) > MAX_OPEN:
            raise HTTPException(422, "This text has formatting nested too deeply to store; paste it as plain text instead")
        content["html"] = clean_html(raw)
        if nesting_depth(content["html"]) > MAX_NESTING:
            raise HTTPException(422, "This text has formatting nested too deeply to store; paste it as plain text instead")
    elif kind == EntryKind.reaction:
        raw_cond = content.get("conditions") if isinstance(content.get("conditions"), dict) else {}
        cond = {k: raw_cond.get(k) for k in CONDITION_FIELDS}
        for k in cond:
            cond[k] = _num(cond[k]) if k in ("temperature_c", "time_h", "target_conc_m") else (_str(cond[k]) if cond[k] is not None else None)
        content["conditions"] = cond
        for k in ("reaction_smiles", "rxn_smiles"):
            if k in content:
                content[k] = _str(content[k], 20000)
        rows = [_clean_row(r) for r in content.get("rows") or [] if isinstance(r, dict)] if isinstance(content.get("rows"), list) else []
        content["rows"] = rows
        if rows:
            calc = chem.compute_stoichiometry(rows, cond["target_conc_m"])
            content["rows"], content["totals"] = calc["rows"], calc["totals"]
            content["warnings"] = calc["warnings"]
    elif kind == EntryKind.purification:
        content["notes_html"] = clean_html(_str(content.get("notes_html"), 500_000))
        content["isolated_mg"] = _num(content.get("isolated_mg"))
        for k in ("method", "stationary_phase", "eluent", "rf_eluent", "appearance", "rf"):
            if k in content and content[k] is not None:
                content[k] = _str(content[k], 400)
    elif kind == EntryKind.characterization:
        items = []
        for it in content.get("items") if isinstance(content.get("items"), list) else []:
            if not isinstance(it, dict):
                continue
            att = it.get("attachment_id")
            item = {"technique": _str(it.get("technique"), 200), "conditions": _str(it.get("conditions"), 2000),
                    "data": _str(it.get("data"), 200_000), "attachment_id": att if isinstance(att, int) and not isinstance(att, bool) else None}
            analysis = _clean_analysis(it.get("analysis"))
            if analysis:
                item["analysis"] = analysis
            items.append(item)
        content["items"] = items
    elif kind == EntryKind.safety:
        for k in ("controls", "waste", "emergency"):
            content[k] = str(content.get(k) or "")[:4000]
        content["notes_html"] = clean_html(_str(content.get("notes_html"), 500_000))
        snap = content.get("snapshot")
        content["snapshot"] = snap if isinstance(snap, dict) and len(json.dumps(snap)) < 200_000 else None
    elif kind == EntryKind.screen:
        def levels(v):
            return [str(x)[:40] for x in v if x is not None and not isinstance(x, (dict, list))][:12] if isinstance(v, list) else []
        content["x_name"], content["y_name"] = str(content.get("x_name") or "")[:80], str(content.get("y_name") or "")[:80]
        content["x_levels"], content["y_levels"] = levels(content.get("x_levels")), levels(content.get("y_levels")) or [""]
        content["response"] = str(content.get("response") or "Yield (%)")[:60]
        content["higher_is_better"] = bool(content.get("higher_is_better", True))
        cells = {}
        raw = content.get("cells")
        for k, c in (raw.items() if isinstance(raw, dict) else []):
            try:
                i, j = (int(x) for x in str(k).split(","))
            except ValueError:
                continue
            if 0 <= i < len(content["y_levels"]) and 0 <= j < len(content["x_levels"]) and isinstance(c, dict):
                cell = {"value": _num(c.get("value")), "note": str(c.get("note") or "")[:200]}
                if isinstance(c.get("experiment_id"), int):
                    cell["experiment_id"] = c["experiment_id"]
                if cell["value"] is not None or cell["note"] or "experiment_id" in cell:
                    cells[f"{i},{j}"] = cell
        content["cells"] = cells
    elif kind == EntryKind.table:
        cols = content.get("columns") if isinstance(content.get("columns"), list) else []
        content["columns"] = [{**{k: v for k, v in c.items() if isinstance(v, (str, int, float, bool)) or v is None}, "name": _str(c.get("name"), 200)}
                              for c in cols if isinstance(c, dict)][:200]
        rows = content.get("rows") if isinstance(content.get("rows"), list) else []
        content["rows"] = [[v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else _str(v, 2000) for v in r]
                           for r in rows if isinstance(r, list)][:20000]
    elif kind == EntryKind.references:
        content = doi.clean_content(content)
    elif kind == EntryKind.custom:
        try:
            content = plugins.clean_content(content)
        except plugins.PluginError as e:
            raise HTTPException(422, str(e)) from None
    elif kind == EntryKind.file:
        ids = content.get("attachment_ids")
        content["attachment_ids"] = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)] if isinstance(ids, list) else []
        if "caption" in content:
            content["caption"] = _str(content["caption"], 2000)
        raw_an = content.get("analyses") if isinstance(content.get("analyses"), dict) else {}
        analyses = {str(k): _clean_analysis(v) for k, v in raw_an.items()}
        content["analyses"] = {k: v for k, v in analyses.items() if v}
    return content


_entry_lock = threading.Lock()  # positions are read-then-written; serialise adds and reorders


def _clean_filename(name: str | None) -> str:
    """Just the file's own name: no folders, no control characters."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip().lstrip(".") or "file"
    if len(base) <= 200:
        return base
    stem, _, ext = base.rpartition(".")  # a very long name is shortened in the middle: the extension says what it is
    return f"{stem[:199 - len(ext)]}.{ext}" if stem and 0 < len(ext) <= 10 else base[:200]


@router.post("/experiments/{exp_id}/entries", status_code=201)
def add_entry(exp_id: int, body: EntryIn, user: Writer, session: DB):
    with _entry_lock:
        return _add_entry(exp_id, body, user, session)


def _add_entry(exp_id: int, body: EntryIn, user, session) -> dict[str, Any]:
    exp = read_experiment(session, user, exp_id)
    require_editable(exp, user)
    existing = live_entries(session, exp.id)
    pos = body.position if body.position is not None else (max((e.position for e in existing), default=-1) + 1)
    for e in existing:
        if e.position >= pos:
            e.position += 1
            session.add(e)
    title = body.title
    if body.kind == EntryKind.custom and not body.content:
        sec = next((x for x in plugins.enabled_sections(session) if f"{x['plugin']}/{x['id']}" == body.plugin_section), None)
        if sec is None:
            raise HTTPException(422, "That section type isn't available; an admin may have switched its plugin off")
        content, title = plugins.new_content(sec), title or sec["title"]
    else:
        content = _clean_content(body.kind, body.content or copy.deepcopy(DEFAULT_CONTENT.get(body.kind, {})))
    e = _new_entry(session, exp.id, body.kind, title or body.kind.value.title(), content, pos, user)
    exp.updated_at = utcnow()
    session.add(exp)
    audit.record(session, user, "create", "entry", e.id, experiment=exp.eid, kind=e.kind.value, title=e.title)
    session.commit()
    return entry_out(e)


class EntryUpdate(BaseModel):
    title: str | None = Field(None, max_length=300)
    content: dict[str, Any] | None = None
    expected_version: int | None = None


@router.put("/entries/{entry_id}")
def update_entry(entry_id: int, body: EntryUpdate, user: Writer, session: DB):
    e = get_or_404(session, Entry, entry_id)
    exp = read_experiment(session, user, e.experiment_id)
    require_editable(exp, user)
    session.refresh(e)  # now that this edit holds the record, read the section as it is
    if e.deleted:
        raise HTTPException(status.HTTP_409_CONFLICT, "This section was removed elsewhere; reload")
    if body.expected_version is not None and body.expected_version != e.version:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Entry was changed elsewhere (now v{e.version}); reload before saving")
    new_title = body.title if body.title is not None else e.title
    new_content = _clean_content(e.kind, body.content) if body.content is not None else e.content
    if new_title == e.title and audit.canonical(new_content) == audit.canonical(e.content):
        return entry_out(e)
    e.title, e.content = new_title, new_content
    e.version += 1
    e.updated_by, e.updated_at = user.id, utcnow()
    exp.updated_at = e.updated_at
    session.add_all([e, exp, EntryVersion(entry_id=e.id, version=e.version, title=e.title,
                                          content=new_content, created_by=user.id)])
    audit.record(session, user, "update", "entry", e.id, experiment=exp.eid, version=e.version,
                 content_sha256=hashlib.sha256(audit.canonical(new_content).encode()).hexdigest())
    session.commit()
    return entry_out(e)


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: int, user: Writer, session: DB):
    """Soft delete: the entry and every prior version remain in the history."""
    e = get_or_404(session, Entry, entry_id)
    exp = read_experiment(session, user, e.experiment_id)
    require_editable(exp, user)
    session.refresh(e)
    e.deleted = True
    e.version += 1
    session.add_all([e, EntryVersion(entry_id=e.id, version=e.version, title=e.title,
                                     content=e.content, deleted=True, created_by=user.id)])
    audit.record(session, user, "delete", "entry", e.id, experiment=exp.eid, title=e.title)
    session.commit()
    return {"ok": True}


@router.post("/entries/{entry_id}/restore")
def restore_entry(entry_id: int, user: Writer, session: DB):
    """Undo a removal: the section comes back where it was, as a new version."""
    e = get_or_404(session, Entry, entry_id)
    exp = read_experiment(session, user, e.experiment_id)
    require_editable(exp, user)
    session.refresh(e)
    if not e.deleted:
        return entry_out(e)
    e.deleted = False
    e.version += 1
    e.updated_by, e.updated_at = user.id, utcnow()
    session.add_all([e, EntryVersion(entry_id=e.id, version=e.version, title=e.title, content=e.content, created_by=user.id)])
    audit.record(session, user, "restore", "entry", e.id, experiment=exp.eid, title=e.title)
    session.commit()
    return entry_out(e)


class ReorderIn(BaseModel):
    entry_ids: list[int]


@router.post("/experiments/{exp_id}/reorder")
def reorder_entries(exp_id: int, body: ReorderIn, user: Writer, session: DB):
    exp = read_experiment(session, user, exp_id)
    require_editable(exp, user)
    entries = {e.id: e for e in live_entries(session, exp.id)}
    if set(body.entry_ids) != set(entries):
        raise HTTPException(400, "entry_ids must list every entry exactly once")
    for i, eid in enumerate(body.entry_ids):
        entries[eid].position = i
        session.add(entries[eid])
    audit.record(session, user, "reorder", "experiment", exp.id, order=body.entry_ids)
    session.commit()
    return {"ok": True}


@router.get("/entries/{entry_id}/versions")
def entry_versions(entry_id: int, user: CurrentUser, session: DB):
    read_experiment(session, user, get_or_404(session, Entry, entry_id).experiment_id)
    vs = session.exec(select(EntryVersion).where(EntryVersion.entry_id == entry_id)
                      .order_by(EntryVersion.version.desc()))
    return [{"version": v.version, "title": v.title, "content": v.content, "deleted": v.deleted,
             "created_at": v.created_at.isoformat() + "Z", "created_by": user_brief(session, v.created_by)}
            for v in vs]


# ====================================================================== attachments


def attachment_out(a: Attachment) -> dict[str, Any]:
    return {"id": a.id, "filename": a.filename, "content_type": a.content_type, "size": a.size,
            "sha256": a.sha256, "created_at": a.created_at.isoformat() + "Z",
            "from_instrument": a.instrument_file_id is not None}


def _blob_path(sha: str):
    return settings.attachments_dir / sha[:2] / sha


@router.post("/experiments/{exp_id}/attachments", status_code=201)
def upload_attachment(exp_id: int, user: Writer, session: DB, file: UploadFile = File(...)):
    # A plain function, run on a worker thread, not `async`: before 0.24.2 this held the database's write lock while
    # waiting for the file, and a second upload at the same moment then froze the whole server for 30 s.
    exp = read_experiment(session, user, exp_id)
    require_editable(exp, user, claim=False)  # check first; the record is taken only once the file is stored
    try:  # content-addressed, deduplicated; complete or not there at all (app/storage.py)
        sha, size = storage.save(file.file, limit=settings.max_upload_mb * 1024 * 1024)
    except ValueError:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB") from None
    except OSError as exc:  # a full disk: said plainly, and nothing was stored (0.25)
        from .. import diskspace
        if not diskspace.is_disk_full(exc):
            raise
        raise HTTPException(507, diskspace.message(exc, file=True)) from None
    session.refresh(exp)
    require_editable(exp, user)
    a = Attachment(experiment_id=exp.id, filename=_clean_filename(file.filename),
                   content_type=file.content_type or "application/octet-stream",
                   size=size, sha256=sha, uploaded_by=user.id)
    session.add(a)
    session.flush()
    audit.record(session, user, "upload", "attachment", a.id, experiment=exp.eid,
                 filename=a.filename, sha256=sha, size=a.size)
    session.commit()
    return attachment_out(a)


@router.get("/attachments/{att_id}")
def download_attachment(att_id: int, user: CurrentUser, session: DB, inline: bool = False):
    a = get_or_404(session, Attachment, att_id)
    read_experiment(session, user, a.experiment_id)
    if a.withdrawn_at is not None:  # sent back with "Not mine": it's someone else's data now (see the Inbox)
        raise HTTPException(404, f"Attachment {att_id} not found")
    path = _blob_path(a.sha256)
    if not path.exists():
        raise HTTPException(410, "Attachment blob missing from storage")
    h = hashlib.sha256()
    with open(path, "rb") as f:  # in pieces: instrument data can be gigabytes
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != a.sha256:
        raise HTTPException(500, "Attachment failed integrity check")
    from ..eln_import import content_type
    media = content_type(a.content_type)  # an odd type stored by an import (0.20.0–0.20.2) can't add headers
    resp = FileResponse(path, media_type=media, filename=a.filename,
                        content_disposition_type="inline" if inline else "attachment")
    if media != "application/pdf":  # an uploaded .html/.svg can never run script on Benchtop's address
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'"
    return resp


# ====================================================================== signing workflow


class SignIn(BaseModel):
    password: str
    comment: str = Field("", max_length=4000)
    witness_id: int | None = None  # author signing: who to ask to witness (optional)


def _check_password(user: User, password: str, session) -> None:
    from ..security import has_password
    if not has_password(user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Signing needs a password. You sign in with single sign-on, so set a signing password in Account settings first.")
    from ..security import MAX_PER_ACCOUNT, check_throttle, clear_failures, record_failure
    key = f"sign:{user.id}"
    check_throttle(session, (key, MAX_PER_ACCOUNT))
    if not verify_password(password, user.password_hash):
        record_failure(session, key)
        audit.record(session, user, "signature_failed", "user", user.id)
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Password re-entry failed; signature not applied")
    clear_failures(session, key)


def _claim(session, exp: Experiment, to: ExperimentStatus, *, frm: ExperimentStatus | None = None,
           not_: ExperimentStatus | None = None, msg: str = "") -> None:
    """Move the record to a new status only if nobody else has moved it first.

    Two clicks on "Sign" (or two people witnessing at once) used to both pass the status check and both
    write a signature. A conditional UPDATE is atomic in the database, so exactly one request wins and
    the others get 409."""
    from sqlalchemy import update
    cond = Experiment.id == exp.id
    cond = cond & (Experiment.status == frm) if frm is not None else cond & (Experiment.status != not_)
    res = session.execute(update(Experiment).where(cond).values(status=to, updated_at=utcnow()))
    if res.rowcount != 1:
        session.rollback()
        raise HTTPException(409, msg or f"{exp.eid} was changed by someone else just now; reload and try again")
    exp.status = to
    exp.updated_at = utcnow()


def _sign(session, exp: Experiment, user: User, meaning: str, comment: str) -> Signature:
    sig = Signature(experiment_id=exp.id, user_id=user.id, meaning=meaning, comment=comment,
                    content_hash=content_hash(session, exp), signer_name=user.full_name or user.username,
                    signer_username=user.username)
    session.add(sig)
    session.flush()
    audit.record(session, user, f"sign:{meaning}", "experiment", exp.id, eid=exp.eid,
                 content_hash=sig.content_hash, comment=comment)
    timestamps.queue_for(session, sig)  # an independent service certifies when it was signed (fetched after commit)
    return sig


@router.post("/experiments/{exp_id}/sign")
def sign_experiment(exp_id: int, body: SignIn, user: Writer, session: DB):
    """Author signature: locks the record and sends it for witnessing."""
    exp = read_experiment(session, user, exp_id)
    if exp.is_template:
        raise HTTPException(400, "Templates are not signed")
    if exp.status != ExperimentStatus.open:
        raise HTTPException(409, f"{exp.eid} is already {exp.status.value}")
    if exp.owner_id != user.id:
        raise HTTPException(403, "Only the author can sign as author")
    missing = [f"{e.title}: {', '.join(m)}" for e in live_entries(session, exp.id) if e.kind == EntryKind.custom
               for m in [plugins.missing_required(e.content or {})] if m]
    if missing:
        raise HTTPException(422, "Fill in the required items before signing — " + "; ".join(missing))
    if body.witness_id is not None:
        witness.validate(session, exp, body.witness_id)  # before signing, so a bad choice doesn't leave it half done
    _check_password(user, body.password, session)
    _claim(session, exp, ExperimentStatus.signed, frm=ExperimentStatus.open, msg=f"{exp.eid} has already been signed")
    _sign(session, exp, user, "author", body.comment)
    nmrpredict.lab_record(session, exp)  # its confirmed NMR assignments now teach the predictor
    if body.witness_id is not None:
        witness.request(session, exp, user, body.witness_id)
    session.commit()
    return get_experiment(exp.id, user, session)


def _author_sig(session, exp: Experiment) -> Signature | None:
    return session.exec(select(Signature).where(
        Signature.experiment_id == exp.id, Signature.meaning == "author").order_by(Signature.id.desc())).first()


@router.post("/experiments/{exp_id}/witness")
def witness_experiment(exp_id: int, body: SignIn, user: Writer, session: DB):
    exp = read_experiment(session, user, exp_id)
    if exp.status != ExperimentStatus.signed:
        raise HTTPException(409, "Only author-signed experiments can be witnessed")
    if exp.owner_id == user.id:
        raise HTTPException(403, "The author cannot witness their own experiment")
    _check_password(user, body.password, session)
    author = _author_sig(session, exp)
    if author is None or author.content_hash != content_hash(session, exp):
        raise HTTPException(409, "Record content no longer matches the author signature")
    _claim(session, exp, ExperimentStatus.witnessed, frm=ExperimentStatus.signed, msg=f"{exp.eid} is no longer awaiting a witness")
    _sign(session, exp, user, "witness", body.comment)
    witness.close(session, exp, "witnessed", user)
    session.commit()
    return get_experiment(exp.id, user, session)


@router.post("/experiments/{exp_id}/reject")
def reject_experiment(exp_id: int, body: SignIn, user: Writer, session: DB):
    """Witness declines; record returns to the author as open, with the reason recorded."""
    exp = read_experiment(session, user, exp_id)
    if exp.status != ExperimentStatus.signed:
        raise HTTPException(409, "Only experiments awaiting witness can be rejected")
    if exp.owner_id == user.id:
        raise HTTPException(403, "Use 'withdraw' instead; authors cannot reject their own records")
    if not body.comment.strip():
        raise HTTPException(400, "A reason is required")
    _check_password(user, body.password, session)
    _claim(session, exp, ExperimentStatus.open, frm=ExperimentStatus.signed, msg=f"{exp.eid} is no longer awaiting a witness")
    _sign(session, exp, user, "rejected", body.comment)
    nmrpredict.lab_forget(session, exp.id)
    witness.close(session, exp, "returned", user, body.comment)
    session.commit()
    return get_experiment(exp.id, user, session)


@router.post("/experiments/{exp_id}/withdraw")
def withdraw_signature(exp_id: int, body: SignIn, user: Writer, session: DB):
    """Author pulls a signed (not yet witnessed) record back to open."""
    exp = read_experiment(session, user, exp_id)
    if exp.status != ExperimentStatus.signed or exp.owner_id != user.id:
        raise HTTPException(409, "Only the author can withdraw a record awaiting witness")
    _check_password(user, body.password, session)
    _claim(session, exp, ExperimentStatus.open, frm=ExperimentStatus.signed, msg=f"{exp.eid} is no longer awaiting a witness")
    _sign(session, exp, user, "withdrawn", body.comment)
    nmrpredict.lab_forget(session, exp.id)
    witness.close(session, exp, "withdrawn", user)
    session.commit()
    return get_experiment(exp.id, user, session)


@router.post("/experiments/{exp_id}/void")
def void_experiment(exp_id: int, body: SignIn, user: Writer, session: DB):
    exp = read_experiment(session, user, exp_id)
    if exp.owner_id != user.id and user.role != Role.admin:
        raise HTTPException(403, "Only the author or an admin can void")
    if exp.status == ExperimentStatus.voided:
        raise HTTPException(409, "Already voided")
    if not body.comment.strip():
        raise HTTPException(400, "A reason is required")
    _check_password(user, body.password, session)
    _claim(session, exp, ExperimentStatus.voided, not_=ExperimentStatus.voided, msg="Already voided")
    _sign(session, exp, user, "void", body.comment)
    nmrpredict.lab_forget(session, exp.id)
    witness.close(session, exp, "voided", user)
    session.commit()
    return get_experiment(exp.id, user, session)


@router.get("/experiments/{exp_id}/witness-candidates")
def witness_candidates(exp_id: int, user: CurrentUser, session: DB):
    """Colleagues who can be asked to witness this record."""
    exp = read_experiment(session, user, exp_id)
    return {"candidates": witness.candidates(session, exp), "email": mailer.configured()}


class WitnessRequestIn(BaseModel):
    witness_id: int


def _request_owner(session, user: User, exp_id: int) -> Experiment:
    exp = read_experiment(session, user, exp_id)
    if exp.owner_id != user.id and user.role != Role.admin:
        raise HTTPException(403, "Only the author (or an admin) can choose the witness")
    if exp.status != ExperimentStatus.signed:
        raise HTTPException(409, f"{exp.eid} isn't waiting for a witness")
    return exp


@router.put("/experiments/{exp_id}/witness-request")
def set_witness_request(exp_id: int, body: WitnessRequestIn, user: Writer, session: DB):
    """Ask someone (else) to witness a signed record."""
    exp = _request_owner(session, user, exp_id)
    witness.request(session, exp, user, body.witness_id)
    audit.record(session, user, "witness_request", "experiment", exp.id, eid=exp.eid, witness_id=body.witness_id)
    session.commit()
    return witness.summary(session, exp)


@router.post("/experiments/{exp_id}/witness-request/remind")
def remind_witness(exp_id: int, user: Writer, session: DB):
    exp = _request_owner(session, user, exp_id)
    witness.lock(session, exp)
    r = witness.open_request(session, exp.id)
    if r is None:
        raise HTTPException(404, "Nobody has been asked to witness this record")
    if r.reminded_at and utcnow() - r.reminded_at < witness.MANUAL_REMIND_GAP:
        raise HTTPException(429, "A reminder went out less than an hour ago")
    witness.remind(session, r, manual_by=user)
    session.commit()
    return witness.summary(session, exp)


@router.get("/signatures/{sig_id}/timestamp")
def download_timestamp(sig_id: int, user: CurrentUser, session: DB):
    """The trusted timestamp of a signature, with what was timestamped and how to check it with openssl."""
    from ..models import SignatureTimestamp
    sig = get_or_404(session, Signature, sig_id)
    exp = read_experiment(session, user, sig.experiment_id)
    row = session.exec(select(SignatureTimestamp).where(SignatureTimestamp.signature_id == sig.id)).first()
    if row is None or row.status != "ok":
        raise HTTPException(404, "This signature has no trusted timestamp (yet)")
    data = timestamps.proof_zip(session, sig, row)
    name = f"{exp.eid}-signature-{sig.id}-timestamp.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/experiments/{exp_id}/verify")
def verify_experiment(exp_id: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    current = content_hash(session, exp)
    sigs = signatures_out(session, exp.id)
    for s in sigs:
        s["matches_current"] = s["content_hash"] == current
    attestations = [s for s in sigs if s["meaning"] in ("author", "witness")]
    latest = attestations[-1] if attestations else None
    return {"content_hash": current, "signatures": sigs,
            "intact": latest is None or exp.status == ExperimentStatus.open or latest["matches_current"]}


@router.get("/experiments/{exp_id}/writeup")
def experiment_writeup(exp_id: int, user: CurrentUser, session: DB):
    from .. import writeup

    exp = read_experiment(session, user, exp_id)
    return writeup.draft(live_entries(session, exp.id))


@router.get("/experiments/{exp_id}/audit")
def experiment_audit(exp_id: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    entry_ids = [e.id for e in session.exec(select(Entry).where(Entry.experiment_id == exp.id))]
    att_ids = [a.id for a in session.exec(select(Attachment).where(Attachment.experiment_id == exp.id))]
    from ..models import AuditEvent

    q = select(AuditEvent).where(
        ((AuditEvent.object_type == "experiment") & (AuditEvent.object_id == exp.id))
        | ((AuditEvent.object_type == "entry") & (AuditEvent.object_id.in_(entry_ids or [-1])))
        | ((AuditEvent.object_type == "attachment") & (AuditEvent.object_id.in_(att_ids or [-1])))
    ).order_by(AuditEvent.id.desc())
    return [audit_out(ev) for ev in session.exec(q)]


def audit_out(ev) -> dict[str, Any]:
    return {"id": ev.id, "ts": ev.ts.isoformat() + "Z", "username": ev.username, "action": ev.action,
            "object_type": ev.object_type, "object_id": ev.object_id, "details": ev.details,
            "hash": ev.hash}


# ====================================================================== export


PURIFICATION_LABELS = {
    "column": "Flash column chromatography", "prep_tlc": "Preparative TLC", "prep_hplc": "Preparative HPLC",
    "recrystallization": "Recrystallisation", "trituration": "Trituration", "distillation": "Distillation",
    "extraction": "Extraction only", "none": "Used crude",
}


def _fmt(v, nd=3):
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return html.escape(str(v))


_IMG_REF = re.compile(r'<img\b[^>]*?data-attachment-id="(\d+)"[^>]*>')


def _inline_images(h: str, atts: dict[int, Attachment], pdf: bool = False) -> str:
    """Pasted pictures are stored as attachment references; reports carry the picture itself, at the size and
    place chosen in the editor (data-width = % of the text width; data-align = center, right, wrap-left, wrap-right)."""
    import base64

    def sub(m):
        tag = m.group(0)
        a = atts.get(int(m.group(1)))
        if not a or not a.content_type.startswith("image/") or a.size > 8_000_000:
            return f"<em>[image: {html.escape(a.filename) if a else 'missing'}]</em>"
        try:
            raw = _blob_path(a.sha256).read_bytes()
        except OSError:
            return f"<em>[image: {html.escape(a.filename)}]</em>"
        data = base64.b64encode(raw).decode()
        am = re.search(r'data-align="(center|right|wrap-left|wrap-right)"', tag)
        align = am.group(1) if am else None
        wm = re.search(r'data-width="(\d{1,3})"', tag)
        width = int(wm.group(1)) if wm and 10 <= int(wm.group(1)) <= 100 else None
        wrap = align in ("wrap-left", "wrap-right")
        if wrap and width is None:
            width = 40
        src = f"data:{a.content_type};base64,{data}"
        if pdf:  # the PDF renderer needs real sizes, and has no text wrapping: wrapped pictures sit left/right
            try:
                import io

                from PIL import Image
                w, hh = Image.open(io.BytesIO(raw)).size
                k = min(460 * width / 100 / w, 600 / hh) if width else min(460 / w, 340 / hh, 1.0)
                img = f'<img src="{src}" width="{max(1, int(w * k))}" height="{max(1, int(hh * k))}">'
            except Exception:
                return f"<em>[image: {html.escape(a.filename)}]</em>"
            side = {"center": "center", "right": "right", "wrap-right": "right"}.get(align or "")
            return f'<div style="text-align:{side}">{img}</div>' if side else img
        style = f"width:{width}%;height:auto" if width else "max-width:100%;max-height:420px"
        if wrap:
            side = align[5:]
            margin = "0.2em 1.1em 0.6em 0" if side == "left" else "0.2em 0 0.6em 1.1em"
            return f'<img src="{src}" alt="{html.escape(a.filename)}" style="float:{side};{style};margin:{margin}">'
        img = f'<img src="{src}" alt="{html.escape(a.filename)}" style="max-width:100%;{style}">'
        return f'<div style="text-align:{align}">{img}</div>' if align in ("center", "right") else img
    return _IMG_REF.sub(sub, h)


def _render_entry_safe(e: Entry, atts: dict[int, Attachment], pdf: bool = False) -> str:
    try:
        return _render_entry(e, atts, pdf)
    except Exception:
        log.exception("Couldn't render entry %s", e.id)
        return f"<section><h2>{html.escape(e.title or '')}</h2><p><i>This section couldn't be shown in the report.</i></p></section>"


def _render_entry(e: Entry, atts: dict[int, Attachment], pdf: bool = False) -> str:
    c = e.content or {}
    h = f"<section><h2>{html.escape(e.title)}</h2>"
    if e.kind == EntryKind.text:
        h += f"<div class='rich'>{_marks_inline(_inline_images(clean_html(c.get('html', '')), atts, pdf))}</div>"
    elif e.kind == EntryKind.reaction:
        cond = c.get("conditions") or {}
        bits = []
        if cond.get("temperature_c") is not None or cond.get("temperature_label"):
            t = f"{_fmt(cond.get('temperature_c'))} °C" if cond.get("temperature_c") is not None else ""
            bits.append(" ".join(x for x in (t, html.escape(cond.get("temperature_label") or "")) if x))
        if cond.get("time_h") is not None:
            bits.append(f"{_fmt(cond['time_h'])} h")
        if cond.get("atmosphere"):
            bits.append(html.escape(cond["atmosphere"]))
        if cond.get("target_conc_m"):
            bits.append(f"{_fmt(cond['target_conc_m'])} M")
        if bits:
            h += f"<p><strong>Conditions:</strong> {' · '.join(bits)}</p>"
        if cond.get("notes"):
            h += f"<p>{html.escape(cond['notes'])}</p>"
        if c.get("reaction_smiles"):
            try:
                if pdf:
                    import base64
                    png = base64.b64encode(chem.depict_reaction_png(c["reaction_smiles"])).decode()
                    h += f"<div class='rxn'><img src='data:image/png;base64,{png}' width='500' height='129'></div>"
                else:
                    h += f"<div class='rxn'>{chem.depict_reaction_svg(c['reaction_smiles'])}</div>"
            except chem.ChemError:
                h += f"<p><code>{html.escape(c['reaction_smiles'])}</code></p>"
        cols = [("role", "Role"), ("name", "Name"), ("formula", "Formula"), ("mw", "MW"),
                ("equiv", "Equiv"), ("mmol", "mmol"), ("mass_mg", "Mass (mg)"),
                ("volume_ml", "Vol (mL)"), ("theoretical_mass_mg", "Theor. (mg)"),
                ("actual_mass_mg", "Actual (mg)"), ("yield_pct", "Yield %")]
        if pdf:  # an A4 page has no room for the formula column
            cols = [col for col in cols if col[0] != "formula"]
        widths = {"role": "11%", "name": "27%"} if pdf else {}
        h += "<table><tr>" + "".join(f"<th{(' width=' + chr(34) + widths[k] + chr(34)) if k in widths else ''}>{t}</th>" for k, t in cols) + "</tr>"
        for r in c.get("rows", []):
            h += "<tr>" + "".join(
                f"<td>{_fmt(r.get(k))}{' ★' if k == 'role' and r.get('limiting') else ''}</td>"
                for k, _ in cols) + "</tr>"
        h += "</table>"
        m = chem.green_metrics([chem.new_row(**r) for r in c.get("rows", [])]) if c.get("rows") else {}
        parts = [f"{label} {_fmt(m[k])}{unit}" for k, label, unit in METRIC_LABELS if m.get(k) is not None]
        if parts:
            h += f"<p><strong>Green metrics</strong> ({html.escape(m['scope'])}): {' · '.join(parts)}</p>"
    elif e.kind == EntryKind.safety:
        snap = c.get("snapshot") or {}
        if snap.get("chemicals"):
            h += f"<p>Hazard data recorded {html.escape(str(snap.get('taken_at', ''))[:10])} from PubChem.</p>"
            h += "<table><tr><th>Chemical</th><th>Signal</th><th>Hazard statements</th><th>Source</th></tr>"
            for ch in snap["chemicals"]:
                h += (f"<tr><td>{html.escape(ch.get('name', ''))}</td><td>{html.escape(ch.get('signal', ''))}</td>"
                      f"<td>{html.escape('; '.join(ch.get('hazards') or []))}</td><td>{html.escape(ch.get('source', ''))}</td></tr>")
            h += "</table>"
            if snap.get("warnings"):
                h += "<ul>" + "".join(f"<li>{html.escape(w.get('text', ''))}</li>" for w in snap["warnings"]) + "</ul>"
        for k, label in (("controls", "Controls"), ("waste", "Waste"), ("emergency", "Emergency")):
            if c.get(k):
                h += f"<p><strong>{label}:</strong> {html.escape(c[k])}</p>"
        h += f"<div class='rich'>{_marks_inline(_inline_images(clean_html(c.get('notes_html', '')), atts, pdf))}</div>"
    elif e.kind == EntryKind.screen:
        xs, ys, cells = c.get("x_levels") or [], c.get("y_levels") or [], c.get("cells") or {}
        h += f"<p>{html.escape(c.get('response', ''))} by {html.escape(c.get('y_name', ''))} (rows) and {html.escape(c.get('x_name', ''))} (columns)</p>"
        h += "<table><tr><th></th>" + "".join(f"<th>{html.escape(x)}</th>" for x in xs) + "</tr>"
        for i, yv in enumerate(ys):
            h += f"<tr><th>{html.escape(yv)}</th>" + "".join(
                f"<td>{_fmt((cells.get(f'{i},{j}') or {}).get('value'))}</td>" for j in range(len(xs))) + "</tr>"
        h += "</table>"
    elif e.kind == EntryKind.custom:
        sch, vals = c.get("schema") or {}, c.get("values") or {}
        shown = [(f["label"], vals.get(f["id"]), f) for f in sch.get("fields", [])]
        h += "<table>" + "".join(
            f"<tr><th>{html.escape(lab)}</th><td>{'✓' if v is True else '—' if v in (None, '', False) else html.escape(str(v)).replace(chr(10), '<br>')}"
            f"{(' ' + html.escape(f['unit'])) if f.get('unit') and v not in (None, '', False) else ''}</td></tr>" for lab, v, f in shown) + "</table>"
        for t in sch.get("tables", []):
            rows = (c.get("rows") or {}).get(t["id"], [])
            h += f"<p><b>{html.escape(t['label'])}</b></p><table><tr>" + "".join(
                f"<th>{html.escape(col['label'])}{(' (' + html.escape(col['unit']) + ')') if col.get('unit') else ''}</th>" for col in t["columns"]) + "</tr>"
            for r in rows:
                h += "<tr>" + "".join(f"<td>{'✓' if r.get(col['id']) is True else html.escape(str(r.get(col['id']) if r.get(col['id']) not in (None, False) else ''))}</td>" for col in t["columns"]) + "</tr>"
            h += "</table>"
        if sch.get("plugin"):
            h += f"<p class='meta' style='font-size:11px'>Section type: {html.escape(sch.get('name', ''))} (plugin {html.escape(sch['plugin'])})</p>"
    elif e.kind == EntryKind.table:
        cols = [col if isinstance(col, dict) else {"name": col} for col in c.get("columns") or [] if col is not None]
        h += "<table><tr>" + "".join(
            f"<th>{html.escape(str(col.get('name', '')))}{(' (' + html.escape(str(col['unit'])) + ')') if col.get('unit') else ''}</th>"
            for col in cols) + "</tr>"
        for row in c.get("rows") or []:
            if isinstance(row, list):
                h += "<tr>" + "".join(f"<td>{_fmt(v)}</td>" for v in row) + "</tr>"
        h += "</table>"
    elif e.kind == EntryKind.purification:
        rows = [("Method", PURIFICATION_LABELS.get(c.get("method"), c.get("method") or "")),
                ("Stationary phase", c.get("stationary_phase")), ("Eluent", c.get("eluent")),
                ("Rf", f"{c.get('rf') or ''} {('(' + c['rf_eluent'] + ')') if c.get('rf_eluent') else ''}".strip()),
                ("Isolated", f"{_fmt(c.get('isolated_mg'))} mg" if c.get("isolated_mg") is not None else ""),
                ("Appearance", c.get("appearance"))]
        h += "<table>" + "".join(f"<tr><th>{k}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows if v) + "</table>"
        h += f"<div class='rich'>{_marks_inline(_inline_images(clean_html(c.get('notes_html', '')), atts, pdf))}</div>"
    elif e.kind == EntryKind.characterization:
        h += "<table><tr><th>Technique</th><th>Conditions</th><th>Data</th><th>File</th></tr>"
        for it in c.get("items", []):
            a = atts.get(it.get("attachment_id"))
            h += (f"<tr><td>{html.escape(it.get('technique', ''))}</td><td>{html.escape(it.get('conditions', ''))}</td>"
                  f"<td>{html.escape(it.get('data', ''))}</td><td>{html.escape(a.filename) if a else ''}</td></tr>")
        h += "</table>"
    elif e.kind == EntryKind.references:
        h += _references_html(c.get("items") or [])
    elif e.kind == EntryKind.file:
        h += "<ul>" + "".join(
            f"<li>{html.escape(atts[i].filename)} — {atts[i].size:,} bytes — sha256 <code>{atts[i].sha256}</code></li>"
            for i in c.get("attachment_ids", []) if i in atts) + "</ul>"
        h += "".join(_inline_images(f'<p><img data-attachment-id="{i}"></p>', atts, pdf)
                     for i in c.get("attachment_ids", []) if i in atts and atts[i].content_type.startswith("image/"))
        if c.get("caption"):
            h += f"<p>{html.escape(str(c['caption']))}</p>"
    return h + "</section>"


def _references_html(items: list) -> str:
    """A numbered list, ACS style: Authors. Title. <i>Journal</i> <b>Year</b>, <i>Volume</i> (Issue), Pages. DOI link."""
    out = []
    for it in items:
        if doi.is_blank(it):
            continue
        c = doi.cite(it)
        bits = []
        if c["authors"]:
            bits.append(html.escape(c["authors"].rstrip(".")) + ".")
        if c["title"]:
            bits.append(html.escape(c["title"].rstrip(".")) + ".")
        src = " ".join(x for x in (f"<i>{html.escape(c['journal'])}</i>" if c["journal"] else "",
                                    f"<b>{html.escape(c['year'])}</b>" if c["year"] else "") if x)
        vol = (f"<i>{html.escape(c['volume'])}</i>" if c["volume"] else "") + (f" ({html.escape(c['issue'])})" if c["issue"] else "")
        tail = ", ".join(x for x in (src, vol.strip(), html.escape(c["pages"])) if x)
        if tail:
            bits.append(tail + ".")
        if c["link"]:
            bits.append(f"<a href=\"{html.escape(c['link'])}\">{html.escape(c['link'])}</a>")
        elif str(it.get("doi") or "").strip():
            bits.append(html.escape(str(it["doi"]).strip()))
        note = f"<br><span class='meta'>{html.escape(c['note']).replace(chr(10), '<br>')}</span>" if c["note"] else ""
        out.append(f"<li>{' '.join(bits)}{note}</li>")
    return f"<ol class='refs'>{''.join(out)}</ol>" if out else "<p class='meta'>No references yet.</p>"


@router.get("/experiments/{exp_id}/export")
def export_experiment(exp_id: int, user: CurrentUser, session: DB, format: str = "html"):
    exp = read_experiment(session, user, exp_id)
    if format == "md":
        from ..export_md import experiment_markdown, safe_name

        md = experiment_markdown(session, exp)
        return Response(md, media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{exp.eid} {safe_name(exp.title)}.md"'.encode("latin-1", "replace").decode("latin-1")})
    if format == "json":
        return {"experiment": experiment_summary(session, exp), "snapshot": snapshot(session, exp),
                "content_hash": content_hash(session, exp), "signatures": signatures_out(session, exp.id)}
    if format == "pdf":
        return _pdf_response(session, exp)
    return HTMLResponse(report_html(session, exp))


def pdf_bytes(session, exp: Experiment) -> bytes:
    """The record as an A4 PDF (Download PDF; also what is published on Zenodo)."""
    import io
    try:
        from xhtml2pdf import pisa
    except ImportError:
        raise HTTPException(503, "PDF export needs one more package: stop Benchtop and start it again so it can install it") from None
    out = io.BytesIO()
    try:
        res = pisa.CreatePDF(report_html(session, exp, pdf=True), dest=out, encoding="utf-8")
    except RecursionError:
        raise HTTPException(422, "This record's formatting is too complex for a PDF; the printable report (Print) still works") from None
    if res.err:
        raise HTTPException(500, "Couldn't make the PDF; the printable report (Print) still works")
    return out.getvalue()


def _pdf_response(session, exp: Experiment) -> Response:
    from ..export_md import safe_name
    name = f"{exp.eid} {safe_name(exp.title)}.pdf".encode("latin-1", "replace").decode("latin-1")
    return Response(pdf_bytes(session, exp), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def report_html(session, exp: Experiment, pdf: bool = False) -> str:
    """The printable record: every section, signatures and the content hash. pdf=True swaps in
    rasters and print-safe styles for the PDF renderer."""
    atts = {a.id: a for a in session.exec(select(Attachment).where(Attachment.experiment_id == exp.id,
                                                                   Attachment.withdrawn_at == None))}  # noqa: E711
    owner = session.get(User, exp.owner_id)
    sigs = signatures_out(session, exp.id)
    body = "".join(_render_entry_safe(e, atts, pdf) for e in live_entries(session, exp.id))
    def stamp(s):
        t = s.get("timestamp")
        if not t:
            return "—"
        if t["status"] == "ok":
            return f"{t['gen_time'][:19].replace('T', ' ')} UTC, {html.escape(t['tsa'] or '')}"
        return "pending" if t["status"] == "pending" else "not obtained"
    sig_html = "".join(
        f"<tr><td>{html.escape(s['meaning'])}</td><td>{html.escape(s['signer_name'] or s['user']['username'])}</td>"
        f"<td>{s['signed_at']}</td><td>{html.escape(s['comment'])}</td><td><code>{s['content_hash'][:16]}…</code></td><td>{stamp(s)}</td></tr>"
        for s in sigs) or "<tr><td colspan=6>Not signed</td></tr>"
    pdf_fix = pdf  # glyphs the PDF's built-in fonts lack
    pdf_css = ("@page{size:a4;margin:1.6cm 1.5cm 1.8cm 1.5cm;@frame footer{-pdf-frame-content:pdffoot;bottom:.6cm;margin-left:1.5cm;margin-right:1.5cm;height:.8cm}}"
               "body{font-family:Helvetica;font-size:10pt;line-height:1.35} h1{font-size:17pt} h2{font-size:12.5pt;margin:.6em 0 .2em}"
               "table{font-size:8.5pt} td,th{padding:2px 4px} section{margin:.8em 0} #pdffoot{font-size:7.5pt;color:#666}") if pdf else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{exp.eid} {html.escape(exp.title)}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:2rem auto;color:#111}}
header{{border-bottom:2px solid #111;margin-bottom:1rem}} h1{{margin:.2rem 0}} .meta{{color:#555}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:12px}} td,th{{border:1px solid #bbb;padding:3px 6px;text-align:left}}
th{{background:#f2f2f2}} section{{margin:1.5rem 0;page-break-inside:avoid}} .rich::after{{content:"";display:block;clear:both}} .rxn svg{{max-width:100%;height:auto}}
code{{font-size:11px}} ul[data-type=taskList]{{list-style:none;padding-left:.2em}} li[data-type=taskItem]::before{{content:'\\2610  '}}
li[data-checked=true]::before{{content:'\\2611  '}} li[data-type=taskItem] div,li[data-type=taskItem] p{{display:inline}} blockquote{{border-left:3px solid #999;margin:.5rem 0;padding:.2rem .8rem;background:#f6f6f6}} mark{{background:#fff3a0}}
.refs li{{margin-bottom:.35rem}} .refs a{{overflow-wrap:anywhere}}
footer{{margin-top:2rem;border-top:1px solid #999;font-size:11px;color:#555}}{pdf_css}</style></head><body>
{f'<div id="pdffoot">{exp.eid} · {html.escape(exp.title[:80])} · {html.escape(exp.status.value)} · page <pdf:pagenumber> of <pdf:pagecount></div>' if pdf else ''}
<header><div class="meta">{exp.eid} · {html.escape(exp.status.value.upper())}</div><h1>{html.escape(exp.title)}</h1>
<div class="meta">Author: {html.escape(owner.full_name or owner.username)} · Created {exp.created_at:%Y-%m-%d %H:%M} UTC · Last modified {exp.updated_at:%Y-%m-%d %H:%M} UTC</div>
<p>{html.escape(exp.description)}</p></header>{body}
<section><h2>Signatures</h2><table><tr><th>Meaning</th><th>Signer</th><th>Signed (UTC)</th><th>Comment</th><th>Content hash</th><th>Trusted timestamp</th></tr>{sig_html}</table></section>
<footer>Record content hash (SHA-256): <code>{content_hash(session, exp)}</code><br>Generated by Benchtop ELN {utcnow():%Y-%m-%d %H:%M} UTC</footer>
</body></html>"""
    if pdf_fix:
        doc = doc.replace(" ★", " (limiting)").replace("⏱ ", "").replace("⏱", "")
    return doc


SPECTRUM_RE = re.compile(r"\.(jdx|dx|jcamp|jcm|csv|tsv|txt|xy|zip)$", re.I)


@router.get("/spectra")
def list_spectra(user: CurrentUser, session: DB, q: str = "", near: int | None = None, limit: int = 60):
    """Attachments that look like spectra, for overlays. `near` (an attachment id) puts its experiment first."""
    home = session.get(Attachment, near).experiment_id if near and session.get(Attachment, near) else None
    from ..access import visible_experiment_filter
    rows = session.exec(visible_experiment_filter(select(Attachment, Experiment).join(Experiment, Experiment.id == Attachment.experiment_id)
                        .order_by(Attachment.created_at.desc()), session, user)).all()
    ql = q.lower().strip()
    out = []
    for a, e in rows:
        if not SPECTRUM_RE.search(a.filename) or (near and a.id == near):
            continue
        if ql and ql not in f"{a.filename} {e.eid} {e.title}".lower():
            continue
        out.append({"id": a.id, "filename": a.filename, "experiment_id": e.id, "eid": e.eid, "title": e.title,
                    "same_experiment": e.id == home, "created_at": a.created_at.isoformat() + "Z"})
    out.sort(key=lambda r: not r["same_experiment"])
    return out[:limit]
