"""Safety data: fetch, store, refresh and interpret GHS classifications.

Every stored record says where it came from and when; records older than
settings.safety_refresh_days are re-fetched by the housekeeping loop. When a refetch
changes the classification, the change is kept, written to the audit log and shown on
the dashboard until someone acknowledges it.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from rdkit import Chem
from sqlmodel import Session, select

from . import audit, pubchem
from .config import settings
from .models import ChemSafety, Material, utcnow

log = logging.getLogger("benchtop.safety")
RETRY_AFTER = timedelta(hours=20)  # after a failed or not-found lookup


def inchikey_of(smiles: str) -> str:
    m = Chem.MolFromSmiles(smiles) if smiles else None
    return (Chem.MolToInchiKey(m) or "") if m else ""


def key_for(*, inchikey: str = "", cas: str = "", name: str = "") -> str:
    if inchikey:
        return inchikey
    if cas:
        return f"cas:{cas.strip()}"
    return f"name:{name.strip().lower()}" if name.strip() else ""


def get_record(session: Session, key: str) -> Optional[ChemSafety]:
    return session.exec(select(ChemSafety).where(ChemSafety.key == key)).first() if key else None


def _status(cs: ChemSafety) -> str:
    """As shown: "pending" (nothing fetched yet) only while PubChem lookups are on. With them off nothing will ever
    arrive, so it's "unavailable" (0.28 showed "Fetching…" for ever, and wouldn't record the hazards)."""
    return "unavailable" if cs.status == "pending" and not pubchem.enabled() else cs.status


def brief(cs: ChemSafety | None) -> dict[str, Any] | None:
    """Compact form for lists: signal word, pictograms, flags."""
    if cs is None or not cs.data:
        return {"status": _status(cs)} if cs else None
    d = cs.data
    return {"status": cs.status, "signal": d.get("signal", ""), "pictograms": [p["code"] for p in d.get("pictograms") or []],
            "flags": [f["label"] for f in d.get("flags") or []],
            "change_unacknowledged": bool(cs.changed_at and (not cs.acknowledged_at or cs.acknowledged_at < cs.changed_at))}


def material_key(m: Material) -> str:
    return key_for(inchikey=m.inchikey, cas=m.cas, name=m.name)


def summary(cs: ChemSafety | None) -> dict[str, Any] | None:
    if cs is None:
        return None
    age = (utcnow() - cs.fetched_at).total_seconds() / 86400 if cs.fetched_at else None
    return {
        "key": cs.key, "status": _status(cs), "error": cs.error, "cid": cs.cid, "title": cs.title, "data": cs.data,
        "fetched_at": cs.fetched_at.isoformat() + "Z" if cs.fetched_at else None,
        "age_days": round(age, 1) if age is not None else None,
        "stale": age is None or age > settings.safety_refresh_days,
        "changed_at": cs.changed_at.isoformat() + "Z" if cs.changed_at else None,
        "change": cs.change,
        "change_unacknowledged": bool(cs.changed_at and (not cs.acknowledged_at or cs.acknowledged_at < cs.changed_at)),
        "refresh_days": settings.safety_refresh_days,
    }


def _fingerprint(data: dict[str, Any] | None) -> dict[str, Any]:
    d = data or {}
    return {"signal": d.get("signal", ""), "codes": sorted({h["code"] for h in d.get("hazards") or []}),
            "pictograms": sorted(p["code"] for p in d.get("pictograms") or [])}


def fetch_into(session: Session, cs: ChemSafety, *, inchikey: str = "", cas: str = "", name: str = "", smiles: str = "") -> ChemSafety:
    """Refresh one record from PubChem in place. Network problems keep the old data and record the error."""
    now = utcnow()
    cs.checked_at = now
    try:
        cid = cs.cid or pubchem.find_cid(inchikey=inchikey, cas=cas, name=name, smiles=smiles)
        if not cid:
            cs.status, cs.error = "not_found", "PubChem has no compound matching this structure, CAS or name"
            session.add(cs)
            return cs
        new = pubchem.ghs(cid)
        cs.cid = cid
        if new is None:
            new = {"cid": cid, "title": "", "not_classified": True, "hazards": [], "pictograms": [], "precautions": [],
                   "other_hazards": [], "signal": "", "flags": [], "sources": [], "source": {"name": "No GHS classification in PubChem"},
                   "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"}
        before, after = _fingerprint(cs.data), _fingerprint(new)
        if cs.data is not None and cs.status in ("ok", "unclassified") and before != after:
            cs.changed_at = now
            cs.change = {"added": sorted(set(after["codes"]) - set(before["codes"])),
                         "removed": sorted(set(before["codes"]) - set(after["codes"])),
                         "signal_before": before["signal"], "signal_after": after["signal"],
                         "previous_fetched_at": cs.fetched_at.isoformat() + "Z" if cs.fetched_at else None}
            audit.record(session, None, "safety_changed", "chemical", cs.id, key=cs.key, title=new.get("title") or cs.title, **cs.change)
        cs.data, cs.title = new, new.get("title") or cs.title
        cs.status = "unclassified" if new.get("not_classified") else "ok"
        cs.error, cs.fetched_at = "", now
    except pubchem.PubChemError as e:
        cs.error = str(e)
        if cs.data is None:
            cs.status = "error"
    session.add(cs)
    return cs


def ensure(session: Session, *, inchikey: str = "", cas: str = "", name: str = "", smiles: str = "",
           force: bool = False, fetch: bool = True) -> Optional[ChemSafety]:
    """The stored record for a chemical, fetching it if missing or stale (unless fetch=False)."""
    if smiles and not inchikey:
        inchikey = inchikey_of(smiles)
    key = key_for(inchikey=inchikey, cas=cas, name=name)
    if not key:
        return None
    cs = session.exec(select(ChemSafety).where(ChemSafety.key == key)).first()
    if cs is None:
        cs = ChemSafety(key=key, title=name)
        session.add(cs)
        session.flush()
    if not fetch or not pubchem.enabled():
        return cs
    now = utcnow()
    fresh = cs.fetched_at and now - cs.fetched_at < timedelta(days=settings.safety_refresh_days)
    backoff = cs.status in ("not_found", "error") and cs.checked_at and now - cs.checked_at < RETRY_AFTER
    if force or not (fresh or backoff):
        fetch_into(session, cs, inchikey=inchikey, cas=cas, name=name, smiles=smiles)
    return cs


def for_material(session: Session, m: Material, *, fetch: bool = False, force: bool = False) -> Optional[ChemSafety]:
    return ensure(session, inchikey=m.inchikey, cas=m.cas, name=m.name, smiles=m.smiles, fetch=fetch, force=force)


def refresh_due(session: Session, limit: int = 40) -> dict[str, int]:
    """Housekeeping: create records for registered materials that lack one, then refresh stale ones."""
    if not pubchem.enabled():
        return {"checked": 0, "changed": 0}
    for m in session.exec(select(Material)).all():
        if material_key(m) and not session.exec(select(ChemSafety).where(ChemSafety.key == material_key(m))).first():
            session.add(ChemSafety(key=material_key(m), title=m.name))
    session.commit()
    now = utcnow()
    cutoff = now - timedelta(days=settings.safety_refresh_days)
    rows = [cs for cs in session.exec(select(ChemSafety)).all()
            if (cs.fetched_at is None or cs.fetched_at < cutoff)
            and not (cs.status in ("not_found", "error") and cs.checked_at and now - cs.checked_at < RETRY_AFTER)]
    rows.sort(key=lambda cs: cs.fetched_at or cs.checked_at or now - timedelta(days=10000))
    mats = {material_key(m): m for m in session.exec(select(Material)).all()}
    checked = changed = 0
    for cs in rows[:limit]:
        m = mats.get(cs.key)
        ik = cs.key if not cs.key.startswith(("cas:", "name:")) else ""
        before = cs.changed_at
        fetch_into(session, cs, inchikey=ik, cas=(m.cas if m else cs.key[4:] if cs.key.startswith("cas:") else ""),
                   name=(m.name if m else cs.key[5:] if cs.key.startswith("name:") else cs.title), smiles=m.smiles if m else "")
        session.commit()
        checked += 1
        changed += int(cs.changed_at != before)
        if cs.error and "reach" in cs.error:
            break  # offline: try again next hour
    return {"checked": checked, "changed": changed}


# ---------------------------------------------------------------- risk assessment for a reaction

AQUEOUS_PROTIC = {inchikey_of(s) for s in ("O", "CO", "CCO", "CC(C)O", "CCCO", "CCCCO", "OO")}
HALOMETHANES = {inchikey_of(s) for s in ("ClCCl", "ClC(Cl)Cl", "BrCBr")}
DIPOLAR_APROTIC = {inchikey_of(s) for s in ("CN(C)C=O", "CS(C)=O", "CC(=O)N(C)C")}
_AZIDE = Chem.MolFromSmarts("[N-]=[N+]=[N]")
_AZIDE2 = Chem.MolFromSmarts("[N]=[N+]=[N-]")


def _is_azide(smiles: str) -> bool:
    m = Chem.MolFromSmiles(smiles) if smiles else None
    return bool(m and (m.HasSubstructMatch(_AZIDE) or m.HasSubstructMatch(_AZIDE2)))


def _is_nah(smiles: str) -> bool:
    return smiles.replace(" ", "") in ("[Na+].[H-]", "[H-].[Na+]", "[NaH]", "[Na][H]", "[H][Na]")


CONTROLS = [  # P-code -> plain-language control
    (("P231", "P232", "P222", "P223"), "Handle under inert gas and protect from moisture"),
    (("P260", "P261", "P271", "P284"), "Work in a fume hood; don't breathe dust, vapour or spray"),
    (("P210", "P240", "P241", "P242", "P243"), "No ignition sources; ground equipment and use non-sparking tools"),
    (("P280",), "Gloves, lab coat and eye protection (face shield where splashes are likely)"),
    (("P201", "P202", "P263"), "Read the special instructions before use; avoid exposure if pregnant"),
    (("P273", "P391"), "Collect all waste; nothing down the drain"),
    (("P220", "P221"), "Keep away from combustible materials"),
    (("P403", "P233", "P235"), "Store cool and well-ventilated, container tightly closed"),
]


def assess(rows: list[dict[str, Any]], records: list[Optional[ChemSafety]]) -> dict[str, Any]:
    """Combine per-chemical GHS data with a few well-known incompatibility rules."""
    chems = []
    for r, cs in zip(rows, records):
        d = cs.data if cs else None
        codes = pubchem.effective_codes(d)
        chems.append({"name": r.get("name") or (d or {}).get("title") or r.get("smiles", ""), "role": r.get("role", ""),
                      "smiles": r.get("smiles", ""), "ik": inchikey_of(r.get("smiles", "")),
                      "codes": codes, "flags": {f["key"] for f in pubchem.flags_for(codes)}, "precautions": (d or {}).get("precautions") or []})
    has = lambda flag: [c["name"] for c in chems if flag in c["flags"]]  # noqa: E731
    warnings: list[dict[str, Any]] = []

    def warn(level: str, text: str, who: list[str]) -> None:
        if who:
            warnings.append({"level": level, "text": text, "chemicals": sorted(set(who))})

    azides = [c["name"] for c in chems if _is_azide(c["smiles"])]
    halo = [c["name"] for c in chems if c["ik"] in HALOMETHANES]
    if azides and halo:
        warn("danger", "Azides with dichloromethane, chloroform or dibromomethane can form highly explosive di- and triazidomethane. Use a different solvent.", azides + halo)
    nah = [c["name"] for c in chems if _is_nah(c["smiles"])]
    dip = [c["name"] for c in chems if c["ik"] in DIPOLAR_APROTIC]
    if nah and dip:
        warn("danger", "Sodium hydride in DMF, DMSO or DMAc can decompose violently, especially on heating or at scale. Keep cold and small, or change solvent.", nah + dip)
    wr = has("water_reactive")
    protic = [c["name"] for c in chems if c["ik"] in AQUEOUS_PROTIC]
    if wr and protic:
        warn("danger", "Water-reactive reagent together with water or an alcohol: expect gas (often hydrogen) to evolve. Add portionwise with cooling, keep the flask vented and away from ignition sources, and quench slowly.", wr + protic)
    elif wr:
        warn("warning", "Water-reactive: dry glassware and solvents, inert atmosphere, and plan a slow, cooled quench.", wr)
    ox, fl = has("oxidiser"), has("flammable")
    if ox and fl:
        warn("danger", "Oxidiser together with flammable materials: fire or explosion risk. Check compatibility, add slowly and control the temperature.", ox + fl)
    warn("danger", "Pyrophoric: transfer under inert gas by syringe or cannula, keep dry sand or a Class D extinguisher at hand, and don't work alone.", has("pyrophoric"))
    warn("danger", "Explosive or self-reactive: smallest practical scale, blast shield, avoid heat, friction and metal spatulas.", has("explosive"))
    warn("danger", "Fatal on exposure: fume hood, know the first-aid measures before you start, and don't work alone.", has("acute_fatal"))
    warn("warning", "Carcinogen, mutagen or reproductive toxin: fume hood, double gloves, minimise the amount, and consider a safer substitute.", has("cmr"))
    warn("warning", "Toxic: fume hood and gloves, and know the first-aid measures before you start.", has("toxic"))
    warn("warning", "Causes organ damage on exposure: fume hood, and keep exposure as low as possible.", has("organ"))
    warn("info", "Suspected carcinogen, mutagen or reproductive toxin: use in a fume hood and keep exposure low.",
         [n for n in has("cmr_suspected") if n not in has("cmr")])
    warn("warning", "Peroxide former: check for peroxides before distilling or evaporating to dryness, and date the bottle when opened.", has("peroxide"))
    warn("warning", "Releases toxic gas with acids: keep away from acidic reagents and work-ups.", has("acid_gas"))
    warn("warning", "Corrosive: face shield for transfers, and have the right spill kit ready.", has("corrosive"))
    warn("info", "Toxic to aquatic life: collect all waste, aqueous layers included.", has("aquatic"))

    pcodes = {p for c in chems for code in c["precautions"] for p in code.split("+")}
    controls = [text for codes, text in CONTROLS if pcodes & set(codes)]
    order = {"danger": 0, "warning": 1, "info": 2}
    warnings.sort(key=lambda w: order[w["level"]])
    return {"warnings": warnings, "controls": controls}
