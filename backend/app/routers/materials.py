"""Materials registry and inventory (lots/containers with a usage ledger)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, col, or_, select

from .. import audit, chem, safety
from ..db import engine
from ..models import Entry, EntryKind, Experiment, Lot, LotTransaction, Material
from ..security import DB, CurrentUser, Writer
from ..patching import cleaned
from ..services import read_experiment, get_or_404, user_brief

router = APIRouter(prefix="/api", tags=["materials & inventory"])

# Unit conversion into grams (mass) or millilitres (volume)
_UNITS = {"mg": ("mass", 1e-3), "g": ("mass", 1.0), "kg": ("mass", 1e3),
          "uL": ("vol", 1e-3), "mL": ("vol", 1.0), "L": ("vol", 1e3)}


def convert(amount: float, from_unit: str, to_unit: str, density: float | None = None) -> float:
    fk, ff = _UNITS[from_unit]
    tk, tf = _UNITS[to_unit]
    base = amount * ff
    if fk != tk:
        if not density:
            raise HTTPException(400, f"Need a density to convert {from_unit} to {to_unit}")
        base = base * density if fk == "vol" else base / density
    return base / tf


def lot_out(lot: Lot) -> dict[str, Any]:
    return {k: (v.isoformat() + "Z" if isinstance(v, datetime) else v) for k, v in lot.model_dump().items()}


def material_out(session, m: Material, with_lots: bool = False, user=None) -> dict[str, Any]:
    d = {k: (v.isoformat() + "Z" if isinstance(v, datetime) else v) for k, v in m.model_dump().items()}
    d["mid"] = m.mid
    lots = session.exec(select(Lot).where(Lot.material_id == m.id).order_by(Lot.id)).all()
    d["lot_count"] = len(lots)
    d["available_lots"] = sum(1 for lot in lots if lot.status == "available")
    rec = safety.get_record(session, safety.material_key(m))
    d["safety"] = safety.summary(rec) if with_lots else safety.brief(rec)
    if with_lots:
        d["lots"] = [lot_out(lot) for lot in lots]
        d["created_by_user"] = user_brief(session, m.created_by)
        if m.source_experiment_id:
            src = session.get(Experiment, m.source_experiment_id)
            from ..access import access_for
            if src and user is not None and not access_for(session, user).can_read(src):
                d["source_experiment"] = {"id": None, "eid": src.eid, "title": ""}  # the ID only: the record itself is private
            else:
                d["source_experiment"] = {"id": src.id, "eid": src.eid, "title": src.title} if src else None
    return d


class MaterialIn(BaseModel):
    name: str
    kind: str = "reagent"
    smiles: str = ""
    cas: str = ""
    density: float | None = None
    synonyms: str = ""
    hazards: str = ""
    notes: str = ""
    mw: float | None = None  # only used if no structure
    pubchem_cid: int | None = None


def _apply_structure(m: Material) -> None:
    if m.smiles:
        try:
            p = chem.properties(m.smiles)
        except chem.ChemError as exc:
            raise HTTPException(422, f"Structure: {exc}")
        m.smiles, m.formula, m.mw, m.inchikey = p["smiles"], p["formula"], p["mw"], p["inchikey"]


@router.get("/materials")
def list_materials(_: CurrentUser, session: DB, q: str = "", kind: str | None = None, limit: int = 500):
    stmt = select(Material).order_by(Material.name).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(col(Material.name).ilike(like), col(Material.cas).ilike(like),
                              col(Material.synonyms).ilike(like), col(Material.formula).ilike(like)))
    if kind:
        stmt = stmt.where(Material.kind == kind)
    return [material_out(session, m) for m in session.exec(stmt)]


def _fetch_safety_later(mat_id: int) -> None:
    with Session(engine, expire_on_commit=False) as s:
        m = s.get(Material, mat_id)
        if m:
            safety.for_material(s, m, fetch=True)
            s.commit()


@router.post("/materials", status_code=201)
def create_material(body: MaterialIn, user: Writer, session: DB, background: BackgroundTasks):
    m = Material(**body.model_dump(), created_by=user.id)
    _apply_structure(m)
    if m.inchikey:
        dup = session.exec(select(Material).where(Material.inchikey == m.inchikey)).first()
        if dup:
            raise HTTPException(409, f"Same structure already registered as {dup.mid} ({dup.name})")
    session.add(m)
    session.flush()
    audit.record(session, user, "create", "material", m.id, mid=m.mid, name=m.name, inchikey=m.inchikey)
    session.commit()
    background.add_task(_fetch_safety_later, m.id)
    return material_out(session, m, with_lots=True, user=user)


@router.post("/materials/{mat_id}/safety/refresh")
def refresh_material_safety(mat_id: int, _: CurrentUser, session: DB):
    m = get_or_404(session, Material, mat_id)
    if not safety.pubchem.enabled():
        raise HTTPException(503, "PubChem lookups are turned off on this server")
    cs = safety.for_material(session, m, fetch=True, force=True)
    session.commit()
    if cs and cs.error and cs.status == "error":
        raise HTTPException(502, cs.error)
    return safety.summary(cs)


@router.post("/materials/{mat_id}/safety/acknowledge")
def acknowledge_material_safety(mat_id: int, user: CurrentUser, session: DB):
    m = get_or_404(session, Material, mat_id)
    cs = safety.get_record(session, safety.material_key(m))
    if cs is None:
        raise HTTPException(404, "No safety data for this material yet")
    cs.acknowledged_at = safety.utcnow()
    session.add(cs)
    audit.record(session, user, "safety_acknowledged", "chemical", cs.id, key=cs.key, material=m.mid, change=cs.change)
    session.commit()
    return safety.summary(cs)


@router.get("/materials/{mat_id}")
def get_material(mat_id: int, user: CurrentUser, session: DB):
    from ..access import access_for
    acc = access_for(session, user)
    m = get_or_404(session, Material, mat_id)
    out = material_out(session, m, with_lots=True, user=user)
    # experiments whose reaction tables reference this material
    used, hidden = [], set()
    for e in session.exec(select(Entry).where(Entry.kind == EntryKind.reaction, Entry.deleted == False)):  # noqa: E712
        rows = (e.content or {}).get("rows")
        if isinstance(rows, list) and any(isinstance(r, dict) and r.get("material_id") == m.id for r in rows):
            exp = session.get(Experiment, e.experiment_id)
            if exp and not acc.can_read(exp):
                hidden.add(exp.id)  # counted, so "Used in" doesn't say it's in none when the lot's ledger lists them
            elif exp and exp.id not in {u["id"] for u in used}:
                used.append({"id": exp.id, "eid": exp.eid, "title": exp.title, "status": exp.status.value})
    out["used_in"] = used
    out["used_in_hidden"] = len(hidden)
    return out


class MaterialPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    smiles: str | None = None
    cas: str | None = None
    density: float | None = None
    synonyms: str | None = None
    hazards: str | None = None
    notes: str | None = None
    mw: float | None = None


@router.patch("/materials/{mat_id}")
def update_material(mat_id: int, body: MaterialPatch, user: Writer, session: DB):
    m = get_or_404(session, Material, mat_id)
    changes = cleaned(m, body.model_dump(exclude_unset=True))
    for k, v in changes.items():
        setattr(m, k, v)
    if "smiles" in changes:
        _apply_structure(m)
    session.add(m)
    audit.record(session, user, "update", "material", m.id, changes=changes)
    session.commit()
    return material_out(session, m, with_lots=True, user=user)


# ------------------------------------------------------------------ lots


class LotIn(BaseModel):
    barcode: str = ""
    supplier: str = ""
    catalog_number: str = ""
    purity: float | None = None
    amount: float
    unit: str = "g"
    location: str = ""
    expires_at: datetime | None = None


@router.post("/materials/{mat_id}/lots", status_code=201)
def create_lot(mat_id: int, body: LotIn, user: Writer, session: DB):
    m = get_or_404(session, Material, mat_id)
    if body.unit not in _UNITS:
        raise HTTPException(400, f"unit must be one of {list(_UNITS)}")
    if not (body.amount > 0 and body.amount < 1e12):
        raise HTTPException(400, "Amount must be a positive number")
    lot = Lot(material_id=m.id, created_by=user.id, **body.model_dump())
    session.add(lot)
    session.flush()
    if not lot.barcode:
        lot.barcode = f"{m.mid}-L{lot.id:04d}"
    session.add(LotTransaction(lot_id=lot.id, delta=lot.amount, balance_after=lot.amount,
                               note="received", user_id=user.id))
    audit.record(session, user, "create", "lot", lot.id, material=m.mid, barcode=lot.barcode,
                 amount=lot.amount, unit=lot.unit)
    session.commit()
    return lot_out(lot)


@router.get("/lots")
def list_lots(_: CurrentUser, session: DB, q: str = "", location: str = "", status: str | None = None):
    stmt = select(Lot, Material).join(Material, Material.id == Lot.material_id).order_by(Material.name, Lot.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(col(Lot.barcode).ilike(like), col(Material.name).ilike(like),
                              col(Material.cas).ilike(like)))
    if location:
        stmt = stmt.where(col(Lot.location).ilike(f"%{location}%"))
    if status:
        stmt = stmt.where(Lot.status == status)
    return [{**lot_out(lot), "material_name": m.name, "mid": m.mid, "smiles": m.smiles, "mw": m.mw}
            for lot, m in session.exec(stmt)]


class LotPatch(BaseModel):
    location: str | None = None
    status: str | None = None
    purity: float | None = None
    supplier: str | None = None
    expires_at: datetime | None = None


@router.patch("/lots/{lot_id}")
def update_lot(lot_id: int, body: LotPatch, user: Writer, session: DB):
    lot = get_or_404(session, Lot, lot_id)
    changes = cleaned(lot, body.model_dump(exclude_unset=True))
    for k, v in changes.items():
        setattr(lot, k, v)
    session.add(lot)
    audit.record(session, user, "update", "lot", lot.id, changes=changes)
    session.commit()
    return lot_out(lot)


class TxIn(BaseModel):
    amount: float  # positive number; sign given by `direction`
    unit: str
    direction: str = "out"  # out = consume, in = top up / correction
    experiment_id: int | None = None
    note: str = ""


@router.post("/lots/{lot_id}/transactions", status_code=201)
def lot_transaction(lot_id: int, body: TxIn, user: Writer, session: DB):
    lot = get_or_404(session, Lot, lot_id)
    m = get_or_404(session, Material, lot.material_id)
    if body.unit not in _UNITS:
        raise HTTPException(400, "unknown unit")
    if body.experiment_id is not None:  # 0 is an ID too (one that never exists), not "no experiment"
        read_experiment(session, user, body.experiment_id)
    if body.direction not in ("out", "in"):
        raise HTTPException(400, "direction must be 'out' or 'in'")
    if not (body.amount > 0 and body.amount < 1e12):
        raise HTTPException(400, "Amount must be a positive number")
    qty = convert(body.amount, body.unit, lot.unit, m.density)
    delta = -qty if body.direction == "out" else qty
    if lot.amount + delta < -1e-9:
        raise HTTPException(409, f"Only {lot.amount:g} {lot.unit} left in {lot.barcode}")
    lot.amount = round(lot.amount + delta, 6)
    if lot.amount <= 1e-9:
        lot.status = "empty"
    tx = LotTransaction(lot_id=lot.id, delta=round(delta, 6), balance_after=lot.amount,
                        experiment_id=body.experiment_id, note=body.note, user_id=user.id)
    session.add_all([lot, tx])
    session.flush()
    audit.record(session, user, "inventory", "lot", lot.id, barcode=lot.barcode, delta=tx.delta,
                 unit=lot.unit, experiment_id=body.experiment_id)
    session.commit()
    return {"lot": lot_out(lot), "transaction_id": tx.id}


@router.post("/lots/transactions/{tx_id}/reverse", status_code=201)
def reverse_transaction(tx_id: int, user: Writer, session: DB):
    """Undo a usage entry by posting the opposite amount. The original stays in the ledger."""
    tx = get_or_404(session, LotTransaction, tx_id)
    marker = f"Reversal of #{tx.id}"
    if session.exec(select(LotTransaction).where(LotTransaction.note.startswith(marker))).first():
        raise HTTPException(409, "This entry has already been reversed")
    if tx.note.startswith("Reversal of #"):
        raise HTTPException(409, "A reversal can't itself be reversed; record a new adjustment instead")
    lot = get_or_404(session, Lot, tx.lot_id)
    if lot.amount - tx.delta < -1e-9:
        raise HTTPException(409, f"Only {lot.amount:g} {lot.unit} left in {lot.barcode}")
    lot.amount = round(lot.amount - tx.delta, 6)
    if lot.amount > 1e-9 and lot.status == "empty":
        lot.status = "available"
    rev = LotTransaction(lot_id=lot.id, delta=round(-tx.delta, 6), balance_after=lot.amount,
                         experiment_id=tx.experiment_id, note=f"{marker} ({tx.note})"[:200], user_id=user.id)
    session.add_all([lot, rev])
    session.flush()
    audit.record(session, user, "inventory_reverse", "lot", lot.id, barcode=lot.barcode, reversed_tx=tx.id,
                 delta=rev.delta, unit=lot.unit)
    session.commit()
    return {"lot": lot_out(lot), "transaction_id": rev.id}


@router.get("/lots/{lot_id}/transactions")
def lot_history(lot_id: int, user: CurrentUser, session: DB):
    from ..access import access_for
    acc = access_for(session, user)
    get_or_404(session, Lot, lot_id)
    txs = session.exec(select(LotTransaction).where(LotTransaction.lot_id == lot_id).order_by(LotTransaction.id.desc()))
    out = []
    for t in txs:
        exp = session.get(Experiment, t.experiment_id) if t.experiment_id else None
        # a record this person can't open: its number only, as for a lot's source experiment (no link to a "not found")
        out.append({"id": t.id, "delta": t.delta, "balance_after": t.balance_after, "note": t.note,
                    "created_at": t.created_at.isoformat() + "Z", "user": user_brief(session, t.user_id),
                    "experiment": {"id": exp.id if acc.can_read(exp) else None, "eid": exp.eid} if exp else None})
    return out


# ------------------------------------------------------------------ register a product


class RegisterProductIn(BaseModel):
    experiment_id: int
    name: str
    smiles: str = ""
    amount_mg: float | None = None
    purity: float | None = None
    location: str = ""


@router.post("/materials/register-product", status_code=201)
def register_product(body: RegisterProductIn, user: Writer, session: DB):
    exp = read_experiment(session, user, body.experiment_id)
    m = None
    if body.smiles:
        try:
            ik = chem.properties(body.smiles)["inchikey"]
        except chem.ChemError as exc:
            raise HTTPException(422, f"Structure: {exc}")
        m = session.exec(select(Material).where(Material.inchikey == ik)).first()
    if m is None:
        m = Material(name=body.name, kind="product", smiles=body.smiles, created_by=user.id,
                     source_experiment_id=exp.id, notes=f"Registered from {exp.eid}")
        _apply_structure(m)
        session.add(m)
        session.flush()
        audit.record(session, user, "create", "material", m.id, mid=m.mid, source=exp.eid)
    lot = None
    if body.amount_mg:
        lot = Lot(material_id=m.id, created_by=user.id, amount=body.amount_mg, unit="mg",
                  purity=body.purity, location=body.location, supplier=f"in-house ({exp.eid})", barcode="")
        session.add(lot)
        session.flush()
        lot.barcode = f"{m.mid}-L{lot.id:04d}"
        session.add(LotTransaction(lot_id=lot.id, delta=lot.amount, balance_after=lot.amount,
                                   experiment_id=exp.id, note=f"produced in {exp.eid}", user_id=user.id))
        audit.record(session, user, "create", "lot", lot.id, material=m.mid, source=exp.eid,
                     amount=lot.amount, unit="mg")
    session.commit()
    return {"material": material_out(session, m), "lot": lot_out(lot) if lot else None}
