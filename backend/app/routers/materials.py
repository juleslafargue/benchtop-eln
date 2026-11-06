"""Materials registry and inventory (lots/containers with a usage ledger)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, col, func, or_, select

from .. import audit, chem, paging, safety
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
def list_materials(_: CurrentUser, session: DB, response: Response, q: str = "", kind: str | None = None,
                   limit: int = paging.limit_param(500), offset: int = paging.offset_param()):
    """Registered materials by name. Until 0.31 this stopped at 500 without saying so; X-Total-Count now says how many
    match, and `offset` fetches the rest."""
    stmt = select(Material)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(col(Material.name).ilike(like), col(Material.cas).ilike(like),
                              col(Material.synonyms).ilike(like), col(Material.formula).ilike(like)))
    if kind:
        stmt = stmt.where(Material.kind == kind)
    paging.set_total(response, paging.count(session, stmt))
    stmt = stmt.order_by(Material.name, Material.id).offset(offset).limit(limit)  # id: same-named ones page reliably
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
    allow_duplicate: bool = False  # another lot may already have this barcode (a supplier's code on every bottle of a batch)


# ------------------------------------------------------------------ barcodes (0.32)

# a scanner types the code and then Enter (sometimes a tab or another control character): those never belong to it
_EDGES = re.compile(r"^[\s\x00-\x1f\x7f]+|[\s\x00-\x1f\x7f]+$")


def clean_code(raw: str | None) -> str:
    """A scanned or typed code without the spaces and control characters around it. Inside it nothing changes (a GS1
    code's group separators stay)."""
    return _EDGES.sub("", raw or "")[:200]


def _gs1_flat(code: str) -> str:
    """A GS1 code without its brackets and group separators, or "" if it doesn't look like one."""
    if re.match(r"\(\d{2,4}\)", code) or "\x1d" in code:
        return re.sub(r"[()\x1d]", "", code)
    return ""


def _code_forms(code: str) -> set[str]:
    """The ways the same code can arrive, in lower case: as written; a UPC-A code (12 digits) and the same code read as
    EAN-13 (a 0 in front); a GS1 code as printed, "(01)09506000134352(10)ABC", and as a scanner sends it (no brackets,
    a group separator between the fields)."""
    c = code.lower()
    forms = {c}
    if re.fullmatch(r"\d{12}", c):
        forms.add("0" + c)
    elif re.fullmatch(r"0\d{12}", c):
        forms.add(c[1:])
    flat = _gs1_flat(c)
    if flat:
        forms.add(flat)
    return forms


def lots_with_code(session: Session, code: str, exclude: int | None = None) -> list[Lot]:
    """Every lot whose barcode is this code (in any case; see _code_forms), oldest first."""
    code = clean_code(code)
    if not code:
        return []
    forms = _code_forms(code)
    found = {lot.id: lot for lot in session.exec(select(Lot).where(func.lower(Lot.barcode).in_(forms)))}
    if any(f[:1].isdigit() for f in forms):  # a GS1 code stored the other way: "(01)…" against the scanner's form
        stored = select(Lot).where(or_(col(Lot.barcode).contains("("), col(Lot.barcode).contains("\x1d")))
        for lot in session.exec(stored):
            if _gs1_flat(lot.barcode.lower()) in forms:
                found[lot.id] = lot
    return [found[k] for k in sorted(found) if k != exclude]


def _refuse_duplicate(session: Session, others: list[Lot]) -> None:
    """409 naming the lot (or lots) that already have this barcode."""
    m = session.get(Material, others[0].material_id)
    first = f"{m.mid}-L{others[0].id:04d} ({m.name})" if m else f"Lot {others[0].id}"
    more = len(others) - 1
    who = f"{first} and {more} other lot{'s' if more > 1 else ''} already have" if more else f"{first} already has"
    raise HTTPException(409, f"{who} this barcode.")


@router.post("/materials/{mat_id}/lots", status_code=201)
def create_lot(mat_id: int, body: LotIn, user: Writer, session: DB):
    m = get_or_404(session, Material, mat_id)
    if body.unit not in _UNITS:
        raise HTTPException(400, f"unit must be one of {list(_UNITS)}")
    if not (body.amount > 0 and body.amount < 1e12):
        raise HTTPException(400, "Amount must be a positive number")
    fields = body.model_dump(exclude={"allow_duplicate"})
    fields["barcode"] = clean_code(fields["barcode"])
    others = lots_with_code(session, fields["barcode"])
    if others and not body.allow_duplicate:
        _refuse_duplicate(session, others)
    lot = Lot(material_id=m.id, created_by=user.id, **fields)
    session.add(lot)
    session.flush()
    if not lot.barcode:
        lot.barcode = f"{m.mid}-L{lot.id:04d}"
    session.add(LotTransaction(lot_id=lot.id, delta=lot.amount, balance_after=lot.amount,
                               note="received", user_id=user.id))
    audit.record(session, user, "create", "lot", lot.id, material=m.mid, barcode=lot.barcode,
                 amount=lot.amount, unit=lot.unit, **({"same_barcode_as": [x.id for x in others]} if others else {}))
    session.commit()
    return lot_out(lot)


@router.get("/scan")
def scan(code: str, user: CurrentUser, session: DB):
    """What a scanned or typed code is: every lot with that barcode (none, one or several), each with its material and
    hazard pictograms. When no lot has it: the record it names (EXP-00025, MAT-00007), or the experiment a sample
    code (JL-013) was handed to, if you may open it. Read-only."""
    code = clean_code(code)
    if not code:
        raise HTTPException(422, "Scan or type a code")
    lots = []
    for lot in lots_with_code(session, code):
        m = session.get(Material, lot.material_id)
        if m is None:
            continue
        lots.append({**lot_out(lot), "material_name": m.name, "mid": m.mid, "smiles": m.smiles, "mw": m.mw,
                     "material": {"id": m.id, "mid": m.mid, "name": m.name, "kind": m.kind, "formula": m.formula,
                                  "smiles": m.smiles, "mw": m.mw, "density": m.density, "cas": m.cas},
                     "safety": safety.brief(safety.get_record(session, safety.material_key(m)))})
    record = None
    if not lots:
        from .bench import lookup  # EXP- and MAT- numbers, with the same checks as a link in the text
        from .samples import lookup_sample  # sample codes, as the ⌘K box finds them
        try:
            record = lookup(code, user, session)
        except HTTPException:
            try:
                s = lookup_sample(code, user, session)
                e = s["experiment"]
                record = {"kind": "sample", "code": s["code"], "id": e["id"], "experiment": e["eid"],
                          "title": e["title"], "status": e["status"]}
            except HTTPException:
                record = None
    return {"code": code, "lots": lots, "record": record}


@router.get("/lots")
def list_lots(_: CurrentUser, session: DB, response: Response, q: str = "", location: str = "", status: str | None = None,
              limit: int | None = paging.limit_param(None), offset: int = paging.offset_param()):
    """Containers by material name; every match unless `limit` is given."""
    stmt = select(Lot, Material).join(Material, Material.id == Lot.material_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(col(Lot.barcode).ilike(like), col(Material.name).ilike(like),
                              col(Material.cas).ilike(like)))
    if location:
        stmt = stmt.where(col(Lot.location).ilike(f"%{location}%"))
    if status:
        stmt = stmt.where(Lot.status == status)
    paging.set_total(response, paging.count(session, stmt))
    stmt = stmt.order_by(Material.name, Lot.id).offset(offset).limit(limit)
    return [{**lot_out(lot), "material_name": m.name, "mid": m.mid, "smiles": m.smiles, "mw": m.mw}
            for lot, m in session.exec(stmt)]


class LotPatch(BaseModel):
    location: str | None = None
    status: str | None = None
    purity: float | None = None
    supplier: str | None = None
    expires_at: datetime | None = None
    barcode: str | None = None
    allow_duplicate: bool = False


@router.patch("/lots/{lot_id}")
def update_lot(lot_id: int, body: LotPatch, user: Writer, session: DB):
    lot = get_or_404(session, Lot, lot_id)
    changes = cleaned(lot, body.model_dump(exclude_unset=True, exclude={"allow_duplicate"}))
    others: list[Lot] = []
    if "barcode" in changes:
        changes["barcode"] = clean_code(changes["barcode"])
        if not changes["barcode"]:  # cleared: back to the one Benchtop makes, as for a new lot
            m = get_or_404(session, Material, lot.material_id)
            changes["barcode"] = f"{m.mid}-L{lot.id:04d}"
        if changes["barcode"] == lot.barcode:
            del changes["barcode"]
        else:
            others = lots_with_code(session, changes["barcode"], exclude=lot.id)
            if others and not body.allow_duplicate:
                _refuse_duplicate(session, others)
    for k, v in changes.items():
        setattr(lot, k, v)
    session.add(lot)
    audit.record(session, user, "update", "lot", lot.id, changes=changes,
                 **({"same_barcode_as": [x.id for x in others]} if others else {}))
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
def lot_history(lot_id: int, user: CurrentUser, session: DB, response: Response,
                limit: int | None = paging.limit_param(None), offset: int = paging.offset_param()):
    """A lot's ledger, newest first; every entry unless `limit` is given."""
    from ..access import access_for
    acc = access_for(session, user)
    get_or_404(session, Lot, lot_id)
    stmt = select(LotTransaction).where(LotTransaction.lot_id == lot_id)
    paging.set_total(response, paging.count(session, stmt))
    txs = session.exec(stmt.order_by(LotTransaction.id.desc()).offset(offset).limit(limit))
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
