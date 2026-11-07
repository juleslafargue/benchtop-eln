"""Draft an SI-style experimental paragraph from an experiment's structured sections.

The output is a starting point the chemist edits; it never invents values. Anything it
needs but can't find is listed in `missing` instead of being guessed.
"""
from __future__ import annotations

import html
import re
from typing import Any

from .models import Entry, EntryKind

SUPERS = {"1H": "¹H", "13C": "¹³C", "19F": "¹⁹F", "31P": "³¹P", "11B": "¹¹B", "15N": "¹⁵N"}
PURIF = {
    "column": "flash column chromatography", "prep_tlc": "preparative TLC", "prep_hplc": "preparative HPLC",
    "recrystallization": "recrystallisation", "trituration": "trituration", "distillation": "distillation",
}
ATM = {"N2": "N₂", "H2": "H₂", "O2": "O₂"}


def _sig(v: float, n: int = 3) -> str:
    if v == 0:
        return "0"
    s = f"{v:.{n}g}"
    if "e" in s:
        s = f"{v:.0f}"
    return s


def _sig3(v: float) -> str:
    """3 significant figures, keeping meaningful trailing zeros (1.00, 0.0500)."""
    return f"{v:#.3g}".rstrip(".") if v < 1000 else f"{v:.0f}"


def name_mid(n: str) -> str:
    """Lower-case a chemical name's leading capital when it isn't at the start of a sentence
    (4-Methoxybiphenyl -> 4-methoxybiphenyl), leaving symbols like N,N- or Pd(PPh3)4 alone."""
    return re.sub(r"^([\d,'\-()\[\]]*)([A-Z])(?=[a-z]{2})", lambda m: m.group(1) + m.group(2).lower(), n)


def mass(mg: float | None) -> str | None:
    if mg is None:
        return None
    return f"{_sig(mg / 1000)} g" if mg >= 1000 else f"{_sig(mg)} mg"


def amount(mmol: float | None) -> str | None:
    if mmol is None:
        return None
    return f"{_sig3(mmol)} mmol"


def volume(ml: float | None) -> str | None:
    if ml is None:
        return None
    return f"{_sig3(ml * 1000)} µL" if ml < 0.1 else f"{_sig3(ml)} mL"


def technique_label(t: str) -> str:
    for k, v in SUPERS.items():
        t = re.sub(rf"^{k}\b", v, t)
    return t


def _reagent_phrase(r: dict[str, Any], first: bool = False) -> str:
    name = r.get("name") or r.get("formula") or "reagent"
    name = name if first else name_mid(name)
    bits: list[str] = []
    eq = r.get("equiv")
    if r.get("conc_m"):
        bits.append(f"{_sig(r['conc_m'])} M solution")
    liquid = r.get("density") or r.get("conc_m") or r.get("basis") == "volume"
    if liquid and r.get("volume_ml") is not None:
        bits.append(volume(r["volume_ml"]))
    if r.get("mass_mg") is not None and not r.get("conc_m"):
        bits.append(mass(r["mass_mg"]))
    if r.get("mmol") is not None:
        bits.append(amount(r["mmol"]))
    if r.get("role") == "catalyst" and eq is not None and eq < 0.5:
        bits.append(f"{_sig(eq * 100, 2)} mol%")
    elif eq is not None:
        bits.append(f"{eq:.1f} equiv" if abs(eq - round(eq, 1)) < 1e-9 else f"{_sig(eq)} equiv")
    return f"{name} ({', '.join(b for b in bits if b)})" if bits else name


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def draft(entries: list[Entry]) -> dict[str, Any]:
    missing: list[str] = []
    rxn = next((e.content or {} for e in entries if e.kind == EntryKind.reaction and (e.content or {}).get("rows")), None)
    pur = next((e.content or {} for e in entries if e.kind == EntryKind.purification), {}) or {}
    chars = [it for e in entries if e.kind == EntryKind.characterization for it in (e.content or {}).get("items", [])]

    paras: list[str] = []
    product = None
    if rxn is None:
        missing.append("a reaction table with amounts")
    else:
        rows = rxn.get("rows", [])
        cond = rxn.get("conditions") or {}
        reactants = [r for r in rows if r.get("role") in ("reactant", "reagent", "catalyst")]
        reactants.sort(key=lambda r: (not r.get("limiting"), {"reactant": 0, "reagent": 1, "catalyst": 2}[r["role"]]))
        solvents = [r for r in rows if r.get("role") == "solvent"]
        products = [r for r in rows if r.get("role") == "product"]
        product = max(products, key=lambda p: p.get("yield_pct") or -1) if products else None

        if not any(r.get("mmol") for r in reactants):
            missing.append("amounts for the limiting reagent")
        s = _join([_reagent_phrase(r, first=(i == 0)) for i, r in enumerate(reactants)]) or "The reactants"
        if solvents:
            conc = (rxn.get("totals") or {}).get("concentration_m")
            cm = f"{_sig(conc, 2)} M" if conc else None
            if len(solvents) == 1:
                r = solvents[0]
                inner = ", ".join(x for x in (volume(r.get("volume_ml")), cm) if x)
                s += f" in {name_mid(r.get('name') or r.get('formula') or 'solvent')}" + (f" ({inner})" if inner else "")
            else:
                sv = _join([name_mid(r.get("name") or r.get("formula") or "solvent") + (f" ({volume(r['volume_ml'])})" if r.get("volume_ml") else "") for r in solvents])
                s += f" in {sv}" + (f" (overall {cm})" if cm else "")
        temp = cond.get("temperature_label") or (f"{_sig(cond['temperature_c'])} °C" if cond.get("temperature_c") is not None else None)
        time = cond.get("time_h")
        atm = cond.get("atmosphere")
        tail = " were stirred"
        if temp:
            tail += f" at {temp}"
        else:
            missing.append("reaction temperature")
        if atm and atm != "air":
            tail += f" under {ATM.get(atm, atm)}"
        if time is not None:
            tail += f" for {_sig(time)} h" if time >= 1 else f" for {_sig(time * 60)} min"
        else:
            missing.append("reaction time")
        sentence = s[0].upper() + s[1:] + tail + "."
        if cond.get("notes"):
            sentence += f" {cond['notes'].strip().rstrip('.')}."
        paras.append(sentence)

    # work-up / isolation
    iso_mg = pur.get("isolated_mg")
    if iso_mg is None and product:
        iso_mg = product.get("actual_mass_mg")
    yld = None
    if product and iso_mg is not None and product.get("theoretical_mass_mg"):
        purity = (product.get("purity") or 100) / 100
        yld = 100 * iso_mg * purity / product["theoretical_mass_mg"]
    method = pur.get("method")
    if method in PURIF:
        detail = ", ".join(name_mid(x) for x in (pur.get("stationary_phase"), pur.get("eluent")) if x)
        iso = f"Purification by {PURIF[method]}" + (f" ({detail})" if detail else "") + " afforded"
    elif method == "none":
        iso = "Concentration afforded"
    elif method == "extraction":
        iso = "Extractive work-up afforded"
    else:
        iso = "Isolation afforded"
        missing.append("purification method")
    what = name_mid(product["name"]) if product and product.get("name") else "the title compound"
    look = f" as {_article(pur['appearance'].strip()[:1].lower() + pur['appearance'].strip()[1:])}" if pur.get("appearance") else ""
    amt = []
    if iso_mg is not None:
        amt.append(mass(iso_mg))
    else:
        missing.append("isolated mass")
    if yld is not None:
        amt.append(f"{yld:.0f}%")
    elif iso_mg is not None:
        missing.append("product structure (for % yield)")
    paras.append(f"{iso} {what}{look}" + (f" ({', '.join(amt)})" if amt else "") + ".")

    # characterisation
    data_bits: list[str] = []
    rf = pur.get("rf")
    if rf:
        data_bits.append(f"R<sub>f</sub> = {rf}" + (f" ({pur['rf_eluent']})" if pur.get("rf_eluent") else "") )
    for it in chars:
        d = (it.get("data") or "").strip().rstrip(".")
        if not d:
            continue
        t = technique_label((it.get("technique") or "").strip())
        c = (it.get("conditions") or "").strip()
        if t.lower() == "mp":
            data_bits.append(f"mp {d}")
        elif t.lower() == "rf" and not rf:
            data_bits.append(f"R<sub>f</sub> = {d}")
        else:
            data_bits.append(f"{t}" + (f" ({c})" if c else "") + f" {d}")
    if not data_bits:
        missing.append("characterisation data")
    char_para = "; ".join(data_bits) + ("." if data_bits else "")

    heading = product["name"] if product and product.get("name") else "Title compound"
    exp_html = " ".join(html.escape(p) for p in paras)
    html_out = f"<p><strong>{html.escape(heading)}.</strong> {exp_html}</p>"
    char_html = ""
    if char_para:
        char_html = html.escape(char_para).replace("&lt;sub&gt;", "<sub>").replace("&lt;/sub&gt;", "</sub>")
        char_html = re.sub(r"\bJ = ", "<i>J</i> = ", char_html)
        html_out += f"<p>{char_html}</p>"
    text = f"{heading}. " + " ".join(paras) + ("\n" + char_para.replace("<sub>f</sub>", "f") if char_para else "")
    # the parts on their own too, for the Supporting Information builder (0.33), which sets its own heading
    prod = {k: product.get(k) for k in ("name", "smiles", "formula", "mw")} if product else None
    return {"text": text, "html": html_out, "missing": missing, "heading": heading, "experimental_html": exp_html,
            "characterisation_html": char_html, "product": prod}


def _article(phrase: str) -> str:
    phrase = phrase.strip()
    if re.match(r"^(a|an|the)\b", phrase, re.I):
        return phrase
    return ("an " if phrase[:1].lower() in "aeiou" else "a ") + phrase
