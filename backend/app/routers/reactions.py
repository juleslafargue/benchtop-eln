"""Cross-experiment view of every reaction: conditions, scale, outcome. Filterable, exportable."""
from __future__ import annotations

import csv
import io
from typing import Any

import html
import re

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import select

from .. import chem, paging
from ..models import Entry, EntryKind, Experiment
from ..security import DB, CurrentUser
from ..services import user_brief

import logging
log = logging.getLogger("benchtop.reactions")
router = APIRouter(prefix="/api", tags=["reactions"])


def _collect(session, user) -> list[dict[str, Any]]:
    from ..access import visible_experiment_filter
    exps = {e.id: e for e in session.exec(visible_experiment_filter(select(Experiment).where(Experiment.is_template == False), session, user))}  # noqa: E712
    kinds = [EntryKind.reaction, EntryKind.purification, EntryKind.characterization]  # skip text, tables, etc.
    entries = session.exec(select(Entry).where(Entry.deleted == False, Entry.kind.in_(kinds))  # noqa: E712
                           .order_by(Entry.experiment_id, Entry.position))
    by_exp: dict[int, list[Entry]] = {}
    for en in entries:
        if en.experiment_id in exps:
            by_exp.setdefault(en.experiment_id, []).append(en)
    out = []
    for exp_id, ens in by_exp.items():
        try:
            out.extend(_rows_for(session, exps[exp_id], ens))
        except Exception:  # an odd record shouldn't empty the reactions page for everyone
            log.exception("Skipping reactions of experiment %s", exp_id)
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def _rows_for(session, exp: Experiment, ens: list[Entry]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    purif = next((e.content for e in ens if e.kind == EntryKind.purification), None) or {}
    chars = [it.get("technique") for e in ens if e.kind == EntryKind.characterization
             for it in (e.content or {}).get("items", []) if it.get("data")]
    for en in ens:
        if en.kind != EntryKind.reaction:
            continue
        c = en.content or {}
        rows = c.get("rows") or []
        if not rows and not c.get("reaction_smiles"):
            continue
        products = [r for r in rows if r.get("role") == "product"]
        lim = next((r for r in rows if r.get("limiting")), None)
        best = max((p for p in products if p.get("yield_pct") is not None), key=lambda p: p["yield_pct"], default=None)
        cond = c.get("conditions") or {}
        out.append({
            "experiment_id": exp.id, "eid": exp.eid, "title": exp.title, "status": exp.status.value,
            "owner": user_brief(session, exp.owner_id), "tags": exp.tags or [],
            "date": exp.created_at.isoformat() + "Z", "entry_title": en.title,
            "reaction_smiles": c.get("reaction_smiles", ""),
            "temperature_c": cond.get("temperature_c"), "temperature_label": cond.get("temperature_label"),
            "time_h": cond.get("time_h"), "atmosphere": cond.get("atmosphere"),
            "concentration_m": (c.get("totals") or {}).get("concentration_m"),
            "limiting": lim.get("name") or lim.get("formula") if lim else None,
            "scale_mmol": (c.get("totals") or {}).get("limiting_mmol"),
            "reagents": [r.get("name") or r.get("formula") for r in rows if r.get("role") in ("reagent", "catalyst")],
            "solvents": [r.get("name") or r.get("formula") for r in rows if r.get("role") == "solvent"],
            "product": (best or (products[0] if products else {})).get("name") or (best or {}).get("formula"),
            "yield_pct": best["yield_pct"] if best else None,
            "purification": purif.get("method") if purif.get("isolated_mg") is not None or purif.get("eluent") else None,
            "characterised": chars,
            "smiles_all": [r.get("smiles") for r in rows if r.get("smiles")],
            "metrics": chem.green_metrics([chem.new_row(**r) for r in rows]) if rows else {},
            "products_detail": [{"name": p.get("name") or p.get("formula"), "smiles": p.get("smiles"), "yield_pct": p.get("yield_pct"),
                                 "actual_mass_mg": p.get("actual_mass_mg")} for p in products if p.get("smiles")],
        })
    return out


def _filter(rows, q, min_yield, max_yield, min_temp, max_temp, atmosphere, substructure, status):
    match = None
    if substructure:
        try:
            match = chem.substructure_matcher(substructure)
        except chem.ChemError as exc:
            raise HTTPException(422, str(exc))
    ql = q.lower().strip()
    res = []
    for r in rows:
        if ql:
            hay = " ".join(str(x) for x in (r["title"], r["eid"], r["limiting"], r["product"], " ".join(r["reagents"]),
                                              " ".join(r["solvents"]), " ".join(r["tags"]), r["temperature_label"])).lower()
            if ql not in hay:
                continue
        y, t = r["yield_pct"], r["temperature_c"]
        if min_yield is not None and (y is None or y < min_yield):
            continue
        if max_yield is not None and (y is None or y > max_yield):
            continue
        if min_temp is not None and (t is None or t < min_temp):
            continue
        if max_temp is not None and (t is None or t > max_temp):
            continue
        if atmosphere and (r["atmosphere"] or "").lower() != atmosphere.lower():
            continue
        if status and r["status"] != status:
            continue
        if match and not any(match(s) for s in r["smiles_all"]):
            continue
        res.append(r)
    return res


@router.get("/reactions")
def list_reactions(user: CurrentUser, session: DB, q: str = "", min_yield: float | None = None,
                   max_yield: float | None = None, min_temp: float | None = None, max_temp: float | None = None,
                   atmosphere: str = "", substructure: str = "", status: str = "", format: str = "json"):
    rows = _filter(_collect(session, user), q, min_yield, max_yield, min_temp, max_temp, atmosphere, substructure, status)
    if format != "csv":
        return rows
    buf = io.StringIO()
    cols = ["eid", "title", "status", "date", "limiting", "scale_mmol", "reagents", "solvents", "temperature_c",
            "temperature_label", "time_h", "atmosphere", "concentration_m", "product", "yield_pct", "purification",
            "characterised", "reaction_smiles"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["; ".join(r[c]) if isinstance(r[c], list) else ("" if r[c] is None else r[c]) for c in cols])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="reactions.csv"'})


@router.get("/compounds")
def compounds(user: CurrentUser, session: DB, response: Response,
              limit: int | None = paging.limit_param(None), offset: int = paging.offset_param()):
    """Every product made (from reaction tables) and every registered product/sample, one row per
    structure, with medicinal-chemistry descriptors. Every one unless `limit` is given."""
    from ..models import Material

    seen: dict[str, dict[str, Any]] = {}

    def add(smiles: str, name: str, **extra: Any) -> dict[str, Any] | None:
        try:
            p = chem.properties(smiles)
        except chem.ChemError:
            return None
        key = p["inchikey"] or p["smiles"]
        row = seen.get(key)
        if row is None:
            row = seen[key] = {"name": name, "smiles": p["smiles"], "formula": p["formula"], "mw": p["mw"],
                               "logp": p["logp"], "tpsa": p["tpsa"], "hbd": p["hbd"], "hba": p["hba"],
                               "rotatable_bonds": p["rotatable_bonds"], "heavy_atoms": p["heavy_atoms"],
                               "aromatic_rings": p["aromatic_rings"], "fsp3": p["fsp3"], "qed": p["qed"],
                               "lipinski_violations": p["lipinski_violations"], "veber_violations": p["veber_violations"],
                               "experiments": [], "best_yield": None, "material": None}
        if not row["name"] and name:
            row["name"] = name
        return row

    for r in _collect(session, user):
        for pd in r["products_detail"]:
            row = add(pd["smiles"], pd["name"] or "")
            if row is None:
                continue
            if r["experiment_id"] not in [e["id"] for e in row["experiments"]]:
                row["experiments"].append({"id": r["experiment_id"], "eid": r["eid"], "title": r["title"], "status": r["status"],
                                           "date": r["date"], "yield_pct": pd["yield_pct"]})
            if pd["yield_pct"] is not None:
                row["best_yield"] = max(row["best_yield"] or 0, pd["yield_pct"])
    for m in session.exec(select(Material).where(Material.kind.in_(["product", "sample"]))).all():
        if m.smiles:
            row = add(m.smiles, m.name)
            if row is not None:
                row["material"] = {"id": m.id, "mid": m.mid}
    for row in seen.values():
        row["experiments"].sort(key=lambda e: e["date"] or "", reverse=True)  # newest first
    rows = sorted(seen.values(), key=lambda r: r["name"].lower())
    paging.set_total(response, len(rows))
    return paging.window(rows, limit, offset)


# ---------------------------------------------------------------- "have we done this before?"

class SimilarIn(BaseModel):
    rows: list[dict[str, Any]] = []
    experiment_id: int | None = None
    limit: int = 5


_TAG = re.compile(r"<[^>]+>")
_NOTE_TITLES = re.compile(r"observ|result|conclu|outcome|comment|note|discussion|summary", re.I)


def _plain(h: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", h or ""))).strip()


def _notes(ens: list[Entry], rxn: Entry) -> str:
    """The most useful free text about how it went: results/observations sections first, then
    purification and reaction notes. Trimmed to a sentence or two."""
    texts = []
    for e in ens:
        if e.kind == EntryKind.text and _NOTE_TITLES.search(e.title or ""):
            texts.append(_plain((e.content or {}).get("html", "")))
    pur = next((e.content for e in ens if e.kind == EntryKind.purification), None) or {}
    texts += [_plain(pur.get("notes_html", "")), ((rxn.content or {}).get("conditions") or {}).get("notes") or ""]
    t = next((x for x in texts if len(x) > 3), "")
    return t if len(t) <= 220 else t[:217].rsplit(" ", 1)[0] + "…"


def _condition_row(r: dict[str, Any]) -> dict[str, Any]:
    """A reagent/catalyst/solvent row ready to drop into another reaction at a different scale:
    amounts typed as mass or mmol become the equivalents they worked out to."""
    keep = ("role", "name", "smiles", "formula", "mw", "material_id", "density", "purity", "conc_m", "notes")
    out = {k: r.get(k) for k in keep if r.get(k) not in (None, "")}
    basis = r.get("basis") or "equiv"
    if r.get("role") == "solvent":
        out["basis"] = basis if basis in ("fill", "volume") else "volume"
        if out["basis"] == "volume" and r.get("volume_ml") is not None:
            out["volume_ml"] = r["volume_ml"]
    elif r.get("equiv") is not None:
        out.update(basis="equiv", equiv=r["equiv"])
    return out


@router.post("/reactions/similar")
def similar_reactions(body: SimilarIn, user: CurrentUser, session: DB):
    """Past reactions most like the one being planned, best first, with how they turned out."""
    q = chem.reaction_profile(body.rows)
    if not q["sub"] and not q["prod"]:
        return {"results": [], "searched": 0, "reason": "Add a structure for a starting material or product to look for similar reactions."}
    from ..access import visible_experiment_filter
    stmt = select(Experiment).where(Experiment.is_template == False, Experiment.status != "voided")  # noqa: E712
    stmt = visible_experiment_filter(stmt, session, user)
    exps = {e.id: e for e in session.exec(stmt) if e.id != body.experiment_id}
    by_exp: dict[int, list[Entry]] = {}
    if exps:
        for en in session.exec(select(Entry).where(Entry.deleted == False, Entry.experiment_id.in_(list(exps))).order_by(Entry.position)):  # noqa: E712
            by_exp.setdefault(en.experiment_id, []).append(en)
    scored, searched = [], 0
    for exp_id, ens in by_exp.items():
        for en in ens:
            if en.kind != EntryKind.reaction or not (en.content or {}).get("rows"):
                continue
            searched += 1
            sim = chem.reaction_similarity(q, chem.reaction_profile(en.content["rows"]))
            if sim and sim["score"] >= 0.4:
                scored.append((sim, exps[exp_id], en, ens))
    scored.sort(key=lambda t: (-t[0]["score"], -(t[1].updated_at.timestamp())))
    out = []
    for sim, exp, en, ens in scored[: max(1, min(body.limit, 20))]:
        c = en.content or {}
        rows = c.get("rows") or []
        cond = c.get("conditions") or {}
        products = [r for r in rows if r.get("role") == "product"]
        best = max((p for p in products if p.get("yield_pct") is not None), key=lambda p: p["yield_pct"], default=None)
        name = lambda r: r.get("name") or r.get("formula") or ""  # noqa: E731
        out.append({
            **sim, "experiment_id": exp.id, "eid": exp.eid, "title": exp.title, "status": exp.status.value,
            "owner": user_brief(session, exp.owner_id), "date": exp.created_at.isoformat() + "Z", "entry_id": en.id,
            "reaction_smiles": c.get("reaction_smiles", ""),
            "substrates": [name(r) for r in rows if r.get("role") == "reactant"],
            "products": [name(r) for r in products],
            "reagents": [name(r) for r in rows if r.get("role") in ("reagent", "catalyst")],
            "solvents": [name(r) for r in rows if r.get("role") == "solvent"],
            "temperature_c": cond.get("temperature_c"), "temperature_label": cond.get("temperature_label"),
            "time_h": cond.get("time_h"), "atmosphere": cond.get("atmosphere"),
            "yield_pct": best["yield_pct"] if best else None,
            "notes": _notes(ens, en),
            # what "Use these conditions" copies: everything that isn't a substrate or product
            "condition_rows": [_condition_row(r) for r in rows if r.get("role") in ("reagent", "catalyst", "solvent")],
            "conditions_raw": {k: v for k, v in cond.items() if v not in (None, "")},
        })
    return {"results": out, "searched": searched}
