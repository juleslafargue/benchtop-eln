"""Compound lookup (PubChem) and per-experiment safety assessment."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import func, select

from .. import chem, pubchem, safety
from ..config import settings
from ..models import ChemSafety, EntryKind, Experiment, Material, Role
from ..security import DB, Admin, CurrentUser
from ..services import get_or_404, live_entries

router = APIRouter(prefix="/api", tags=["safety"])
MAX_FETCH_PER_REQUEST = 15


@router.get("/lookup")
def lookup(q: str, user: CurrentUser, session: DB):
    """Find a compound by name, CAS number or SMILES and return its identity plus GHS data."""
    q = q.strip()
    if not q:
        raise HTTPException(400, "Type a name, CAS number or SMILES")
    if not pubchem.enabled():  # said plainly; an admin, who can turn it on, also gets the setting's name
        raise HTTPException(503, "Looking chemicals up on PubChem is turned off on this server. Pick the chemical from the "
                                 "registry, or draw its structure, instead" + (" (BENCHTOP_PUBCHEM=off)." if user.role == Role.admin else "."))
    smiles = ""
    if not pubchem.valid_cas(q) and any(ch in q for ch in "=#()[]@/\\") :
        try:
            smiles = chem.properties(q)["smiles"]
        except chem.ChemError:
            pass
    try:
        cid = pubchem.find_cid(cas=q if pubchem.valid_cas(q) else "", name="" if smiles else q, smiles=smiles)
        if not cid:
            raise HTTPException(404, f"PubChem has nothing matching “{q}”")
        info = pubchem.compound(cid)
    except pubchem.PubChemError as e:
        raise HTTPException(502, str(e))
    cs = safety.ensure(session, inchikey=info["inchikey"], cas=info["cas"], name=info["name"], smiles=info["smiles"])
    if cs and not cs.cid:
        cs.cid = cid
    session.commit()
    reg = session.exec(select(Material).where(Material.inchikey == info["inchikey"])).first() if info["inchikey"] else None
    if info["smiles"]:  # RDKit's MW/formula match the rest of the app (salts, hydrates)
        try:
            p = chem.properties(info["smiles"])
            info.update({"smiles": p["smiles"], "mw": p["mw"], "formula": p["formula"]})
        except chem.ChemError:
            pass
    return {**info, "safety": safety.summary(cs), "registered": {"id": reg.id, "mid": reg.mid, "name": reg.name} if reg else None}


def _reaction_rows(session: DB, exp: Experiment) -> list[dict[str, Any]]:
    rows = []
    for e in live_entries(session, exp.id):
        if e.kind == EntryKind.reaction:
            rows.extend(r for r in (e.content or {}).get("rows") or [] if r.get("smiles") or r.get("name"))
    return rows


@router.get("/experiments/{exp_id}/safety")
def experiment_safety(exp_id: int, user: CurrentUser, session: DB, refresh: bool = False):
    """Hazards of everything in the experiment's reaction tables, plus combination warnings.
    Missing or stale data is fetched (a limited number per request so the page stays responsive)."""
    from ..services import read_experiment
    exp = read_experiment(session, user, exp_id)
    rows = _reaction_rows(session, exp)
    mats = {m.id: m for m in session.exec(select(Material).where(Material.id.in_([r.get("material_id") for r in rows if r.get("material_id")] or [-1]))).all()}
    fetched, records, out = 0, [], []
    for r in rows:
        m = mats.get(r.get("material_id"))
        kw = dict(inchikey=m.inchikey, cas=m.cas, name=m.name, smiles=m.smiles) if m else dict(smiles=r.get("smiles", ""), name=r.get("name", ""))
        cs = safety.ensure(session, **kw, fetch=False)
        need = cs is not None and (refresh or cs.status == "pending" or safety.summary(cs)["stale"])
        if need and fetched < MAX_FETCH_PER_REQUEST and pubchem.enabled():
            cs = safety.ensure(session, **kw, force=refresh)
            fetched += 1
        records.append(cs)
        out.append({"name": (r.get("name") or "").strip() or (cs.title if cs and cs.title else "") or chem.row_label(r, rows),
                    "role": r.get("role", ""), "smiles": r.get("smiles", ""),
                    "material_id": m.id if m else None, "safety": safety.summary(cs)})
    session.commit()
    a = safety.assess(rows, records)
    pictos: list[str] = []
    for c in out:
        for p in ((c["safety"] or {}).get("data") or {}).get("pictograms") or []:
            if p["code"] not in pictos:
                pictos.append(p["code"])
    return {"chemicals": out, **a, "pictograms": sorted(pictos), "pubchem_enabled": pubchem.enabled(),
            "pending": sum(1 for c in out if (c["safety"] or {}).get("status") in (None, "pending")),
            "refresh_days": settings.safety_refresh_days}


@router.get("/safety/status")
def safety_status(_: CurrentUser, session: DB):
    total = session.exec(select(func.count()).select_from(ChemSafety)).one()
    rows = session.exec(select(ChemSafety)).all()
    stale = sum(1 for cs in rows if safety.summary(cs)["stale"])
    last = max((cs.fetched_at for cs in rows if cs.fetched_at), default=None)
    return {"enabled": pubchem.enabled(), "records": total, "stale": stale,
            "errors": sum(1 for cs in rows if cs.status == "error"),
            "changed_unacknowledged": sum(1 for cs in rows if safety.summary(cs)["change_unacknowledged"]),
            "last_fetch": last.isoformat() + "Z" if last else None, "refresh_days": settings.safety_refresh_days}


@router.post("/safety/refresh")
def refresh_all(_: Admin, session: DB):
    """Admin: refresh everything that's due now instead of waiting for the hourly check."""
    if not pubchem.enabled():
        raise HTTPException(503, "PubChem lookups are turned off on this server")
    return safety.refresh_due(session, limit=200)
