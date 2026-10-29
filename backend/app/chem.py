"""Cheminformatics helpers built on RDKit.

Units used throughout the stoichiometry table (the same conventions as most chemistry ELNs):
    mass  mg      amount  mmol     volume  mL
    MW    g/mol   density g/mL     conc    mol/L (== mmol/mL)
"""
from __future__ import annotations

from functools import lru_cache

import math

from typing import Any, Optional

from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import AllChem, Descriptors, rdChemReactions, rdMolDescriptors
from rdkit.Chem.Draw import rdMolDraw2D

RDLogger.DisableLog("rdApp.*")


class ChemError(ValueError):
    pass


# ---------------------------------------------------------------- parsing / properties

def mol_from_any(text: str) -> Chem.Mol:
    """Accept SMILES, a molfile/MOL block, or an InChI string."""
    text = (text or "").strip()
    if not text:
        raise ChemError("empty structure")
    mol = None
    if "M  END" in text or "V2000" in text or "V3000" in text:
        mol = Chem.MolFromMolBlock(text, sanitize=True)
    elif text.startswith("InChI="):
        mol = Chem.MolFromInchi(text)
    else:
        mol = Chem.MolFromSmiles(text)
    if mol is None:
        raise ChemError("could not parse structure")
    return mol


def properties(text: str) -> dict[str, Any]:
    mol = mol_from_any(text)
    return {
        "smiles": Chem.MolToSmiles(mol),
        "formula": rdMolDescriptors.CalcMolFormula(mol),
        "mw": round(Descriptors.MolWt(mol), 4),
        "exact_mass": round(Descriptors.ExactMolWt(mol), 5),
        "inchi": Chem.MolToInchi(mol) or "",
        "inchikey": Chem.MolToInchiKey(mol) or "",
        "hbd": rdMolDescriptors.CalcNumHBD(mol),
        "hba": rdMolDescriptors.CalcNumHBA(mol),
        "logp": round(Descriptors.MolLogP(mol), 2),
        "tpsa": round(rdMolDescriptors.CalcTPSA(mol), 2),
        "rotatable_bonds": rdMolDescriptors.CalcNumRotatableBonds(mol),
        "molblock": Chem.MolToMolBlock(mol),
        **druglike(mol),
    }


def druglike(mol: Chem.Mol) -> dict[str, Any]:
    """Medicinal-chemistry descriptors on the largest fragment (so salts and solvates don't skew them)."""
    frags = Chem.GetMolFrags(mol, asMols=True)
    m = max(frags, key=lambda f: f.GetNumHeavyAtoms()) if frags else mol
    mw, logp = Descriptors.MolWt(m), Descriptors.MolLogP(m)
    hbd, hba = rdMolDescriptors.CalcNumHBD(m), rdMolDescriptors.CalcNumHBA(m)
    tpsa, rotb = rdMolDescriptors.CalcTPSA(m), rdMolDescriptors.CalcNumRotatableBonds(m)
    lip = [name for name, bad in (("MW > 500", mw > 500), ("cLogP > 5", logp > 5), ("HBD > 5", hbd > 5), ("HBA > 10", hba > 10)) if bad]
    veber = [name for name, bad in (("TPSA > 140", tpsa > 140), ("rotatable bonds > 10", rotb > 10)) if bad]
    try:
        from rdkit.Chem import QED
        qed = round(QED.qed(m), 2)
    except Exception:  # noqa: BLE001 - QED fails on some exotic species
        qed = None
    return {
        "heavy_atoms": m.GetNumHeavyAtoms(), "aromatic_rings": rdMolDescriptors.CalcNumAromaticRings(m),
        "fsp3": round(rdMolDescriptors.CalcFractionCSP3(m), 2), "qed": qed,
        "lipinski_violations": lip, "veber_violations": veber,
    }


def depict_svg(text: str, width: int = 300, height: int = 200, highlight: str = "", atom_ids: list[int] | None = None,
               numbers: bool = False) -> str:
    mol = mol_from_any(text)
    if atom_ids or numbers:  # atom indices refer to the main fragment, as in predictions
        frags = Chem.GetMolFrags(mol, asMols=True)
        mol = max(frags, key=lambda f: f.GetNumHeavyAtoms())
    AllChem.Compute2DCoords(mol)
    d = rdMolDraw2D.MolDraw2DSVG(width, height)
    opts = d.drawOptions()
    opts.clearBackground = False
    opts.bondLineWidth = 1.5
    if numbers:
        for a in mol.GetAtoms():
            a.SetProp("atomNote", str(a.GetIdx() + 1))
        opts.annotationFontScale = 0.6
    atoms: list[int] = [i for i in (atom_ids or []) if 0 <= i < mol.GetNumAtoms()]
    bonds: list[int] = []
    if highlight:  # colour the part of the molecule that matches a substructure query
        patt = Chem.MolFromSmarts(highlight) or Chem.MolFromSmiles(highlight)
        if patt is not None:
            for match in mol.GetSubstructMatches(patt)[:4]:
                atoms.extend(match)
                for bnd in patt.GetBonds():
                    b2 = mol.GetBondBetweenAtoms(match[bnd.GetBeginAtomIdx()], match[bnd.GetEndAtomIdx()])
                    if b2 is not None:
                        bonds.append(b2.GetIdx())
    if atoms:
        colour = (0.42, 0.75, 0.98)
        d.DrawMolecule(mol, highlightAtoms=atoms, highlightBonds=bonds,
                       highlightAtomColors={i: colour for i in atoms}, highlightBondColors={i: colour for i in bonds})
    else:
        d.DrawMolecule(mol)
    d.FinishDrawing()
    return d.GetDrawingText()


def depict_reaction_svg(rxn_smiles: str, width: int = 700, height: int = 200) -> str:
    rxn = _reaction(rxn_smiles)
    d = rdMolDraw2D.MolDraw2DSVG(width, height)
    d.drawOptions().clearBackground = False
    d.DrawReaction(rxn)
    d.FinishDrawing()
    return d.GetDrawingText()


def depict_reaction_png(rxn_smiles: str, width: int = 1400, height: int = 360) -> bytes:
    """A raster scheme for PDFs (which can't take inline SVG)."""
    rxn = _reaction(rxn_smiles)
    d = rdMolDraw2D.MolDraw2DCairo(width, height)
    d.drawOptions().bondLineWidth = 2
    d.DrawReaction(rxn)
    d.FinishDrawing()
    return d.GetDrawingText()


def _reaction(text: str):
    text = (text or "").strip()
    try:
        if "$RXN" in text:
            rxn = rdChemReactions.ReactionFromRxnBlock(text)
        else:
            rxn = rdChemReactions.ReactionFromSmarts(text, useSmiles=True)
    except Exception as exc:  # RDKit raises ValueError subclasses inconsistently
        raise ChemError(f"could not parse reaction: {exc}") from exc
    if rxn is None:
        raise ChemError("could not parse reaction")
    return rxn


def _merge_salts(mols) -> list[Chem.Mol]:
    """Reaction SMILES writes NaBH4 as '[Na+].[BH4-]', i.e. two components.
    Recombine adjacent charged fragments until their net charge is zero."""
    out: list[Chem.Mol] = []
    pending: Optional[Chem.Mol] = None
    charge = 0
    for m in mols:
        q = Chem.GetFormalCharge(m)
        if pending is None:
            if q == 0:
                out.append(m)
            else:
                pending, charge = m, q
            continue
        if q == 0:  # neutral molecule between ions: keep it separate
            out.append(m)
            continue
        pending = Chem.CombineMols(pending, m)
        charge += q
        if charge == 0:
            out.append(pending)
            pending = None
    if pending is not None:
        out.append(pending)
    return out


def parse_reaction(text: str) -> dict[str, Any]:
    """Split a reaction (SMILES 'A.B>C>D' or RXN block) into stoichiometry rows."""
    rxn = _reaction(text)
    rows: list[dict[str, Any]] = []

    def add(mol: Chem.Mol, role: str) -> None:
        try:
            Chem.SanitizeMol(mol)
        except Exception:
            pass
        smi = Chem.MolToSmiles(mol)
        try:
            p = properties(smi)
            rows.append(new_row(role=role, smiles=p["smiles"], mw=p["mw"], formula=p["formula"]))
        except ChemError:
            rows.append(new_row(role=role, smiles=smi))

    for m in _merge_salts(rxn.GetReactants()):
        add(m, "reactant")
    for m in _merge_salts(rxn.GetAgents()):
        add(m, "reagent")
    for m in _merge_salts(rxn.GetProducts()):
        add(m, "product")
    if rows and not any(r["limiting"] for r in rows):
        for r in rows:
            if r["role"] == "reactant":
                r["limiting"] = True
                break
    return {"rows": rows, "reaction_smiles": rdChemReactions.ReactionToSmiles(rxn)}


# ---------------------------------------------------------------- stoichiometry

ROLES = ("reactant", "reagent", "catalyst", "solvent", "product")
BASES = ("mass", "volume", "equiv", "mmol", "fill")  # fill: solvent volume set by target concentration


def new_row(**kw: Any) -> dict[str, Any]:
    row = {
        "role": "reactant", "name": "", "smiles": "", "formula": "", "mw": None,
        "material_id": None, "lot_id": None,
        "equiv": None, "mmol": None, "mass_mg": None, "volume_ml": None,
        "density": None, "conc_m": None, "purity": 100.0,
        "limiting": False, "basis": "equiv",
        "actual_mass_mg": None, "theoretical_mass_mg": None, "yield_pct": None,
        "notes": "",
    }
    row.update(kw)
    return row


ROLE_WORD = {"reactant": "Reactant", "reagent": "Reagent", "catalyst": "Catalyst", "solvent": "Solvent", "product": "Product"}


def row_label(row: dict[str, Any], rows: list[Any]) -> str:
    """What to call a reaction-table row in a message: its name, else its formula, else its place ("Reactant 2").
    Never the raw SMILES, which means nothing to most readers; and no name is made up."""
    name = str(row.get("name") or "").strip()
    if name:
        return name
    formula = str(row.get("formula") or "").strip()
    if formula:
        return formula
    role = row.get("role") if row.get("role") in ROLES else "reactant"
    same = [r for r in rows if isinstance(r, dict) and (r.get("role") if r.get("role") in ROLES else "reactant") == role]
    n = next((i for i, r in enumerate(same, 1) if r is row), len(same) or 1)
    return f"{ROLE_WORD[role]} {n}"


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None  # "NaN"/"Infinity" would make invalid JSON


def _r(v: Optional[float], nd: int = 4) -> Optional[float]:
    """Round for display without destroying small amounts: at least `nd` decimals and 6 significant figures."""
    if v is None or not math.isfinite(v):
        return None
    if v == 0:
        return 0.0
    digits = max(nd, 5 - int(math.floor(math.log10(abs(v)))))
    return round(v, min(digits, 12))


def compute_stoichiometry(rows_in: list[dict[str, Any]], target_conc_m: Any = None) -> dict[str, Any]:
    """Fill in derived quantities for a reaction table.

    Each row carries a `basis` saying which quantity the chemist typed:
      mass   -> mmol = mass * purity / MW
      volume -> mass = V * density (neat liquid) or mmol = V * conc (solution)
      mmol   -> taken as-is
      equiv  -> mmol = equiv * mmol(limiting reagent)
    The limiting row (exactly one) defines 1.0 equiv; products get theoretical and
    percent yield from the limiting amount.

    With `target_conc_m` (mol/L, relative to the limiting reagent), solvent rows whose basis
    is "fill" get whatever volume brings the total to that concentration, split evenly.
    """
    rows = [new_row(**{k: v for k, v in r.items()}) for r in rows_in]
    warnings: list[str] = []

    for r in rows:
        if r["role"] not in ROLES:
            r["role"] = "reactant"
        if r["basis"] not in BASES:
            r["basis"] = "equiv"
        if r["smiles"] and not _f(r["mw"]):
            try:
                p = properties(r["smiles"])
                r["mw"], r["formula"] = p["mw"], p["formula"]
                r["smiles"] = p["smiles"]
            except ChemError:
                warnings.append(f"Could not read the structure of {row_label(r, rows)}")

    limiting = [r for r in rows if r["limiting"] and r["role"] != "product"]
    if len(limiting) > 1:
        warnings.append("More than one limiting reagent flagged; using the first")
        for r in limiting[1:]:
            r["limiting"] = False
        limiting = limiting[:1]
    if not limiting:
        for r in rows:
            if r["role"] == "reactant":
                r["limiting"] = True
                limiting = [r]
                break

    def resolve_direct(r: dict[str, Any]) -> Optional[float]:
        """mmol from the row's own inputs (not via equivalents)."""
        mw, purity = _f(r["mw"]), (_f(r["purity"]) or 100.0) / 100.0
        dens, conc = _f(r["density"]), _f(r["conc_m"])
        mass, vol, mmol = _f(r["mass_mg"]), _f(r["volume_ml"]), _f(r["mmol"])
        b = r["basis"]
        if b == "mmol" and mmol is not None:
            return mmol
        if b == "volume" and vol is not None:
            if conc:
                return vol * conc
            if dens:
                mass = vol * dens * 1000.0
                r["mass_mg"] = mass
                return mass * purity / mw if mw else None
            return None
        if b == "mass" and mass is not None and mw:
            return mass * purity / mw
        return None

    lim_mmol: Optional[float] = None
    if limiting:
        lr = limiting[0]
        if lr["basis"] == "equiv":  # equiv is meaningless for the reference row
            lr["basis"] = "mass" if _f(lr["mass_mg"]) else ("mmol" if _f(lr["mmol"]) else "mass")
        lim_mmol = resolve_direct(lr)
        if lim_mmol is None:
            warnings.append("Enter an amount (mass, volume or mmol) for the limiting reagent")

    for r in rows:
        if r["role"] == "solvent":
            # solvents: only volume (and mass if density) are meaningful
            vol, dens = _f(r["volume_ml"]), _f(r["density"])
            r["mass_mg"] = _r(vol * dens * 1000.0, 2) if (vol and dens) else _f(r["mass_mg"])
            r["equiv"] = r["mmol"] = None
            continue

        mw, purity = _f(r["mw"]), (_f(r["purity"]) or 100.0) / 100.0
        dens, conc = _f(r["density"]), _f(r["conc_m"])

        if r["role"] == "product":
            eq = _f(r["equiv"]) or 1.0
            r["equiv"] = eq
            if lim_mmol is not None:
                r["mmol"] = lim_mmol * eq
                if mw:
                    r["theoretical_mass_mg"] = _r(r["mmol"] * mw, 3)
            actual = _f(r["actual_mass_mg"])
            if actual is not None and _f(r["theoretical_mass_mg"]):
                r["yield_pct"] = round(100.0 * actual * purity / r["theoretical_mass_mg"], 1)
                if r["yield_pct"] > 100.5:
                    warnings.append(f"Yield of {r['name'] or 'the product'} is {r['yield_pct']:g}%: over 100% usually means residual solvent "
                                    "or impurities, or a mass entered in the wrong unit")
            else:
                r["yield_pct"] = None
            r["mmol"] = _r(_f(r["mmol"]))
            continue

        if r["limiting"]:
            mmol = lim_mmol
        elif r["basis"] == "equiv":
            eq = _f(r["equiv"])
            mmol = eq * lim_mmol if (eq is not None and lim_mmol is not None) else None
        else:
            mmol = resolve_direct(r)

        r["mmol"] = mmol
        if mmol is not None:
            if lim_mmol:
                r["equiv"] = 1.0 if r["limiting"] else mmol / lim_mmol
            b = r["basis"]
            if b in ("equiv", "mmol"):
                if conc:
                    r["volume_ml"] = mmol / conc
                    r["mass_mg"] = mmol * mw / purity if mw else None
                elif mw:
                    r["mass_mg"] = mmol * mw / purity
                    r["volume_ml"] = r["mass_mg"] / dens / 1000.0 if dens else None
            elif b == "mass":
                r["volume_ml"] = (_f(r["mass_mg"]) / dens / 1000.0) if dens else None
            elif b == "volume" and conc:
                # solution: mass of solute delivered
                r["mass_mg"] = mmol * mw / purity if mw else None
        for k, nd in (("mmol", 4), ("equiv", 3), ("mass_mg", 3), ("volume_ml", 4)):
            r[k] = _r(_f(r[k]), nd)

    target = _f(target_conc_m)
    fill_rows = [r for r in rows if r["role"] == "solvent" and r["basis"] == "fill"]
    if fill_rows:
        if target and lim_mmol:
            needed = lim_mmol / target  # mmol / (mmol/mL) = mL
            fixed = sum(_f(r["volume_ml"]) or 0 for r in rows if r not in fill_rows and r["role"] != "product")
            remaining = needed - fixed
            if remaining <= 0:
                warnings.append(f"Other liquids already exceed {needed:.3g} mL; can't reach {target:g} M")
                remaining = 0.0
            for r in fill_rows:
                r["volume_ml"] = _r(remaining / len(fill_rows), 3)
                dens = _f(r["density"])
                r["mass_mg"] = _r(r["volume_ml"] * dens * 1000.0, 2) if dens else None
        elif not target:
            warnings.append("Set a target concentration to calculate the solvent volume")

    totals = {
        "limiting_mmol": _r(lim_mmol),
        "total_volume_ml": _r(sum(_f(r["volume_ml"]) or 0 for r in rows), 3),
    }
    if lim_mmol and totals["total_volume_ml"]:
        totals["concentration_m"] = _r(lim_mmol / totals["total_volume_ml"], 4)
    totals["metrics"] = green_metrics(rows)
    return {"rows": rows, "totals": totals, "warnings": warnings}


def green_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Atom economy, reaction mass efficiency, E-factor, PMI and volume efficiency for the reaction
    as written (work-up and purification solvents are not in the table, so they're not counted)."""
    prod = next((r for r in rows if r["role"] == "product" and _f(r["mw"])), None)
    reactants = [r for r in rows if r["role"] == "reactant"]
    inputs = [r for r in rows if r["role"] != "product"]
    out: dict[str, Any] = {"scope": "reaction only (work-up and purification not included)"}
    # Only rows marked Reactant count in atom economy and RME. Over 100 % is impossible: usually a co-reactant
    # (acetic anhydride, say) is marked as Reagent. Then no number is given, and "impossible" says which.
    impossible: list[str] = []
    if prod and reactants and all(_f(r["mw"]) for r in reactants):
        # one of each reactant, as in the balanced equation for most couplings and condensations
        ae = _r(100.0 * _f(prod["mw"]) / sum(_f(r["mw"]) for r in reactants), 1)
        if ae > 100:
            impossible.append("atom_economy")
        else:
            out["atom_economy_pct"] = ae
    missing = [row_label(r, rows) for r in inputs if _f(r["mass_mg"]) is None]
    out["missing_mass"] = missing
    actual = _f(prod["actual_mass_mg"]) if prod else None
    if actual:
        inputs_mg = sum(_f(r["mass_mg"]) or 0 for r in inputs)
        if inputs_mg and not missing:
            out["pmi"] = _r(inputs_mg / actual, 1)
            out["e_factor"] = _r((inputs_mg - actual) / actual, 1)
        rmass = [_f(r["mass_mg"]) for r in reactants]
        if rmass and all(rmass):
            rme = _r(100.0 * actual / sum(rmass), 1)
            if rme > 100:
                impossible.append("rme")
            else:
                out["rme_pct"] = rme
        solvent_ml = sum(_f(r["volume_ml"]) or 0 for r in rows if r["role"] == "solvent")
        if solvent_ml:
            out["solvent_l_per_kg"] = _r(solvent_ml / (actual / 1000.0), 0)  # mL/g == L/kg
    if impossible:
        out["impossible"] = impossible
    return out


# ---------------------------------------------------------------- searching

def substructure_matcher(query: str):
    q = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
    if q is None:
        raise ChemError("invalid SMARTS/SMILES query")

    def match(smiles: str) -> bool:
        m = Chem.MolFromSmiles(smiles) if smiles else None
        return bool(m and m.HasSubstructMatch(q))

    return match


_fpgen = None


def _fp(mol: Chem.Mol):
    global _fpgen
    if _fpgen is None:
        from rdkit.Chem import rdFingerprintGenerator

        _fpgen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    return _fpgen.GetFingerprint(mol)


def similarity_scorer(query: str):
    qfp = _fp(mol_from_any(query))

    def score(smiles: str) -> float:
        m = Chem.MolFromSmiles(smiles) if smiles else None
        return DataStructs.TanimotoSimilarity(qfp, _fp(m)) if m else 0.0

    return score


# ---------------------------------------------------------------- reaction similarity ("have we done this before?")

@lru_cache(maxsize=20000)
def _mol_key(smiles: str) -> tuple[Optional[str], Any]:
    """(InChIKey of the parent, Morgan fingerprint) for a SMILES, or (None, None) if it doesn't parse."""
    m = Chem.MolFromSmiles(smiles) if smiles else None
    if m is None:
        return None, None
    frags = Chem.GetMolFrags(m, asMols=True)  # salts: compare the largest piece (the parent)
    parent = max(frags, key=lambda f: f.GetNumHeavyAtoms()) if len(frags) > 1 else m
    return Chem.MolToInchiKey(parent) or None, _fp(parent)


def _set_sim(a: list[Any], b: list[Any]) -> Optional[float]:
    """Symmetric best-match Tanimoto between two sets of fingerprints (None if either side is empty)."""
    if not a or not b:
        return None
    ab = sum(max(DataStructs.BulkTanimotoSimilarity(x, b)) for x in a) / len(a)
    ba = sum(max(DataStructs.BulkTanimotoSimilarity(y, a)) for y in b) / len(b)
    return (ab + ba) / 2


def reaction_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fingerprints and keys for one reaction table, split into substrates, products and everything else."""
    out: dict[str, Any] = {"sub": [], "prod": [], "other": set(), "sub_keys": set(), "prod_keys": set(), "names": set()}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("smiles") or "", str):
            continue
        role, smi = r.get("role"), (r.get("smiles") or "").strip()
        key, fp = _mol_key(smi) if smi else (None, None)
        name = (r.get("name") or "").strip().lower()
        if role == "product":
            if fp is not None:
                out["prod"].append(fp); out["prod_keys"].add(key)
        elif role == "reactant" or r.get("limiting"):
            if fp is not None:
                out["sub"].append(fp); out["sub_keys"].add(key)
        else:  # reagents, catalysts, solvents: compared by identity
            if key:
                out["other"].add(key)
            elif name:
                out["names"].add(name)
    return out


def _transform_fp(p: dict[str, Any]):
    """What the reaction does: product bits minus substrate bits (a crude difference fingerprint)."""
    if not p["sub"] or not p["prod"]:
        return None
    on_p = set().union(*(set(f.GetOnBits()) for f in p["prod"]))
    on_s = set().union(*(set(f.GetOnBits()) for f in p["sub"]))
    made, lost = on_p - on_s, on_s - on_p
    return made, lost


def reaction_similarity(q: dict[str, Any], c: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Score how alike two reaction profiles are (0..1) and say in what way.

    Substrates and products are compared as sets of structures, the transformation by which
    structural features appear and disappear, and reagents/solvents by identity. A query without
    a product yet (still planning) is scored on substrates and reagents alone."""
    sub = _set_sim(q["sub"], c["sub"])
    prod = _set_sim(q["prod"], c["prod"])
    if sub is None and prod is None:
        return None
    tq, tc = _transform_fp(q), _transform_fp(c)
    trans = None
    if tq and tc:
        def jac(x: set, y: set) -> float:
            return len(x & y) / len(x | y) if (x or y) else 1.0
        trans = (jac(tq[0], tc[0]) + jac(tq[1], tc[1])) / 2
    qa, ca = q["other"] | q["names"], c["other"] | c["names"]
    cond = len(qa & ca) / len(qa | ca) if (qa and ca) else None
    parts = [(sub, 0.35), (prod, 0.35), (trans, 0.2), (cond, 0.1)]
    got = [(v, w) for v, w in parts if v is not None]
    score = sum(v * w for v, w in got) / sum(w for _, w in got)
    same_sub = bool(q["sub_keys"]) and q["sub_keys"] == c["sub_keys"]
    same_prod = bool(q["prod_keys"]) and q["prod_keys"] == c["prod_keys"]
    if same_sub and (same_prod or not q["prod_keys"]):
        kind, score = "same", max(score, 0.95)
    elif same_prod:
        kind = "same_product"
    elif (trans is not None and trans >= 0.6 and (sub or 0) >= 0.3) or (q["sub_keys"] & c["sub_keys"] and max(prod or 0, trans or 0) >= 0.35):
        kind = "analogue"      # same transformation on a different substrate (or a different coupling partner)
    elif same_sub or (sub or 0) >= 0.7:
        kind = "same_substrate"
    else:
        kind = "related"
    return {"score": round(score, 3), "kind": kind, "substrates": None if sub is None else round(sub, 3),
            "products": None if prod is None else round(prod, 3), "transformation": None if trans is None else round(trans, 3),
            "conditions": None if cond is None else round(cond, 3)}


# ---------------------------------------------------------------- structure checks for spectra

def structure_check(text: str) -> dict[str, Any]:
    """Topologically distinct carbons and proton environments (symmetry from canonical ranks).

    The two protons of a CH2 next to a stereocentre, or at the end of a C=C (styrene, acrylates), count as two
    environments: they usually give two signals. When they happen to overlap, fewer signals than environments
    is worded as "some probably overlap"."""
    from .nmrpredict import diastereotopic_ch2, geminal_alkene_ch2
    mol = mol_from_any(text)
    split = diastereotopic_ch2(mol) | geminal_alkene_ch2(mol)
    molH = Chem.AddHs(mol)  # heavy atoms keep their indices; the hydrogens come after them
    ranks = list(Chem.CanonicalRankAtoms(molH, breakTies=False))
    carbons = {ranks[a.GetIdx()] for a in molH.GetAtoms() if a.GetSymbol() == "C"}
    groups: dict[Any, dict[str, Any]] = {}
    seen: dict[int, int] = {}
    for a in molH.GetAtoms():
        if a.GetSymbol() != "H":
            continue
        heavy = a.GetNeighbors()[0]
        key: Any = ranks[a.GetIdx()]
        if heavy.GetIdx() in split:  # its two H apart (the same half of symmetry-equivalent CH2s together)
            half = seen[heavy.GetIdx()] = seen.get(heavy.GetIdx(), -1) + 1
            key = ("split", ranks[heavy.GetIdx()], half)
        g = groups.setdefault(key, {"count": 0, "on": heavy.GetSymbol(), "aromatic": heavy.GetIsAromatic()})
        g["count"] += 1
    env = sorted(groups.values(), key=lambda g: -g["count"])
    exchangeable = sum(g["count"] for g in env if g["on"] in ("O", "N", "S"))
    return {
        "formula": rdMolDescriptors.CalcMolFormula(mol),
        "total_h": sum(g["count"] for g in env),
        "exchangeable_h": exchangeable,
        "h_environments": [{"count": g["count"], "on": g["on"], "aromatic": g["aromatic"]} for g in env],
        "unique_carbons": len(carbons),
        "total_carbons": sum(1 for a in mol.GetAtoms() if a.GetSymbol() == "C"),
    }


# monoisotopic masses (IUPAC/NIST)
_E = 0.000548580
_ADDUCTS = [  # name, sign, atoms added (+) or removed (-), charge
    ("[M + H]+", {"H": 1}, 1), ("[M + Na]+", {"Na": 1}, 1), ("[M + K]+", {"K": 1}, 1),
    ("[M + NH4]+", {"N": 1, "H": 4}, 1), ("[M]+", {}, 1), ("[M − H]−", {"H": -1}, -1),
]
_MONO = {"H": 1.00782503207, "C": 12.0, "N": 14.0030740048, "O": 15.99491461956, "Na": 22.9897692809,
         "K": 38.96370668, "F": 18.99840322, "Cl": 34.96885268, "Br": 78.9183371, "I": 126.904473,
         "S": 31.97207100, "P": 30.97376163, "Si": 27.9769265325, "B": 11.0093054, "Se": 79.9165213}


def _counts(mol: Chem.Mol) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in Chem.AddHs(mol).GetAtoms():
        out[a.GetSymbol()] = out.get(a.GetSymbol(), 0) + 1
    return out


def _hill(c: dict[str, int]) -> str:
    keys = sorted(c, key=lambda k: (k != "C", k != "H" if "C" in c else False, k))
    return "".join(f"{k}{c[k] if c[k] != 1 else ''}" for k in keys if c[k] > 0)


def hrms_ions(text: str) -> dict[str, Any]:
    mol = mol_from_any(text)
    base = _counts(mol)
    missing = [k for k in base if k not in _MONO]
    if missing:
        raise ChemError(f"No monoisotopic mass for {', '.join(missing)}")
    neutral = sum(_MONO[k] * n for k, n in base.items()) - Chem.GetFormalCharge(mol) * _E
    ions = []
    for name, delta, z in _ADDUCTS:
        c = dict(base)
        for k, n in delta.items():
            c[k] = c.get(k, 0) + n
        if any(v < 0 for v in c.values()):
            continue
        m = sum(_MONO[k] * n for k, n in c.items()) - z * _E
        ions.append({"adduct": name, "formula": _hill(c), "charge": z, "mz": round(m / abs(z), 4)})
    return {"formula": _hill(base), "monoisotopic": round(neutral, 4), "ions": ions}
