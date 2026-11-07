"""A realistic demo lab: eight people, two groups, five notebooks, ~30 experiments over three months.

    python -m app.demo_lab            # into BENCHTOP_DATA_DIR (must be a fresh, empty database)

Everything goes through the API, as if people had used Benchtop day by day: a simulated clock moves
through the three months before today, so dates, signatures, comments and the audit trail all line up (and the
audit hash chain verifies). The chemistry is real chemistry with sensible amounts; yields, notes and
problems are the kind a real group records. Spectra are simulated from Benchtop's own shift
prediction, except the ibuprofen reference, which uses real raw FIDs.

Fictional people and a fictional target series (KX-101 and analogues); nothing here is real data
about real compounds' biology.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import warnings

with warnings.catch_warnings():  # the demo builder drives the API in-process; the library's deprecation notice isn't for users
    warnings.simplefilter("ignore", DeprecationWarning)
    from fastapi.testclient import TestClient

from . import chem, models
from .demo_spectra_sim import carbon, chromatogram, proton, tlc_plate

PW = "demo-lab-password"
# the story ends "now" (when the demo is loaded) and goes back three months from there
TODAY = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
CLOCK = {"now": TODAY}


class _SimDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102 - simulated clock for the demo history
        n = CLOCK["now"]
        return n if tz else n.replace(tzinfo=None)


def at(days_ago: float, hour: float = 10.0) -> None:
    CLOCK["now"] = (TODAY - timedelta(days=days_ago)).replace(hour=int(hour), minute=int((hour % 1) * 60), second=0)


def later(minutes: float) -> None:
    CLOCK["now"] = CLOCK["now"] + timedelta(minutes=minutes)


PEOPLE = [  # username, full name, role, who they are
    ("hweiss", "Hannah Weiss", "admin", "Lab manager: accounts, inventory, safety"),
    ("mruiz", "Maria Ruiz", "scientist", "Principal investigator, leads the Ruiz group"),
    ("pnair", "Priya Nair", "scientist", "Postdoc: KX-series medicinal chemistry"),
    ("dcho", "Daniel Cho", "scientist", "PhD student: Pd cross-coupling methodology"),
    ("slaurent", "Sophie Laurent", "scientist", "PhD student: heterocycles and click chemistry"),
    ("amorgan", "Alex Morgan", "scientist", "Undergraduate research student"),
    ("kwatanabe", "Kenji Watanabe", "scientist", "Process chemist, leads Process development"),
    ("ohaddad", "Omar Haddad", "viewer", "Collaborator (biology), read-only"),
]


class Lab:
    def __init__(self, c: TestClient):
        self.c = c
        self.tok: dict[str, tuple[str, datetime]] = {}
        self.uid: dict[str, int] = {}
        self.mat: dict[str, dict] = {}
        self.pins: dict[str, list[int]] = {}

    # ------------------------------------------------------------------ plumbing
    def h(self, u: str) -> dict:
        t = self.tok.get(u)
        if not t or CLOCK["now"] - t[1] > timedelta(hours=6) or CLOCK["now"] < t[1]:
            r = self.c.post("/api/auth/login", json={"username": u, "password": PW})
            assert r.status_code == 200, (u, r.text)
            self.tok[u] = (r.json()["token"], CLOCK["now"])
        return {"Authorization": f"Bearer {self.tok[u][0]}"}

    def call(self, method: str, u: str, path: str, **kw):
        r = self.c.request(method, path, headers=self.h(u), **kw)
        assert r.status_code < 400, (method, path, r.status_code, r.text[:300])
        return r.json() if r.content else None

    def get(self, u, p, **kw): return self.call("GET", u, p, **kw)
    def post(self, u, p, **kw): return self.call("POST", u, p, **kw)
    def put(self, u, p, **kw): return self.call("PUT", u, p, **kw)
    def patch(self, u, p, **kw): return self.call("PATCH", u, p, **kw)

    # ------------------------------------------------------------------ registry
    def material(self, name, smiles, cas, kind="reagent", density=None, lots=(), synonyms=""):
        m = self.post("hweiss", "/api/materials", json={"name": name, "smiles": smiles, "cas": cas, "kind": kind,
                                                        "density": density, "synonyms": synonyms})
        m["lot_ids"] = []
        for amt, unit, loc, sup, pur, *extra in lots:
            body = {"amount": amt, "unit": unit, "location": loc, "supplier": sup, "purity": pur}
            if extra:
                body["expires_at"] = (TODAY + timedelta(days=extra[0])).replace(tzinfo=None).isoformat()
            m["lot_ids"].append(self.post("hweiss", f"/api/materials/{m['id']}/lots", json=body)["id"])
        self.mat[name] = m
        return m

    def row(self, name, role, **kw):
        m = self.mat[name]
        r = {"role": role, "name": name, "smiles": m["smiles"], "material_id": m["id"],
             "lot_id": m["lot_ids"][0] if m["lot_ids"] else None, "density": m.get("density"), **kw}
        return r

    @staticmethod
    def product(name, smiles, lim_smiles, lim_mg, yield_pct, purity=98, lim_purity=100):
        """A product row whose isolated mass gives `yield_pct` from `lim_mg` of the limiting reagent."""
        mmol = lim_mg * lim_purity / 100 / chem.properties(lim_smiles)["mw"]
        mg = round(mmol * chem.properties(smiles)["mw"] * yield_pct / 100 / (purity / 100), 1) if yield_pct else None
        return {"role": "product", "name": name, "smiles": smiles, "actual_mass_mg": mg, "purity": purity, "equiv": 1}

    # ------------------------------------------------------------------ experiments
    def experiment(self, u, nb, title, *, tags=(), description="", template=None):
        body = {"title": title, "notebook_id": nb, "tags": list(tags), "description": description}
        if template:
            body["template_id"] = template
        exp = self.post(u, "/api/experiments", json=body)
        full = self.get(u, f"/api/experiments/{exp['id']}")
        exp["s"] = {e["title"]: e for e in full["entries"]}
        for key, secs in EXTRA.items():
            if key in title:
                for sec, val in secs.items():
                    if sec == "_remove":
                        for t in val:
                            if t in exp["s"]:
                                self.call("DELETE", u, f"/api/entries/{exp['s'].pop(t)['id']}")
                    elif sec in exp["s"]:
                        self.section(u, exp, sec, {"html": val} if isinstance(val, str) else val)
        return exp

    def section(self, u, exp, title, content, new_title=None):
        e = exp["s"][title]
        body = {"content": content}
        if new_title:
            body["title"] = new_title
        r = self.put(u, f"/api/entries/{e['id']}", json=body)
        exp["s"][new_title or title] = r
        return r

    def add_section(self, u, exp, kind, title, content):
        r = self.post(u, f"/api/experiments/{exp['id']}/entries", json={"kind": kind, "title": title, "content": content})
        exp["s"][title] = r
        return r

    def plugin_section(self, u, exp, key, values=None, rows=None, title=None):
        """A section from a plugin file (the examples are switched on in demo mode)."""
        r = self.post(u, f"/api/experiments/{exp['id']}/entries", json={"kind": "custom", "plugin_section": key, **({"title": title} if title else {})})
        c = r["content"]
        c["values"].update(values or {})
        c["rows"].update(rows or {})
        r = self.put(u, f"/api/entries/{r['id']}", json={"content": c})
        exp["s"][r["title"]] = r
        return r

    def log(self, u, exp, text, minutes_later=0, attachment_id=None):
        """A bench-log line at the simulated time."""
        later(minutes_later)
        n = CLOCK["now"]
        return self.post(u, f"/api/experiments/{exp['id']}/log", json={"text": text, "time": n.strftime("%H:%M"), "day": n.strftime("%a %-d %b %Y"),
                                                                     **({"attachment_id": attachment_id} if attachment_id else {})})

    def html(self, u, exp, title, text):
        return self.section(u, exp, title, {"html": text})

    def reaction(self, u, exp, rows, cond, rxn_smiles=None):
        if rxn_smiles is None:
            subs = ".".join(r["smiles"] for r in rows if r["role"] == "reactant")
            prods = ".".join(r["smiles"] for r in rows if r["role"] == "product")
            rxn_smiles = f"{subs}>>{prods}"
        return self.section(u, exp, "Reaction", {"reaction_smiles": rxn_smiles, "rows": rows, "conditions": cond})

    def purification(self, u, exp, **kw):
        base = {"method": "column", "stationary_phase": "", "eluent": "", "rf": "", "rf_eluent": "",
                "isolated_mg": None, "appearance": "", "notes_html": ""}
        return self.section(u, exp, "Work-up & purification", {**base, **kw})

    def safety(self, u, exp, controls, waste, emergency="", notes=""):
        return self.section(u, exp, "Safety & risk assessment", {"controls": controls, "waste": waste, "emergency": emergency,
                                                                  "notes_html": notes, "snapshot": None})

    def attach(self, u, exp, filename, data: bytes, ctype="chemical/x-jcamp-dx"):
        return self.c.post(f"/api/experiments/{exp['id']}/attachments", headers=self.h(u),
                           files={"file": (filename, data, ctype)}).json()

    def characterise(self, u, exp, smiles, label, *, solvent="CDCl3", c13=True, impurity=None, mp=None, ir=None,
                     hrms="[M + H]+", exch=None, seed=1, extra=()):
        """Simulated 1H (and 13C) spectra attached, plus the text a chemist would write for each."""
        items = []
        j1, t1 = proton(smiles, f"{label} 1H NMR ({solvent})", solvent, impurity=impurity, seed=seed, exch=exch)
        a1 = self.attach(u, exp, f"{label.replace(' ', '_')}_1H.jdx", j1.encode())
        items.append({"technique": "1H NMR", "conditions": f"400 MHz, {solvent}", "data": t1, "attachment_id": a1["id"]})
        if c13:
            j2, t2 = carbon(smiles, f"{label} 13C NMR ({solvent})", solvent, seed=seed + 1)
            a2 = self.attach(u, exp, f"{label.replace(' ', '_')}_13C.jdx", j2.encode())
            items.append({"technique": "13C NMR", "conditions": f"101 MHz, {solvent}", "data": t2, "attachment_id": a2["id"]})
        if hrms:
            ion = next(i for i in chem.hrms_ions(smiles)["ions"] if i["adduct"] == hrms)
            found = round(ion["mz"] + (0.0004 if seed % 2 else -0.0006), 4)
            items.append({"technique": "HRMS", "conditions": "ESI-TOF",
                          "data": f"m/z calcd for {ion['formula']} {hrms} {ion['mz']:.4f}; found {found:.4f}"})
        if ir:
            items.append({"technique": "IR", "conditions": "ATR, neat", "data": ir})
        if mp:
            items.append({"technique": "mp", "conditions": "", "data": mp})
        items += list(extra)
        self.section(u, exp, "Characterisation", {"items": items})
        return items

    def deduct(self, u, exp, *pairs):
        """Take amounts from lots and link them to the experiment (name, amount, unit)."""
        for name, amount, unit in pairs:
            m = self.mat[name]
            self.post(u, f"/api/lots/{m['lot_ids'][0]}/transactions",
                      json={"amount": amount, "unit": unit, "experiment_id": exp["id"], "note": f"used in {exp['eid']}"})

    def sign(self, u, exp, comment=""):
        later(20)
        return self.post(u, f"/api/experiments/{exp['id']}/sign", json={"password": PW, "comment": comment})

    def witness(self, u, exp, comment="", days=1.0):
        CLOCK["now"] += timedelta(days=days)
        return self.post(u, f"/api/experiments/{exp['id']}/witness", json={"password": PW, "comment": comment})

    def reject(self, u, exp, reason, days=1.0):
        CLOCK["now"] += timedelta(days=days)
        return self.post(u, f"/api/experiments/{exp['id']}/reject", json={"password": PW, "comment": reason})

    def comment(self, u, exp, body, section=None, parent=None, minutes=45):
        later(minutes)
        js = {"body": body}
        if section:
            js["entry_id"] = exp["s"][section]["id"]
        if parent:
            js["parent_id"] = parent
        return self.post(u, f"/api/experiments/{exp['id']}/comments", json=js)


# ====================================================================== the story

# Sections filled (or removed, where they don't apply) as soon as an experiment is created,
# keyed by part of its title.
EXTRA: dict[str, dict] = {
    "base × solvent": {"Procedure": "<p>Nine 4 mL vials from stock solutions (bromide + boronic acid in the organic solvent; Pd(PPh<sub>3</sub>)<sub>4</sub> added as a solid in the glovebox); base weighed per vial; degassed water added last. Heated in a block at 90 °C for 16 h. Yields by UPLC against 1,3,5-trimethoxybenzene.</p>",
                       "_remove": ["Work-up & purification", "Characterisation"]},
    "1-Methylindole": {"Aim": "<p>1-Methylindole as a starting material for C3-functionalisation studies; also a reference run for NaH/DMF alkylations in the group.</p>"},
    "ester hydrolysis": {"Aim": "<p>Step 2 of 3 for the KX series: saponify the biaryl methyl ester to the acid used in every amide coupling.</p>"},
    "contaminated": {"Aim": "<p>Scope entry: 1-bromo-4-fluorobenzene under the group SOP.</p>"},
    "Aspirin": {"Aim": "<p>Demonstration prep for the second-year lab: O-acetylation of salicylic acid; shows recrystallisation and the FeCl<sub>3</sub> purity test.</p>",
                "Observations & results": "<p>78% after one recrystallisation; typical student yields 50–70%. mp sharp (134–136 °C).</p>"},
    "Paracetamol": {"Aim": "<p>Demonstration prep: chemoselective N-acetylation of 4-aminophenol in water (amine over phenol).</p>",
                    "Observations & results": "<p>72%. Crude was pink (oxidised aminophenol); charcoal removes it. Waiting for Maria to witness.</p>"},
    "KX-105": {"Characterisation": {"items": [{"technique": "LC-MS", "conditions": "ESI+, C18, 5→95% MeCN, 254 nm",
                                               "data": "24 h: product m/z 303.1 [M+H]+, 12% UV area; HOAt active ester m/z 318.1, 71%; acid 9%"}]}},
    "Suzuki step, 1 g": {"Procedure": "<p>Exactly as in Priya's KX core record (group SOP, dioxane/water 4:1, 90 °C), on 1.00 g of bromide in a 100 mL flask.</p>"},
    "KX-106 Boc": {"Aim": "<p>Piperazine amide (KX-106) via the Boc-protected amine; the free NH is for later sulfonamide and urea analogues.</p>",
                   "Observations & results": "<p>85%. Rotamers broaden the piperazine CH<sub>2</sub> signals at rt (sharpen at 80 °C in DMSO-d<sub>6</sub>).</p>"},
    "Boc removal": {"Aim": "<p>Remove the Boc group to give KX-106 as the free base.</p>"},
    "KX-107": {"Aim": "<p>Benzylic amine analogue (no amide): does the carbonyl matter for activity? Reductive amination of the biaryl aldehyde with morpholine.</p>"},
    "Buchwald–Hartwig amination screen": {"_remove": ["Work-up & purification", "Characterisation"]},
    "Ibuprofen reference": {"Procedure": "<p>Commercial ibuprofen (20 mg) in CDCl<sub>3</sub> (0.6 mL). Bruker 600 MHz, zg30 (16 scans) and zgpg30 (1024 scans). Exported the raw experiment folders as zips and dropped them into the Characterisation section; Benchtop processed the FIDs.</p>",
                            "_remove": ["Work-up & purification"]},
    "5 mmol (RuPhos": {"Aim": "<p>Scale the best screen conditions (RuPhos/NaOtBu) to 5 mmol with half the Pd, to check it holds up before using it on KX substrates.</p>"},
    "Repeat of KX-102": {"Work-up & purification": {"method": "column", "stationary_phase": "Silica gel", "eluent": "0→8% MeOH/DCM", "rf": "0.30", "rf_eluent": "5% MeOH/DCM",
                                                     "isolated_mg": None, "appearance": "White solid", "notes_html": "<p>EtOAc / NaHCO<sub>3</sub> / 5% LiCl work-up (emulsion in the 2nd LiCl wash; added brine), then column.</p>"}},
    "CuAAC": {"Aim": "<p>Model CuAAC with the benzyl azide stock: check the azide solution's titre and the standard CuSO<sub>4</sub>/ascorbate conditions before the library.</p>"},
}

def run(c: TestClient) -> Lab:
    lab = Lab(c)
    at(92, 9)
    r = c.post("/api/setup", json={"full_name": "Hannah Weiss", "username": "hweiss", "email": "h.weiss@ruizlab.example",
                                   "password": PW, "load_demo": False})
    assert r.status_code == 201, r.text
    for u, name, role, _ in PEOPLE[1:]:
        lab.post("hweiss", "/api/users", json={"username": u, "password": PW, "full_name": name, "role": role,
                                               "email": f"{u}@ruizlab.example", "must_change": False})
        later(3)
    lab.uid = {x["username"]: x["id"] for x in lab.get("hweiss", "/api/users")}

    # ---------------------------------------------------------------- groups
    ruiz = lab.post("hweiss", "/api/groups", json={"name": "Ruiz group", "description": "Synthetic methodology and medicinal chemistry"})
    proc = lab.post("hweiss", "/api/groups", json={"name": "Process development", "description": "Route scouting and scale-up"})
    lab.put("hweiss", f"/api/groups/{ruiz['id']}/members/{lab.uid['mruiz']}", json={"role": "lead"})
    for u in ("pnair", "dcho", "slaurent", "amorgan"):
        lab.put("mruiz", f"/api/groups/{ruiz['id']}/members/{lab.uid[u]}", json={"role": "member"})
    lab.put("hweiss", f"/api/groups/{proc['id']}/members/{lab.uid['kwatanabe']}", json={"role": "lead"})
    lab.put("kwatanabe", f"/api/groups/{proc['id']}/members/{lab.uid['pnair']}", json={"role": "member"})
    lab.put("kwatanabe", f"/api/groups/{proc['id']}/members/{lab.uid['mruiz']}", json={"role": "member"})

    # ---------------------------------------------------------------- reagents and solvents
    L = lab.material
    A = "Shelf A"; B = "Cabinet B (bases)"; F = "Flammables cabinet"; G = "Glovebox"; R = "Fridge 2 (4 °C)"; D = "Desiccator"
    L("Methyl 4-bromobenzoate", "COC(=O)c1ccc(Br)cc1", "619-42-1", lots=[(100, "g", A + "1", "Combi-Blocks", 98)])
    L("4-Pyridinylboronic acid", "OB(O)c1ccncc1", "1692-15-5", lots=[(25, "g", R, "Combi-Blocks", 97)])
    L("Phenylboronic acid", "OB(O)c1ccccc1", "98-80-6", lots=[(25, "g", A + "3", "Sigma-Aldrich", 97)])
    L("4-Bromoanisole", "COc1ccc(Br)cc1", "104-92-7", density=1.494, lots=[(25, "g", A + "2", "Sigma-Aldrich", 99)])
    L("4-Bromobenzonitrile", "N#Cc1ccc(Br)cc1", "623-00-7", lots=[(10, "g", A + "2", "Oakwood", 99)])
    L("2-Bromotoluene", "Cc1ccccc1Br", "95-46-5", density=1.422, lots=[(25, "g", A + "2", "TCI", 98)])
    L("4'-Bromoacetophenone", "CC(=O)c1ccc(Br)cc1", "99-90-1", lots=[(25, "g", A + "2", "Sigma-Aldrich", 98)])
    L("3-Bromopyridine", "Brc1cccnc1", "626-55-1", density=1.64, lots=[(10, "g", A + "2", "Oakwood", 99)])
    L("4-Bromo-N,N-dimethylaniline", "CN(C)c1ccc(Br)cc1", "586-77-6", lots=[(10, "g", A + "2", "TCI", 98)])
    L("1-Bromo-4-nitrobenzene", "O=[N+]([O-])c1ccc(Br)cc1", "586-78-7", lots=[(25, "g", A + "2", "Sigma-Aldrich", 99)])
    L("Tetrakis(triphenylphosphine)palladium(0)",
      "c1ccc(cc1)P(c1ccccc1)c1ccccc1.c1ccc(cc1)P(c1ccccc1)c1ccccc1.c1ccc(cc1)P(c1ccccc1)c1ccccc1.c1ccc(cc1)P(c1ccccc1)c1ccccc1.[Pd]",
      "14221-01-3", synonyms="Pd(PPh3)4", lots=[(1.5, "g", G, "Strem", 99)])
    L("Palladium(II) acetate", "CC(=O)[O-].CC(=O)[O-].[Pd+2]", "3375-31-3", synonyms="Pd(OAc)2", lots=[(1, "g", G, "Strem", 98)])
    L("XPhos", "CC(C)c1cc(C(C)C)c(-c2ccccc2P(C2CCCCC2)C2CCCCC2)c(C(C)C)c1", "564483-18-7", lots=[(1, "g", G, "Sigma-Aldrich", 98)])
    L("SPhos", "COc1cccc(OC)c1-c1ccccc1P(C1CCCCC1)C1CCCCC1", "657408-07-6", lots=[(1, "g", G, "Sigma-Aldrich", 98)])
    L("RuPhos", "CC(C)Oc1cccc(OC(C)C)c1-c1ccccc1P(C1CCCCC1)C1CCCCC1", "787618-22-8", lots=[(1, "g", G, "Sigma-Aldrich", 98)])
    L("rac-BINAP", "c1ccc(cc1)P(c1ccccc1)c1ccc2ccccc2c1-c1c(ccc2ccccc12)P(c1ccccc1)c1ccccc1", "98327-87-8", lots=[(1, "g", G, "Strem", 98)])
    L("Potassium carbonate", "[K+].[K+].[O-]C([O-])=O", "584-08-7", lots=[(1000, "g", B, "Fisher", 99)])
    L("Caesium carbonate", "[Cs+].[Cs+].[O-]C([O-])=O", "534-17-8", lots=[(100, "g", D, "Sigma-Aldrich", 99)])
    L("Potassium phosphate tribasic", "[K+].[K+].[K+].[O-]P([O-])([O-])=O", "7778-53-2", lots=[(500, "g", B, "Sigma-Aldrich", 98)])
    L("Sodium tert-butoxide", "CC(C)(C)[O-].[Na+]", "865-48-5", lots=[(25, "g", G, "Sigma-Aldrich", 97)])
    L("Lithium hydroxide monohydrate", "[Li+].[OH-].O", "1310-66-3", lots=[(250, "g", B, "Sigma-Aldrich", 98)])
    L("HATU", "CN(C)C(=[N+](C)C)On1nnc2cccnc21.F[P-](F)(F)(F)(F)F", "148893-10-1", lots=[(25, "g", R, "Oakwood", 98)])
    L("N,N-Diisopropylethylamine", "CCN(C(C)C)C(C)C", "7087-68-5", density=0.742, synonyms="DIPEA, Hünig's base", lots=[(500, "mL", F, "Sigma-Aldrich", 99)])
    L("3-(Trifluoromethyl)aniline", "Nc1cccc(C(F)(F)F)c1", "98-16-8", density=1.29, lots=[(25, "g", A + "4", "Oakwood", 99)])
    L("4-Fluoroaniline", "Nc1ccc(F)cc1", "371-40-4", density=1.173, lots=[(25, "g", A + "4", "Sigma-Aldrich", 99)])
    L("Morpholine", "C1COCCN1", "110-91-8", density=1.007, lots=[(500, "mL", F, "Sigma-Aldrich", 99)])
    L("Cyclopropylamine", "NC1CC1", "765-30-0", density=0.824, lots=[(25, "mL", R, "Sigma-Aldrich", 98)])
    L("2,6-Dimethylaniline", "Cc1cccc(C)c1N", "87-62-7", density=0.984, lots=[(25, "g", A + "4", "TCI", 99)])
    L("tert-Butyl piperazine-1-carboxylate", "CC(C)(C)OC(=O)N1CCNCC1", "57260-71-6", synonyms="1-Boc-piperazine", lots=[(25, "g", A + "4", "Combi-Blocks", 98)])
    L("Trifluoroacetic acid", "OC(=O)C(F)(F)F", "76-05-1", density=1.489, synonyms="TFA", lots=[(100, "mL", "Acid cabinet", "Sigma-Aldrich", 99)])
    L("4-(Pyridin-4-yl)benzaldehyde", "O=Cc1ccc(-c2ccncc2)cc1", "99163-12-9", lots=[(5, "g", R, "Enamine", 97)])
    L("Sodium triacetoxyborohydride", "CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]", "56553-60-7", synonyms="STAB", lots=[(25, "g", D, "Sigma-Aldrich", 97)])
    L("Acetic acid", "CC(=O)O", "64-19-7", density=1.049, lots=[(1, "L", "Acid cabinet", "Fisher", 99.7)])
    L("Salicylic acid", "O=C(O)c1ccccc1O", "69-72-7", lots=[(500, "g", A + "5", "Fisher", 99)])
    L("Acetic anhydride", "CC(=O)OC(C)=O", "108-24-7", density=1.08, lots=[(500, "mL", F, "Fisher", 99)])
    L("Phosphoric acid (85%)", "OP(=O)(O)O", "7664-38-2", density=1.685, lots=[(500, "mL", "Acid cabinet", "Fisher", 85)])
    L("4-Aminophenol", "Nc1ccc(O)cc1", "123-30-8", lots=[(100, "g", A + "5", "Sigma-Aldrich", 98)])
    L("Indole", "c1ccc2[nH]ccc2c1", "120-72-9", lots=[(25, "g", A + "6", "Sigma-Aldrich", 99)])
    L("Sodium hydride (60% in mineral oil)", "[H-].[Na+]", "7646-69-7", lots=[(25, "g", G, "Sigma-Aldrich", 60, 18)])
    L("Iodomethane", "CI", "74-88-4", density=2.28, synonyms="Methyl iodide, MeI", lots=[(25, "mL", R, "Sigma-Aldrich", 99)])
    L("Benzyl bromide", "BrCc1ccccc1", "100-39-0", density=1.438, lots=[(100, "mL", F, "Sigma-Aldrich", 98)])
    L("Sodium azide", "[N-]=[N+]=[N-].[Na+]", "26628-22-8", lots=[(25, "g", "Toxics cabinet", "Sigma-Aldrich", 99.5)])
    L("Phenylacetylene", "C#Cc1ccccc1", "536-74-3", density=0.93, lots=[(25, "mL", R, "Sigma-Aldrich", 98)])
    L("Copper(II) sulfate", "O=S(=O)([O-])[O-].[Cu+2]", "7758-98-7", lots=[(250, "g", B, "Fisher", 99)])
    L("Sodium L-ascorbate", "OC[C@H](O)[C@H]1OC(=O)C([O-])=C1O.[Na+]", "134-03-2", lots=[(100, "g", A + "5", "Sigma-Aldrich", 98)])
    for name, smi, cas, d, amt in [("1,4-Dioxane", "C1COCCO1", "123-91-1", 1.033, 2.5), ("Toluene", "Cc1ccccc1", "108-88-3", 0.867, 4),
                                   ("N,N-Dimethylformamide", "CN(C)C=O", "68-12-2", 0.944, 4), ("Dichloromethane", "ClCCl", "75-09-2", 1.326, 4),
                                   ("Tetrahydrofuran", "C1CCOC1", "109-99-9", 0.889, 4), ("Methanol", "CO", "67-56-1", 0.792, 4),
                                   ("Ethyl acetate", "CCOC(C)=O", "141-78-6", 0.902, 20), ("1,2-Dichloroethane", "ClCCCl", "107-06-2", 1.253, 1),
                                   ("2-Methyltetrahydrofuran", "CC1CCCO1", "96-47-9", 0.854, 20), ("tert-Butanol", "CC(C)(C)O", "75-65-0", 0.775, 1),
                                   ("Water (deionised)", "O", "7732-18-5", 1.0, 20)]:
        L(name, smi, cas, kind="solvent", density=d, lots=[(amt, "L", "Solvent store" if amt >= 4 else F, "Fisher", 99.8)])

    # ---------------------------------------------------------------- notebooks and templates
    NB = {}
    NB["methods"] = lab.post("dcho", "/api/notebooks", json={"name": "Pd cross-coupling methodology", "group_id": ruiz["id"], "visibility": "group",
                                                             "description": "Suzuki scope and Buchwald–Hartwig conditions (Daniel Cho)"})["id"]
    NB["kx"] = lab.post("pnair", "/api/notebooks", json={"name": "KX-series kinase inhibitor analogues", "group_id": ruiz["id"], "visibility": "group",
                                                         "description": "4-(Pyridin-4-yl)benzamides: synthesis of KX-101 and analogues (Priya Nair)"})["id"]
    NB["proc"] = lab.post("kwatanabe", "/api/notebooks", json={"name": "KX-101 route: scale-up", "group_id": proc["id"], "visibility": "group",
                                                               "description": "Suzuki step from 1 g to 50 g; solvent swap, Pd removal, IPC (Kenji Watanabe)"})["id"]
    NB["sophie"] = lab.post("slaurent", "/api/notebooks", json={"name": "Heterocycles & click chemistry", "group_id": ruiz["id"], "visibility": "group",
                                                                "description": "Indole N-alkylation, azides and CuAAC (Sophie Laurent)"})["id"]
    NB["alex"] = lab.post("amorgan", "/api/notebooks", json={"name": "Alex: undergraduate research", "group_id": ruiz["id"], "visibility": "private",
                                                             "description": "Training reactions and reference spectra; private to Alex and the group's lead"})["id"]
    NB["teach"] = lab.post("slaurent", "/api/notebooks", json={"name": "Teaching lab demonstrations", "visibility": "everyone",
                                                               "description": "Classic preparations used in second-year labs; open to everyone"})["id"]

    later(60)
    tpl_suzuki = lab.post("dcho", "/api/experiments", json={"title": "Suzuki–Miyaura coupling (group SOP)", "is_template": True,
                                                            "description": "Standard conditions: Pd(PPh3)4 3 mol%, K2CO3 2 equiv, dioxane/water 4:1, 90 °C"})
    t = lab.get("dcho", f"/api/experiments/{tpl_suzuki['id']}")
    ts = {e["title"]: e for e in t["entries"]}
    lab.put("dcho", f"/api/entries/{ts['Procedure']['id']}", json={"content": {"html":
        "<ol><li>Charge an oven-dried vial with the aryl bromide (1.0 equiv), boronic acid (1.2 equiv), K<sub>2</sub>CO<sub>3</sub> (2.0 equiv) and Pd(PPh<sub>3</sub>)<sub>4</sub> (3 mol%).</li>"
        "<li>Evacuate and backfill with N<sub>2</sub> (×3). Add degassed 1,4-dioxane/water (4:1, 0.2 M).</li>"
        "<li>Heat at 90 °C for 16 h. Monitor by TLC (10% EtOAc/hexanes) or LC-MS.</li>"
        "<li>Cool, dilute with EtOAc, wash with water and brine, dry (MgSO<sub>4</sub>), concentrate.</li>"
        "<li>Purify by flash chromatography.</li></ol>"}})
    lab.put("dcho", f"/api/entries/{ts['Safety & risk assessment']['id']}", json={"content": {
        "controls": "Fume hood; nitrile gloves; degas solvents; Pd waste kept separate.", "waste": "Aqueous: basic aqueous waste. Organic: halogenated if DCM used. Pd residues: heavy-metal waste.",
        "emergency": "", "notes_html": "", "snapshot": None}})
    tpl_amide = lab.post("pnair", "/api/experiments", json={"title": "HATU amide coupling", "is_template": True,
                                                            "description": "Acid (1 equiv), amine (1.1 equiv), HATU (1.2 equiv), DIPEA (3 equiv), DMF 0.2 M, rt"})
    t = lab.get("pnair", f"/api/experiments/{tpl_amide['id']}")
    ts = {e["title"]: e for e in t["entries"]}
    lab.put("pnair", f"/api/entries/{ts['Procedure']['id']}", json={"content": {"html":
        "<ol><li>Dissolve the acid in DMF (0.2 M); add DIPEA (3.0 equiv) then HATU (1.2 equiv); stir 10 min (activation, yellow).</li>"
        "<li>Add the amine (1.1 equiv); stir at rt, 2–16 h. LC-MS for conversion.</li>"
        "<li>Dilute with EtOAc; wash with sat. NaHCO<sub>3</sub>, 5% LiCl (×3, removes DMF), brine; dry, concentrate.</li>"
        "<li>Purify (column or trituration from Et<sub>2</sub>O).</li></ol>"}})

    # everyone's work, interleaved in time: each story yields (days_ago, hour) before its next step,
    # and the clock only ever moves forward, so the audit trail reads chronologically
    stories = [_methods(lab, NB, tpl_suzuki["id"]), _kx(lab, NB, tpl_amide["id"]), _process(lab, NB), _sophie(lab, NB), _students(lab, NB)]
    def when(step):
        d, hr = step
        return (TODAY - timedelta(days=d)).replace(hour=int(hr), minute=int(round(hr % 1 * 60)))
    queue = [(when(st), i, g) for i, g in enumerate(stories) if (st := next(g, None)) is not None]
    while queue:
        queue.sort(key=lambda q: (q[0], q[1]))
        t, i, g = queue.pop(0)
        CLOCK["now"] = max(CLOCK["now"], t)
        st = next(g, None)
        if st is not None:
            queue.append((when(st), i, g))

    # pins and preferences, as people would have set them
    CLOCK["now"] = max(CLOCK["now"], TODAY - timedelta(minutes=30))
    for u, ids in lab.pins.items():
        lab.patch(u, "/api/auth/me", json={"preferences": {"pinned": ids}})
    return lab


# ---------------------------------------------------------------- Daniel: methodology
def _methods(lab: Lab, NB, tpl):
    nb = NB["methods"]
    # conditions screen as a screening grid
    yield (86, 10)
    e = lab.experiment("dcho", nb, "Suzuki conditions screen: base × solvent", tags=["suzuki", "screening", "optimisation"],
                       description="Methyl 4-bromobenzoate + PhB(OH)2, Pd(PPh3)4 3 mol%, 90 °C, 16 h, 0.5 mmol; LC yields vs. internal standard")
    lab.html("dcho", e, "Aim", "<p>Find robust conditions for the Suzuki step before the substrate-scope study and the KX-101 route. Three bases × three solvent systems, everything else fixed.</p>")
    lab.reaction("dcho", e, [
        lab.row("Methyl 4-bromobenzoate", "reactant", basis="mass", mass_mg=107.5, limiting=True),
        lab.row("Phenylboronic acid", "reactant", basis="equiv", equiv=1.2, purity=97),
        lab.row("Potassium carbonate", "reagent", basis="equiv", equiv=2.0),
        lab.row("Tetrakis(triphenylphosphine)palladium(0)", "catalyst", basis="equiv", equiv=0.03),
        lab.row("1,4-Dioxane", "solvent", basis="fill"),
        lab.product("Methyl biphenyl-4-carboxylate", "COC(=O)c1ccc(-c2ccccc2)cc1", "COC(=O)c1ccc(Br)cc1", 107.5, None),
    ], {"temperature_c": 90, "time_h": 16, "atmosphere": "N2", "target_conc_m": 0.2, "notes": "Each cell is one 0.5 mmol vial; base and solvent as in the grid"})
    lab.add_section("dcho", e, "screen", "Base × solvent screen", {
        "x_name": "Solvent (4:1 with water)", "y_name": "Base (2.0 equiv)", "x_levels": ["1,4-Dioxane", "Toluene", "DMF"],
        "y_levels": ["K2CO3", "Cs2CO3", "K3PO4"], "response": "LC yield (%)", "higher_is_better": True,
        "cells": {"0,0": {"value": 92}, "0,1": {"value": 71}, "0,2": {"value": 64, "note": "dark, some homocoupling"},
                  "1,0": {"value": 95}, "1,1": {"value": 80}, "1,2": {"value": 58},
                  "2,0": {"value": 89}, "2,1": {"value": 77}, "2,2": {"value": 49, "note": "ester hydrolysis seen"}}})
    lab.html("dcho", e, "Observations & results", "<p>Dioxane/water is clearly best. Cs<sub>2</sub>CO<sub>3</sub> edges K<sub>2</sub>CO<sub>3</sub> (95 vs 92%) but costs ~20× more, so <b>K<sub>2</sub>CO<sub>3</sub>/dioxane–water</b> becomes the group SOP. DMF gives partial ester hydrolysis with K<sub>3</sub>PO<sub>4</sub>.</p>")
    lab.sign("dcho", e, "Screen complete")
    lab.witness("mruiz", e, "Agreed: K2CO3/dioxane-water as SOP", days=2)
    lab.pins["dcho"] = [e["id"]]

    # substrate scope, same SOP
    scope_ids: list[int] = []
    scope = [
        ("4-Bromoanisole", "COc1ccc(Br)cc1", "4-Methoxybiphenyl", "COc1ccc(-c2ccccc2)cc1", 187.0, 84, "White solid", "86–88 °C", "0.45 (10% EtOAc/hexanes)", "", 1),
        ("4-Bromobenzonitrile", "N#Cc1ccc(Br)cc1", "Biphenyl-4-carbonitrile", "N#Cc1ccc(-c2ccccc2)cc1", 182.0, 91, "White solid", "85–86 °C", "0.40 (10% EtOAc/hexanes)", "", 2),
        ("4'-Bromoacetophenone", "CC(=O)c1ccc(Br)cc1", "4-Acetylbiphenyl", "CC(=O)c1ccc(-c2ccccc2)cc1", 199.0, 88, "Off-white solid", "119–121 °C", "0.30 (10% EtOAc/hexanes)", "", 3),
        ("1-Bromo-4-nitrobenzene", "O=[N+]([O-])c1ccc(Br)cc1", "4-Nitrobiphenyl", "O=[N+]([O-])c1ccc(-c2ccccc2)cc1", 202.0, 93, "Pale yellow solid", "112–114 °C", "0.42 (10% EtOAc/hexanes)", "", 4),
        ("3-Bromopyridine", "Brc1cccnc1", "3-Phenylpyridine", "c1ccc(-c2cccnc2)cc1", 158.0, 71, "Colourless oil", None, "0.25 (30% EtOAc/hexanes)", "Pyridine product streaks on silica; 1% Et3N in eluent helped.", 5),
        ("2-Bromotoluene", "Cc1ccccc1Br", "2-Methylbiphenyl", "Cc1ccccc1-c1ccccc1", 171.0, 63, "Colourless oil", None, "0.60 (hexanes)", "Ortho-substituted: slower (55% conversion at 6 h). Full conversion needs 24 h; some protodeboronation of PhB(OH)2.", 6),
        ("4-Bromo-N,N-dimethylaniline", "CN(C)c1ccc(Br)cc1", "4-(Dimethylamino)biphenyl", "CN(C)c1ccc(-c2ccccc2)cc1", 200.0, 45, "White solid", "118–120 °C", "0.50 (10% EtOAc/hexanes)", "Electron-rich bromide: slow oxidative addition. Recovered 38% starting material. Try SPhos/Pd(OAc)2 next.", 7),
    ]
    day = 78
    for arbr, arbr_smi, prod, prod_smi, mg, yld, look, mp, rf, note, seed in scope:
        yield (day, 9.5)
        e = lab.experiment("dcho", nb, f"Suzuki scope: {prod}", tags=["suzuki", "scope", "biaryl"], template=tpl,
                           description=f"{arbr} + PhB(OH)2, group SOP, 1.0 mmol")
        lab.html("dcho", e, "Aim", f"<p>Substrate scope entry: {arbr} under the group SOP (K<sub>2</sub>CO<sub>3</sub>, Pd(PPh<sub>3</sub>)<sub>4</sub>, dioxane/water, 90 °C).</p>")
        p = lab.product(prod, prod_smi, arbr_smi, mg, yld)
        lab.reaction("dcho", e, [
            lab.row(arbr, "reactant", basis="mass", mass_mg=mg, limiting=True),
            lab.row("Phenylboronic acid", "reactant", basis="equiv", equiv=1.2, purity=97),
            lab.row("Potassium carbonate", "reagent", basis="equiv", equiv=2.0),
            lab.row("Tetrakis(triphenylphosphine)palladium(0)", "catalyst", basis="equiv", equiv=0.03),
            lab.row("1,4-Dioxane", "solvent", basis="fill"),
            lab.row("Water (deionised)", "solvent", basis="volume", volume_ml=1.0),
            p], {"temperature_c": 90, "time_h": 24 if "Bromotoluene" in arbr else 16, "atmosphere": "N2", "target_conc_m": 0.2})
        lab.purification("dcho", e, method="column", stationary_phase="Silica gel (40–63 µm)",
                         eluent="0→10% EtOAc/hexanes" if "pyridine" not in prod.lower() else "20→40% EtOAc/hexanes + 1% Et3N",
                         rf=rf.split(" ")[0], rf_eluent=rf.split("(")[1].rstrip(")"), isolated_mg=p["actual_mass_mg"], appearance=look,
                         notes_html="<p>Diluted with EtOAc (20 mL), washed with water and brine, dried over MgSO<sub>4</sub>, concentrated onto silica.</p>")
        lab.characterise("dcho", e, prod_smi, prod, c13=seed in (1, 2, 5), mp=mp, seed=seed,
                         ir={"Biphenyl-4-carbonitrile": "2226 (C≡N), 1604, 1484, 766 cm⁻¹", "4-Acetylbiphenyl": "1680 (C=O), 1601, 1265, 765 cm⁻¹",
                             "4-Nitrobiphenyl": "1513, 1343 (NO2), 853, 740 cm⁻¹"}.get(prod))
        lab.html("dcho", e, "Observations & results",
                 f"<p>{yld}% isolated. {note or 'Clean conversion by TLC after 16 h; the mixture turned dark brown (Pd black) near the end, as usual.'}</p>")
        lab.deduct("dcho", e, (arbr, mg, "mg"), ("Phenylboronic acid", 151, "mg"), ("Tetrakis(triphenylphosphine)palladium(0)", 35, "mg"))
        lab.sign("dcho", e)
        if seed <= 5:
            lab.witness("slaurent" if seed % 2 else "mruiz", e, days=1.5)
        day -= 3 if seed < 6 else 2
        scope_ids.append(e["id"])

    # the paper's supporting information (0.33), started from the scope entries
    si = lab.c.post("/api/si-documents", headers=lab.h("dcho"), json={"title": "Suzuki couplings of aryl bromides under the group SOP"}).json()
    lab.c.put(f"/api/si-documents/{si['id']}", headers=lab.h("dcho"), json={
        "expected_version": si["version"], "items": [{"experiment_id": i, "number": f"3{'abcdefg'[k]}"} for k, i in enumerate(scope_ids)],
        "general": "All reactions were run under nitrogen in oven-dried vials. Reagents were used as received; 1,4-dioxane was dried over "
                   "4 Å molecular sieves. Flash column chromatography used silica gel (40–63 µm).\n\n"
                   "NMR spectra were recorded at 298 K on a 400 MHz spectrometer and referenced to the residual solvent signal."})

    # a voided record
    yield (57, 15)
    e = lab.experiment("dcho", nb, "Suzuki scope: 4-fluorobiphenyl (contaminated boronic acid lot)", tags=["suzuki", "scope"], template=tpl)
    lab.html("dcho", e, "Observations & results", "<p>Product contaminated with ~30% biphenyl. Traced to a phenylboronic acid lot from the shared shelf that had partly decomposed (old bottle, not this registry lot). Voiding; will repeat with fresh material.</p>")
    lab.sign("dcho", e)
    later(60)
    lab.post("dcho", f"/api/experiments/{e['id']}/void", json={"password": PW, "comment": "Contaminated starting material; results not valid. Repeat planned."})

    # Buchwald–Hartwig screen: ligand × base
    yield (24, 10)
    e = lab.experiment("dcho", nb, "Buchwald–Hartwig amination screen: ligand × base", tags=["buchwald-hartwig", "c-n coupling", "screening"],
                       description="4-Bromoanisole + morpholine, Pd(OAc)2 2 mol%, ligand 4 mol%, toluene, 100 °C, 18 h, 0.25 mmol")
    lab.html("dcho", e, "Aim", "<p>C–N coupling for the next KX analogues needs an amination that tolerates heteroaryls. Model reaction: 4-bromoanisole + morpholine. Screen four ligands against three bases.</p>")
    lab.reaction("dcho", e, [
        lab.row("4-Bromoanisole", "reactant", basis="mass", mass_mg=46.8, limiting=True),
        lab.row("Morpholine", "reactant", basis="equiv", equiv=1.2),
        lab.row("Sodium tert-butoxide", "reagent", basis="equiv", equiv=1.4),
        lab.row("Palladium(II) acetate", "catalyst", basis="equiv", equiv=0.02),
        lab.row("RuPhos", "catalyst", basis="equiv", equiv=0.04),
        lab.row("Toluene", "solvent", basis="fill"),
        lab.product("4-(4-Methoxyphenyl)morpholine", "COc1ccc(N2CCOCC2)cc1", "COc1ccc(Br)cc1", 46.8, None),
    ], {"temperature_c": 100, "time_h": 18, "atmosphere": "Ar", "target_conc_m": 0.25, "notes": "Set up in the glovebox; ligand and base per the grid"})
    lab.html("dcho", e, "Procedure", "<p>12 vials (1-dram, stir bars) set up in the glovebox from stock solutions: Pd(OAc)<sub>2</sub>/ligand premixed in toluene (10 min), aryl bromide and morpholine as a toluene stock, solid bases weighed directly. Sealed, heated in an aluminium block at 100 °C for 18 h. GC yields against dodecane internal standard.</p>")
    lab.add_section("dcho", e, "screen", "Ligand × base screen", {
        "x_name": "Ligand", "y_name": "Base", "x_levels": ["XPhos", "SPhos", "RuPhos", "BINAP"], "y_levels": ["NaOtBu", "Cs2CO3", "K3PO4"],
        "response": "GC yield (%)", "higher_is_better": True,
        "cells": {"0,0": {"value": 88}, "0,1": {"value": 79}, "0,2": {"value": 96}, "0,3": {"value": 41},
                  "1,0": {"value": 72}, "1,1": {"value": 65}, "1,2": {"value": 84}, "1,3": {"value": 22},
                  "2,0": {"value": 35}, "2,1": {"value": 30}, "2,2": {"value": 51}, "2,3": {"value": 8, "note": "mostly SM"}}})
    lab.safety("dcho", e, "Glovebox for weighing NaOtBu and Pd; sealed vials; heating block behind shield.",
               "Toluene: non-halogenated organic waste. Pd residues: heavy-metal waste.", "NaOtBu is corrosive and moisture-sensitive; quench spills with isopropanol.")
    lab.html("dcho", e, "Observations & results", "<p><b>RuPhos/NaOtBu: 96%</b>, the clear winner. Weak bases are poor with every ligand at 100 °C. BINAP is not competitive. Next: 5 mmol scale with RuPhos/NaOtBu.</p>")
    lab.sign("dcho", e, "Ready for review")
    lab.comment("mruiz", e, "Nice and clean. Did you check the Cs2CO3 runs at 110 °C? For base-sensitive KX substrates NaOtBu may be too harsh.", minutes=600)
    th = lab.comment("dcho", e, "@mruiz Not yet. I'll run RuPhos/Cs2CO3 at 110 °C alongside the scale-up.", minutes=90)

    yield (6, 11)
    e2 = lab.experiment("dcho", nb, "4-(4-Methoxyphenyl)morpholine, 5 mmol (RuPhos/NaOtBu)", tags=["buchwald-hartwig", "scale-up"],
                        description="Best screen conditions at 5 mmol")
    p = lab.product("4-(4-Methoxyphenyl)morpholine", "COc1ccc(N2CCOCC2)cc1", "COc1ccc(Br)cc1", 935.0, 92)
    lab.reaction("dcho", e2, [
        lab.row("4-Bromoanisole", "reactant", basis="mass", mass_mg=935.0, limiting=True),
        lab.row("Morpholine", "reactant", basis="equiv", equiv=1.2),
        lab.row("Sodium tert-butoxide", "reagent", basis="equiv", equiv=1.4),
        lab.row("Palladium(II) acetate", "catalyst", basis="equiv", equiv=0.01),
        lab.row("RuPhos", "catalyst", basis="equiv", equiv=0.02),
        lab.row("Toluene", "solvent", basis="fill"), p], {"temperature_c": 100, "time_h": 14, "atmosphere": "Ar", "target_conc_m": 0.5})
    lab.html("dcho", e2, "Procedure", "<p>Pd loading halved to 1 mol% for the scale-up. Charged solids in the glovebox, added toluene and morpholine outside under Ar.</p>")
    lab.plugin_section("dcho", e2, "glovebox-log/glovebox", {
        "box": "MBraun LABstar (Rm 214)", "date": (TODAY - timedelta(days=6)).strftime("%Y-%m-%dT10:40"), "o2": 0.6, "h2o": 1.2, "cycles": 3,
        "catalyst_ok": True, "notes": "NaOtBu bottle nearly empty; new one ordered."}, {"transfers": [
            {"direction": "In", "item": "Pd(OAc)2 (MAT lot), RuPhos", "amount": "11 mg, 47 mg"},
            {"direction": "In", "item": "Sodium tert-butoxide", "amount": "673 mg"},
            {"direction": "Out", "item": "Sealed 40 mL vial with solids", "amount": "1"}]})
    lab.purification("dcho", e2, method="column", stationary_phase="Silica gel", eluent="10→20% EtOAc/hexanes", rf="0.35", rf_eluent="20% EtOAc/hexanes",
                     isolated_mg=p["actual_mass_mg"], appearance="White crystalline solid")
    lab.characterise("dcho", e2, "COc1ccc(N2CCOCC2)cc1", "4-(4-Methoxyphenyl)morpholine", mp="71–73 °C", seed=11)
    lab.html("dcho", e2, "Observations & results", "<p>92% at 5 mmol with half the Pd: scales well. Draft; still need the Cs<sub>2</sub>CO<sub>3</sub>/110 °C comparison for Maria.</p>")
    lab.pins["dcho"].append(e2["id"])


# ---------------------------------------------------------------- Priya: KX series
def _kx(lab: Lab, NB, tpl_amide):
    nb = NB["kx"]
    yield (62, 9)
    e = lab.experiment("pnair", nb, "Methyl 4-(pyridin-4-yl)benzoate (KX core, Suzuki)", tags=["kx-series", "suzuki", "intermediate"])
    lab.html("pnair", e, "Aim", "<p>Make 5 g of the biaryl ester core for the KX-series amides (step 1 of 3). Group Suzuki SOP with 4-pyridinylboronic acid.</p>")
    p = lab.product("Methyl 4-(pyridin-4-yl)benzoate", "COC(=O)c1ccc(-c2ccncc2)cc1", "COC(=O)c1ccc(Br)cc1", 6450.0, 78)
    lab.reaction("pnair", e, [
        lab.row("Methyl 4-bromobenzoate", "reactant", basis="mass", mass_mg=6450.0, limiting=True, purity=98),
        lab.row("4-Pyridinylboronic acid", "reactant", basis="equiv", equiv=1.3, purity=97),
        lab.row("Potassium carbonate", "reagent", basis="equiv", equiv=2.0),
        lab.row("Tetrakis(triphenylphosphine)palladium(0)", "catalyst", basis="equiv", equiv=0.03),
        lab.row("1,4-Dioxane", "solvent", basis="fill"),
        lab.row("Water (deionised)", "solvent", basis="volume", volume_ml=30), p], {"temperature_c": 90, "time_h": 16, "atmosphere": "N2", "target_conc_m": 0.2})
    lab.html("pnair", e, "Procedure", "<p>Group Suzuki SOP in a 500 mL three-neck flask: solids charged, evacuated/backfilled with N<sub>2</sub> (×3), degassed dioxane (120 mL) and water (30 mL) added, heated at 90 °C (internal 86 °C) overnight. TLC (50% EtOAc/hexanes) showed full conversion.</p>")
    lab.purification("pnair", e, method="recrystallization", stationary_phase="", eluent="", rf="0.20", rf_eluent="50% EtOAc/hexanes",
                     isolated_mg=p["actual_mass_mg"], appearance="Off-white needles",
                     notes_html="<p>After aqueous work-up, recrystallised from hot EtOAc/heptane (1:3). Second crop added 0.4 g.</p>")
    lab.characterise("pnair", e, "COC(=O)c1ccc(-c2ccncc2)cc1", "Methyl 4-(pyridin-4-yl)benzoate", mp="104–106 °C", seed=21)
    lab.html("pnair", e, "Observations & results", "<p>78% (2 crops). Pyridylboronic acids protodeboronate, hence 1.3 equiv.</p>")
    lab.deduct("pnair", e, ("Methyl 4-bromobenzoate", 6.45, "g"), ("4-Pyridinylboronic acid", 4.8, "g"), ("Tetrakis(triphenylphosphine)palladium(0)", 1.04, "g"))
    lab.sign("pnair", e); lab.witness("mruiz", e, days=1)
    core = e

    yield (58, 10)
    e = lab.experiment("pnair", nb, "4-(Pyridin-4-yl)benzoic acid (ester hydrolysis)", tags=["kx-series", "hydrolysis", "intermediate"])
    p = lab.product("4-(Pyridin-4-yl)benzoic acid", "OC(=O)c1ccc(-c2ccncc2)cc1", "COC(=O)c1ccc(-c2ccncc2)cc1", 4000.0, 94)
    lab.reaction("pnair", e, [
        lab.row("Lithium hydroxide monohydrate", "reagent", basis="equiv", equiv=3.0),
        {"role": "reactant", "name": "Methyl 4-(pyridin-4-yl)benzoate", "smiles": "COC(=O)c1ccc(-c2ccncc2)cc1", "basis": "mass", "mass_mg": 4000.0, "limiting": True},
        lab.row("Tetrahydrofuran", "solvent", basis="volume", volume_ml=40), lab.row("Methanol", "solvent", basis="volume", volume_ml=10),
        lab.row("Water (deionised)", "solvent", basis="volume", volume_ml=20), p], {"temperature_c": None, "temperature_label": "rt", "time_h": 4, "atmosphere": "air"})
    lab.html("pnair", e, "Procedure", "<p>Ester suspended in THF/MeOH/water (4:1:2); LiOH·H<sub>2</sub>O added in one portion; clear solution after 20 min. LC-MS at 4 h: no ester left.</p>")
    lab.html("pnair", e, "Observations & results", "<p>94%, zwitterionic acid, sparingly soluble in everything except DMSO and DMF. Used directly for the amide couplings.</p>")
    lab.purification("pnair", e, method="trituration", isolated_mg=p["actual_mass_mg"], appearance="White powder",
                     notes_html="<p>Concentrated off THF/MeOH, acidified to pH 4–5 with 1 M HCl (zwitterion precipitates), filtered, washed with water and Et<sub>2</sub>O, dried at 50 °C under vacuum.</p>")
    lab.characterise("pnair", e, "OC(=O)c1ccc(-c2ccncc2)cc1", "4-(Pyridin-4-yl)benzoic acid", solvent="DMSO-d6", exch=13.1, mp="> 300 °C", seed=22)
    lab.sign("pnair", e); lab.witness("dcho", e, days=1)

    amines = [  # amine, smiles, product, product smiles, yield, notes, status, days_ago
        ("3-(Trifluoromethyl)aniline", "Nc1cccc(C(F)(F)F)c1", "KX-101", "O=C(Nc1cccc(C(F)(F)F)c1)c1ccc(-c2ccncc2)cc1", 82, "Clean; product precipitated on adding water to the DMF solution.", "witnessed", 55),
        ("4-Fluoroaniline", "Nc1ccc(F)cc1", "KX-102", "O=C(Nc1ccc(F)cc1)c1ccc(-c2ccncc2)cc1", 76, "", "witnessed", 51),
        ("Morpholine", "C1COCCN1", "KX-103", "O=C(c1ccc(-c2ccncc2)cc1)N1CCOCC1", 88, "Fast (complete in 1 h).", "witnessed", 49),
        ("Cyclopropylamine", "NC1CC1", "KX-104", "O=C(NC1CC1)c1ccc(-c2ccncc2)cc1", 71, "Volatile amine: used 1.5 equiv, sealed vial.", "signed", 46),
        ("2,6-Dimethylaniline", "Cc1cccc(C)c1N", "KX-105", "Cc1cccc(C)c1NC(=O)c1ccc(-c2ccncc2)cc1", 12, "Very slow: 12% after 24 h (LC-MS: mostly activated ester). Hindered aniline; HATU isn't enough.", "open", 43),
    ]
    made = {}
    for amine, asmi, code, psmi, yld, note, status, day in amines:
        yield (day, 9)
        acid_mg = 199.2  # 1.0 mmol
        e = lab.experiment("pnair", nb, f"{code}: amide with {amine}", tags=["kx-series", "amide", "hatu"],
                           template=tpl_amide, description=f"4-(Pyridin-4-yl)benzoic acid + {amine}, HATU, 1.0 mmol")
        lab.html("pnair", e, "Aim", f"<p>{code}: KX-series analogue from {amine}. SAR question: {'reference compound for the series' if code == 'KX-101' else 'effect of the amide substituent vs KX-101'}.</p>")
        p = lab.product(code, psmi, "OC(=O)c1ccc(-c2ccncc2)cc1", acid_mg, yld, purity=99 if yld > 50 else 95)
        rows = [{"role": "reactant", "name": "4-(Pyridin-4-yl)benzoic acid", "smiles": "OC(=O)c1ccc(-c2ccncc2)cc1", "basis": "mass", "mass_mg": acid_mg, "limiting": True},
                lab.row(amine, "reactant", basis="equiv", equiv=1.5 if "Cyclopropyl" in amine else 1.1),
                lab.row("HATU", "reagent", basis="equiv", equiv=1.2), lab.row("N,N-Diisopropylethylamine", "reagent", basis="equiv", equiv=3.0),
                lab.row("N,N-Dimethylformamide", "solvent", basis="fill"), p]
        lab.reaction("pnair", e, rows, {"temperature_label": "rt", "time_h": 24 if yld < 50 else 4, "atmosphere": "N2", "target_conc_m": 0.2})
        lab.purification("pnair", e, method="column" if code != "KX-101" else "trituration", stationary_phase="Silica gel" if code != "KX-101" else "",
                         eluent="0→8% MeOH/DCM" if code != "KX-101" else "", rf="0.30" if code != "KX-101" else "", rf_eluent="5% MeOH/DCM",
                         isolated_mg=p["actual_mass_mg"], appearance="White solid" if yld > 50 else "Beige solid",
                         notes_html="<p>Water (10 mL) added; precipitate filtered, washed with water and Et<sub>2</sub>O.</p>" if code == "KX-101" else
                         "<p>EtOAc / NaHCO<sub>3</sub> / 5% LiCl work-up, then column.</p>")
        if yld > 50:
            items = lab.characterise("pnair", e, psmi, code, solvent="DMSO-d6", exch=10.5 if "N1CCOCC1" not in psmi else None,
                                     impurity=("EtOAc", 0.05) if code == "KX-101" else None, c13=code in ("KX-101", "KX-103"),
                                     mp={"KX-101": "236–238 °C", "KX-102": "251–253 °C", "KX-103": "128–130 °C", "KX-104": "201–203 °C"}[code],
                                     ir="3305 (N–H), 1652 (C=O), 1598, 1325 (CF3), 1118 cm⁻¹" if code == "KX-101" else None, seed=30 + len(made),
                                     extra=[{"technique": "HPLC", "conditions": "C18, 5→95% MeCN/H2O (0.1% HCO2H), 10 min, 254 nm", "data": f"tR = {5.2 + len(made) * 0.4:.2f} min, {99.1 - len(made) * 0.3:.1f}% area"}])
        lab.html("pnair", e, "Observations & results", f"<p>{yld}% isolated. {note}</p>")
        lab.deduct("pnair", e, (amine, round((1.5 if "Cyclopropyl" in amine else 1.1) * chem.properties(asmi)["mw"], 1), "mg"), ("HATU", 456, "mg"))
        if status in ("witnessed", "signed"):
            lab.sign("pnair", e)
        if status == "witnessed":
            lab.witness("mruiz", e, days=1)
        made[code] = (e, p)

    kx101, kx101_p = made["KX-101"]
    # register KX-101 as a sample with a lot, and share it with the collaborator
    reg = lab.post("pnair", "/api/materials/register-product", json={"experiment_id": kx101["id"], "name": "KX-101",
                                                                      "smiles": "O=C(Nc1cccc(C(F)(F)F)c1)c1ccc(-c2ccncc2)cc1",
                                                                      "amount_mg": kx101_p["actual_mass_mg"], "purity": 99})
    yield (40, 14)
    lab.put("pnair", f"/api/experiments/{kx101['id']}/shares/{lab.uid['ohaddad']}", json={"permission": "view"})
    c1 = lab.comment("ohaddad", kx101, "@pnair First numbers: IC50 340 nM (ABL1), 2.1 µM (SRC) in our kinase panel. Could we get 20 mg more for the cell assay?", minutes=5)
    lab.comment("pnair", kx101, "@ohaddad Great, thanks! I have 180 mg left; sending 25 mg in DMSO stock (10 mM) tomorrow. Lot is in the registry under KX-101.", parent=c1["id"], minutes=120)
    lab.comment("mruiz", kx101, "Encouraging. Let's prioritise the ortho-substituted anilines next for selectivity.", parent=c1["id"], minutes=200)
    lab.patch("pnair", f"/api/comments/{c1['id']}", json={"resolved": True})
    lab.post("pnair", f"/api/lots/{reg['lot']['id']}/transactions", json={"amount": 25, "unit": "mg", "note": "sent to Haddad lab (cell assay), 10 mM DMSO"}) if reg.get("lot") else None

    # KX-105: the hindered aniline, discussion and a plan
    kx105, _ = made["KX-105"]
    yield (40, 17)
    t = lab.comment("pnair", kx105, "Stuck at 12%. @mruiz @dcho thoughts? Acid chloride or T3P? Or switch to a Buchwald with the aryl bromide + benzamide?", section="Observations & results", minutes=10)
    lab.comment("dcho", kx105, "Acid chloride (oxalyl chloride, cat. DMF) then the aniline with pyridine usually works for 2,6-disubstituted anilines.", parent=t["id"], minutes=95)
    lab.comment("mruiz", kx105, "Agree with Daniel. Keep the HATU result in the notebook though; it's a useful negative.", parent=t["id"], minutes=300)

    # Boc-piperazine amide and deprotection
    yield (39, 9)
    e = lab.experiment("pnair", nb, "KX-106 Boc: amide with 1-Boc-piperazine", tags=["kx-series", "amide", "boc"], template=tpl_amide)
    psmi = "CC(C)(C)OC(=O)N1CCN(C(=O)c2ccc(-c3ccncc3)cc2)CC1"
    p = lab.product("KX-106-Boc", psmi, "OC(=O)c1ccc(-c2ccncc2)cc1", 398.4, 85)
    lab.reaction("pnair", e, [
        {"role": "reactant", "name": "4-(Pyridin-4-yl)benzoic acid", "smiles": "OC(=O)c1ccc(-c2ccncc2)cc1", "basis": "mass", "mass_mg": 398.4, "limiting": True},
        lab.row("tert-Butyl piperazine-1-carboxylate", "reactant", basis="equiv", equiv=1.1),
        lab.row("HATU", "reagent", basis="equiv", equiv=1.2), lab.row("N,N-Diisopropylethylamine", "reagent", basis="equiv", equiv=3.0),
        lab.row("N,N-Dimethylformamide", "solvent", basis="fill"), p], {"temperature_label": "rt", "time_h": 3, "atmosphere": "N2", "target_conc_m": 0.2})
    lab.purification("pnair", e, method="column", stationary_phase="Silica gel", eluent="50→100% EtOAc/hexanes", rf="0.25", rf_eluent="EtOAc",
                     isolated_mg=p["actual_mass_mg"], appearance="White foam")
    lab.characterise("pnair", e, psmi, "KX-106-Boc", c13=False, seed=41)
    lab.sign("pnair", e); lab.witness("mruiz", e, days=1)

    yield (36, 10)
    e = lab.experiment("pnair", nb, "KX-106: Boc removal (TFA/DCM)", tags=["kx-series", "deprotection"])
    psmi2 = "O=C(c1ccc(-c2ccncc2)cc1)N1CCNCC1"
    p = lab.product("KX-106 (bis-TFA salt as free base equiv.)", psmi2, psmi, 551.0, 96, purity=97)
    lab.reaction("pnair", e, [
        {"role": "reactant", "name": "KX-106-Boc", "smiles": psmi, "basis": "mass", "mass_mg": 551.0, "limiting": True},
        lab.row("Trifluoroacetic acid", "reagent", basis="volume", volume_ml=1.5),
        lab.row("Dichloromethane", "solvent", basis="volume", volume_ml=6.0), p], {"temperature_label": "rt", "time_h": 2, "atmosphere": "air"})
    lab.html("pnair", e, "Procedure", "<p>Boc amide in DCM (6 mL) at 0 °C; TFA (1.5 mL) added dropwise; warmed to rt. Gas evolution (CO<sub>2</sub>, isobutylene) stopped after ~30 min; LC-MS complete at 2 h.</p>")
    lab.html("pnair", e, "Observations & results", "<p>96% after SCX free-basing. Free piperazine NH available for the sulfonamide/urea follow-ups.</p>")
    lab.purification("pnair", e, method="extraction", isolated_mg=p["actual_mass_mg"], appearance="Pale yellow gum → solid",
                     notes_html="<p>Concentrated, co-evaporated with toluene (×3), then passed through an SCX-2 cartridge (MeOH, then 2 M NH<sub>3</sub>/MeOH) to give the free base.</p>")
    lab.characterise("pnair", e, psmi2, "KX-106", solvent="DMSO-d6", c13=False, seed=42)
    lab.sign("pnair", e)

    # reductive amination: signed, returned with a reason
    yield (31, 9)
    e = lab.experiment("pnair", nb, "KX-107: reductive amination with morpholine (STAB)", tags=["kx-series", "reductive amination"])
    psmi = "c1cc(-c2ccncc2)ccc1CN1CCOCC1"
    p = lab.product("KX-107", psmi, "O=Cc1ccc(-c2ccncc2)cc1", 183.2, 81)
    lab.reaction("pnair", e, [
        lab.row("4-(Pyridin-4-yl)benzaldehyde", "reactant", basis="mass", mass_mg=183.2, limiting=True, purity=97),
        lab.row("Morpholine", "reactant", basis="equiv", equiv=1.2),
        lab.row("Sodium triacetoxyborohydride", "reagent", basis="equiv", equiv=1.5),
        lab.row("Acetic acid", "reagent", basis="equiv", equiv=1.0),
        lab.row("1,2-Dichloroethane", "solvent", basis="fill"), p], {"temperature_label": "rt", "time_h": 16, "atmosphere": "N2", "target_conc_m": 0.2})
    lab.html("pnair", e, "Procedure", "<p>Aldehyde and morpholine stirred in DCE with AcOH for 30 min (iminium), then STAB added portionwise. Quenched with sat. NaHCO<sub>3</sub>, extracted with DCM.</p>")
    lab.purification("pnair", e, method="column", stationary_phase="Silica gel", eluent="2→5% MeOH/DCM", rf="0.35", rf_eluent="5% MeOH/DCM",
                     isolated_mg=p["actual_mass_mg"], appearance="Colourless oil, solidified on standing")
    lab.section("pnair", e, "Characterisation", {"items": [
        {"technique": "1H NMR", "conditions": "400 MHz, CDCl3", "data": proton(psmi, "KX-107", seed=51)[1]},
        {"technique": "13C NMR", "conditions": "101 MHz, CDCl3", "data": ""}, {"technique": "HRMS", "conditions": "ESI", "data": ""}]})
    lab.html("pnair", e, "Observations & results", "<p>81%. DCE is not great (carcinogen); next time try 2-MeTHF or EtOAc for STAB reductions.</p>")
    lab.sign("pnair", e)
    lab.reject("mruiz", e, "Please add the 13C and HRMS data before I witness; both are still empty.", days=2)
    lab.pins["pnair"] = [kx101["id"], made["KX-105"][0]["id"]]
    lab.pins["mruiz"] = [kx101["id"]]
    lab.pins["ohaddad"] = [kx101["id"]]


# ---------------------------------------------------------------- Kenji: process scale-up
def _process(lab: Lab, NB):
    nb = NB["proc"]
    yield (41, 8)
    e = lab.experiment("kwatanabe", nb, "Suzuki step, 1 g (baseline, dioxane/water)", tags=["kx-101", "process", "suzuki", "baseline"],
                       description="Med-chem conditions as received from Priya, unchanged, to set the baseline")
    lab.html("kwatanabe", e, "Aim", "<p>Reproduce the med-chem Suzuki on 1 g and measure what has to change for scale: solvent (1,4-dioxane is a carcinogen and peroxide former), Pd loading, Pd in product (target ≤ 10 ppm for downstream steps), PMI.</p>")
    p = lab.product("Methyl 4-(pyridin-4-yl)benzoate", "COC(=O)c1ccc(-c2ccncc2)cc1", "COC(=O)c1ccc(Br)cc1", 1000.0, 79)
    lab.reaction("kwatanabe", e, [
        lab.row("Methyl 4-bromobenzoate", "reactant", basis="mass", mass_mg=1000.0, limiting=True),
        lab.row("4-Pyridinylboronic acid", "reactant", basis="equiv", equiv=1.3, purity=97),
        lab.row("Potassium carbonate", "reagent", basis="equiv", equiv=2.0),
        lab.row("Tetrakis(triphenylphosphine)palladium(0)", "catalyst", basis="equiv", equiv=0.03),
        lab.row("1,4-Dioxane", "solvent", basis="fill"),
        lab.row("Water (deionised)", "solvent", basis="volume", volume_ml=5), p], {"temperature_c": 90, "time_h": 16, "atmosphere": "N2", "target_conc_m": 0.2})
    lab.purification("kwatanabe", e, method="recrystallization", isolated_mg=p["actual_mass_mg"], appearance="Off-white solid",
                     notes_html="<p>Recrystallised from EtOAc/heptane. <b>Pd by ICP-MS: 410 ppm</b> (unacceptable).</p>")
    lab.section("kwatanabe", e, "Characterisation", {"items": [
        {"technique": "1H NMR", "conditions": "400 MHz, CDCl3", "data": "Conforms to reference (Priya's KX core record)"},
        {"technique": "HPLC", "conditions": "C18, 10→90% MeCN/H2O (0.1% H3PO4), 254 nm", "data": "98.7% area; des-bromo 0.4%, bipyridine 0.6%"},
        {"technique": "ICP-MS (Pd)", "conditions": "Microwave digestion, HNO3", "data": "410 ppm"}]})
    lab.html("kwatanabe", e, "Observations & results", "<p>79%, matches med-chem. Problems for scale: dioxane, 3 mol% Pd, 410 ppm residual Pd, PMI ≈ 130. Plan: 2-MeTHF/water, Pd(PPh<sub>3</sub>)<sub>4</sub> 1 mol%, thiol scavenger.</p>")
    lab.sign("kwatanabe", e); lab.witness("pnair", e, days=1)

    yield (29, 8)
    r = lab.post("kwatanabe", f"/api/experiments/{e['id']}/clone?scale=10")
    e2 = {"id": r["id"], "eid": r["eid"]}
    full = lab.get("kwatanabe", f"/api/experiments/{e2['id']}")
    e2["s"] = {x["title"]: x for x in full["entries"]}
    lab.patch("kwatanabe", f"/api/experiments/{e2['id']}", json={"title": "Suzuki step, 10 g: 2-MeTHF/water, 1 mol% Pd, thiol scavenger",
                                                                   "tags": ["kx-101", "process", "suzuki", "scale-up"]})
    lab.html("kwatanabe", e2, "Aim", "<p>10× scale with the process changes from the 1 g baseline: 2-MeTHF/water instead of dioxane, Pd lowered to 1 mol%, thiol scavenger for Pd removal, crystallisation instead of a column. Created with <i>Repeat at another scale</i> (×10) from the baseline.</p>")
    p = lab.product("Methyl 4-(pyridin-4-yl)benzoate", "COC(=O)c1ccc(-c2ccncc2)cc1", "COC(=O)c1ccc(Br)cc1", 10000.0, 86)
    lab.reaction("kwatanabe", e2, [
        lab.row("Methyl 4-bromobenzoate", "reactant", basis="mass", mass_mg=10000.0, limiting=True),
        lab.row("4-Pyridinylboronic acid", "reactant", basis="equiv", equiv=1.25, purity=97),
        lab.row("Potassium carbonate", "reagent", basis="equiv", equiv=2.0),
        lab.row("Tetrakis(triphenylphosphine)palladium(0)", "catalyst", basis="equiv", equiv=0.01),
        lab.row("2-Methyltetrahydrofuran", "solvent", basis="fill"),
        lab.row("Water (deionised)", "solvent", basis="volume", volume_ml=60), p], {"temperature_c": 80, "time_h": 12, "atmosphere": "N2", "target_conc_m": 0.4,
                                                                                    "notes": "Jacketed 500 mL reactor, overhead stirring 300 rpm"})
    lab.html("kwatanabe", e2, "Procedure", "<ol><li>Charge reactor with bromide, boronic acid, K<sub>2</sub>CO<sub>3</sub>; inert (3× vacuum/N<sub>2</sub>).</li><li>Add degassed 2-MeTHF and water; add Pd(PPh<sub>3</sub>)<sub>4</sub> as a slurry in 2-MeTHF.</li><li>Heat to 80 °C (jacket 85 °C). IPC by HPLC every 2 h.</li><li>At ≤ 1% SM: cool to 50 °C, separate phases, add SiliaMetS Thiol (5 wt%), stir 4 h at 50 °C, filter.</li><li>Distil 2-MeTHF down to 3 vol, add heptane (6 vol) at 60 °C, cool to 5 °C over 3 h, filter, wash, dry.</li></ol>")
    lab.add_section("kwatanabe", e2, "table", "IPC (HPLC area %, 254 nm)", {
        "columns": [{"name": "Time (h)"}, {"name": "Bromide (SM)"}, {"name": "Product"}, {"name": "Des-bromo (methyl benzoate)"}, {"name": "Homocoupled bipyridine"}],
        "rows": [["0", "98.6", "0.0", "0.3", "0.0"], ["2", "41.2", "56.9", "0.5", "0.4"], ["4", "12.8", "84.6", "0.7", "0.6"],
                 ["6", "3.1", "94.1", "0.8", "0.7"], ["8", "0.6", "96.5", "0.9", "0.7"], ["12", "0.2", "96.8", "0.9", "0.8"]]})
    lab.purification("kwatanabe", e2, method="recrystallization", isolated_mg=p["actual_mass_mg"], appearance="Off-white crystalline solid",
                     notes_html="<p>Pd by ICP-MS: 32 ppm before scavenger, <b>4 ppm after SiliaMetS Thiol</b> (spec ≤ 10 ppm). HPLC purity 99.6%.</p>")
    lab.section("kwatanabe", e2, "Characterisation", {"items": [
        {"technique": "1H NMR", "conditions": "400 MHz, CDCl3", "data": "Conforms to reference"},
        {"technique": "HPLC", "conditions": "C18, 10→90% MeCN/H2O (0.1% H3PO4), 254 nm", "data": "99.6% area; des-bromo 0.15%, bipyridine < 0.05%",
         "attachment_id": lab.attach("kwatanabe", e2, "KX-101-ester_10g_HPLC.csv", chromatogram(
             [(1.05, 9, 0.04), (4.62, 1.6, 0.05), (5.84, 640, 0.06), (7.31, 0.5, 0.06)], seed=7).encode(), "text/csv")["id"]},
        {"technique": "ICP-MS (Pd)", "conditions": "Microwave digestion, HNO3", "data": "32 ppm before scavenger; 4 ppm after SiliaMetS Thiol"},
        {"technique": "KF water", "conditions": "Coulometric", "data": "0.08% w/w"}]})
    lab.html("kwatanabe", e2, "Observations & results", "<p>86% on 10 g, Pd 4 ppm, no dioxane. PMI down from ~130 to ~45. Ready for 50 g.</p>")
    lab.sign("kwatanabe", e2); lab.witness("mruiz", e2, "Excellent improvement", days=2)

    yield (13, 8)
    r = lab.post("kwatanabe", f"/api/experiments/{e2['id']}/clone?scale=5")
    e3 = {"id": r["id"], "eid": r["eid"]}
    full = lab.get("kwatanabe", f"/api/experiments/{e3['id']}")
    e3["s"] = {x["title"]: x for x in full["entries"]}
    lab.patch("kwatanabe", f"/api/experiments/{e3['id']}", json={"title": "Suzuki step, 50 g (kilo-lab rehearsal)", "tags": ["kx-101", "process", "suzuki", "scale-up"]})
    lab.html("kwatanabe", e3, "Aim", "<p>Rehearse the 10 g process at 50 g in the 5 L jacketed reactor before the kilo-lab campaign; watch heat flow and Pd levels. Repeated at ×5 from the 10 g run.</p>")
    p = lab.product("Methyl 4-(pyridin-4-yl)benzoate", "COC(=O)c1ccc(-c2ccncc2)cc1", "COC(=O)c1ccc(Br)cc1", 50000.0, 88)
    rx = lab.get("kwatanabe", f"/api/experiments/{e3['id']}")["entries"]
    rows = next(x for x in rx if x["kind"] == "reaction")["content"]["rows"]
    for rr in rows:
        if rr["role"] == "product":
            rr["actual_mass_mg"] = p["actual_mass_mg"]
    lab.section("kwatanabe", e3, "Reaction", {"reaction_smiles": "COC(=O)c1ccc(Br)cc1.OB(O)c1ccncc1>>COC(=O)c1ccc(-c2ccncc2)cc1", "rows": rows,
                                              "conditions": {"temperature_c": 80, "time_h": 10, "atmosphere": "N2", "target_conc_m": 0.4, "notes": "5 L jacketed reactor"}})
    lab.section("kwatanabe", e3, "Characterisation", {"items": [
        {"technique": "HPLC", "conditions": "C18, 10→90% MeCN/H2O (0.1% H3PO4), 254 nm", "data": "99.7% area"},
        {"technique": "ICP-MS (Pd)", "conditions": "Microwave digestion, HNO3", "data": "3 ppm"},
        {"technique": "DSC", "conditions": "5 °C/min, sealed pan", "data": "mp onset 104.8 °C; no exotherm below 300 °C"}]})
    lab.html("kwatanabe", e3, "Observations & results", "<p>88% (59.1 g). <b>Exotherm on K<sub>2</sub>CO<sub>3</sub>/water addition: +12 °C in 4 min</b> at this scale; dose the base solution over 30 min next time. Pd 3 ppm. Batch released to med-chem as lot KX-INT-01.</p>")
    lab.safety("kwatanabe", e3, "Reactor in walk-in hood; blast shield; base dosed via addition funnel (exotherm, see results); N2 sweep.",
               "Aqueous phase: basic aqueous waste (contains boron). Scavenger cake: Pd heavy-metal waste. 2-MeTHF/heptane mother liquors: non-halogenated.",
               "Exotherm on base addition at scale: stop dosing, full jacket cooling.")
    lab.sign("kwatanabe", e3, "Kilo-lab rehearsal complete")
    lab.put("kwatanabe", f"/api/experiments/{e3['id']}/shares/{lab.uid['dcho']}", json={"permission": "view"})
    lab.comment("kwatanabe", e3, "@dcho FYI the IPC data from the 10 g run might interest you for the methodology paper (des-bromo stays < 1%).", minutes=30)
    lab.pins["kwatanabe"] = [e3["id"], e2["id"]]


# ---------------------------------------------------------------- Sophie: heterocycles, azides
def _sophie(lab: Lab, NB):
    nb = NB["sophie"]
    yield (70, 9)
    e = lab.experiment("slaurent", nb, "1-Methylindole (NaH, MeI, DMF)", tags=["indole", "n-alkylation"])
    p = lab.product("1-Methylindole", "Cn1ccc2ccccc21", "c1ccc2[nH]ccc2c1", 1172.0, 91)
    lab.reaction("slaurent", e, [
        lab.row("Indole", "reactant", basis="mass", mass_mg=1172.0, limiting=True),
        lab.row("Sodium hydride (60% in mineral oil)", "reagent", basis="equiv", equiv=1.2, purity=60),
        lab.row("Iodomethane", "reagent", basis="equiv", equiv=1.3),
        lab.row("N,N-Dimethylformamide", "solvent", basis="fill"), p], {"temperature_c": 0, "temperature_label": "0 °C → rt", "time_h": 2, "atmosphere": "N2", "target_conc_m": 0.5})
    lab.html("slaurent", e, "Procedure", "<ol><li>NaH (60%, washed with hexanes) suspended in DMF at 0 °C under N<sub>2</sub>.</li><li>Indole in DMF added dropwise over 10 min (H<sub>2</sub> evolution); stirred 30 min at 0 °C.</li><li>MeI added dropwise; warmed to rt, 1.5 h.</li><li>Quenched carefully with sat. NH<sub>4</sub>Cl at 0 °C, extracted with Et<sub>2</sub>O, washed with 5% LiCl, dried.</li></ol>")
    lab.html("slaurent", e, "Observations & results", "<p>91%. Fast and clean; keep NaH/DMF at ≤ rt and small scale (known thermal runaway hazard). Benchtop's risk assessment flags the NaH + DMF combination.</p>")
    lab.safety("slaurent", e, "NaH weighed under N2; added portionwise at 0 °C (H2 evolution, vented through bubbler); MeI in hood only, double gloves.",
               "Quench excess NaH with iPrOH then water at 0 °C. MeI-containing waste: halogenated.", "NaH fire: sand / Class D extinguisher, never water.",
               "<p>Known hazard: NaH in DMF can run away above ~50 °C. Kept at 0 °C → rt, small scale only.</p>")
    lab.purification("slaurent", e, method="column", stationary_phase="Silica gel", eluent="hexanes → 2% EtOAc/hexanes", rf="0.55", rf_eluent="5% EtOAc/hexanes",
                     isolated_mg=p["actual_mass_mg"], appearance="Colourless oil")
    lab.characterise("slaurent", e, "Cn1ccc2ccccc21", "1-Methylindole", seed=61)
    lab.sign("slaurent", e); lab.witness("dcho", e, days=1)

    yield (19, 10)
    e = lab.experiment("slaurent", nb, "Benzyl azide (NaN3, DMF)", tags=["azide", "click"])
    lab.html("slaurent", e, "Aim", "<p>Benzyl azide for CuAAC model studies. Small scale (5 mmol); azide kept in solution where possible.</p>")
    rows = [lab.row("Benzyl bromide", "reactant", basis="mass", mass_mg=855.0, limiting=True),
            lab.row("Sodium azide", "reagent", basis="equiv", equiv=1.5),
            lab.row("N,N-Dimethylformamide", "solvent", basis="fill"),
            lab.row("Dichloromethane", "solvent", basis="volume", volume_ml=5),
            lab.product("Benzyl azide", "[N-]=[N+]=NCc1ccccc1", "BrCc1ccccc1", 855.0, None)]
    lab.reaction("slaurent", e, rows, {"temperature_label": "rt", "time_h": 12, "atmosphere": "air", "target_conc_m": 0.5})
    lab.safety("slaurent", e, "Blast shield; plastic spatula; no metal needles with NaN3; never concentrate to dryness.", "Azide waste in the dedicated, labelled container (no acids, no heavy metals).")
    t = lab.comment("mruiz", e, "@slaurent Please take the dichloromethane out: sodium azide and DCM can form diazidomethane, which is explosive. Benchtop flags it in the risk assessment too. Extract with Et2O or MTBE instead.", section="Reaction", minutes=180)
    later(90)
    rows = [r for r in rows if r.get("name") != "Dichloromethane"]
    for r in rows:
        if r["role"] == "product":
            r.update(Lab.product("Benzyl azide", "[N-]=[N+]=NCc1ccccc1", "BrCc1ccccc1", 855.0, 94, purity=95))
    lab.reaction("slaurent", e, rows, {"temperature_label": "rt", "time_h": 12, "atmosphere": "air", "target_conc_m": 0.5,
                                       "notes": "Work-up: water, extracted with Et2O (×3); no DCM"})
    lab.comment("slaurent", e, "Thanks, I didn't know that one. Removed DCM from the plan; extracted with Et2O and kept the azide as a ~1 M solution.", parent=t["id"], minutes=5)
    lab.patch("mruiz", f"/api/comments/{t['id']}", json={"resolved": True})
    lab.purification("slaurent", e, method="extraction", isolated_mg=None, appearance="Colourless solution in Et2O (~1 M)",
                     notes_html="<p>Not concentrated to dryness (azide). Concentration by <sup>1</sup>H NMR vs. internal standard: 0.94 M, 94%.</p>")
    lab.characterise("slaurent", e, "[N-]=[N+]=NCc1ccccc1", "Benzyl azide", c13=False, hrms=None, ir="2095 (N3, strong), 1455, 1254, 698 cm⁻¹", seed=62)
    lab.html("slaurent", e, "Procedure", "<ol><li>NaN<sub>3</sub> (1.5 equiv) in DMF behind a blast shield; benzyl bromide added dropwise at rt.</li><li>Stirred 12 h; TLC (hexanes) showed no bromide.</li><li>Diluted with water (5 vol), extracted with Et<sub>2</sub>O (×3), washed with water and brine, dried (MgSO<sub>4</sub>).</li><li>Concentrated carefully at ≥ 200 mbar, 25 °C, to ~5 mL; stored as a solution at 4 °C.</li></ol>")
    lab.html("slaurent", e, "Observations & results", "<p>94% by qNMR, kept as a 0.94 M Et<sub>2</sub>O solution. First plan used DCM for the extraction; changed after Maria's comment (azide + DCM → diazidomethane risk). See the Reaction section's history (v2 → v3) and the resolved discussion.</p>")
    lab.sign("slaurent", e); lab.witness("mruiz", e, days=1)
    azide = e

    yield (3, 14)
    e = lab.experiment("slaurent", nb, "1-Benzyl-4-phenyl-1H-1,2,3-triazole (CuAAC)", tags=["click", "cuaac", "triazole"])
    p = lab.product("1-Benzyl-4-phenyl-1H-1,2,3-triazole", "c1ccc(Cn2cc(-c3ccccc3)nn2)cc1", "C#Cc1ccccc1", 204.0, 93)
    lab.reaction("slaurent", e, [
        lab.row("Phenylacetylene", "reactant", basis="mass", mass_mg=204.0, limiting=True),
        {"role": "reactant", "name": "Benzyl azide (0.94 M in Et2O)", "smiles": "[N-]=[N+]=NCc1ccccc1", "basis": "equiv", "equiv": 1.05, "conc_m": 0.94},
        lab.row("Copper(II) sulfate", "catalyst", basis="equiv", equiv=0.05),
        lab.row("Sodium L-ascorbate", "reagent", basis="equiv", equiv=0.10),
        lab.row("tert-Butanol", "solvent", basis="volume", volume_ml=2), lab.row("Water (deionised)", "solvent", basis="volume", volume_ml=2), p],
        {"temperature_label": "rt", "time_h": 6, "atmosphere": "air"})
    lab.html("slaurent", e, "Procedure", "<p>Alkyne and benzyl azide solution in <i>t</i>BuOH/water (1:1); freshly made sodium ascorbate solution then CuSO<sub>4</sub> solution added (yellow → brown suspension). Stirred 6 h at rt; white precipitate formed.</p>")
    lab.log("slaurent", e, f"Azide solution from {azide['eid']} (0.94 M) measured by syringe; alkyne in tBuOH/water")
    lab.log("slaurent", e, "Ascorbate then CuSO4 added: yellow, then brown suspension", 6)
    lab.log("slaurent", e, "⏱ Started 1 h timer: TLC check", 1)
    plate = lab.attach("slaurent", e, "TLC_1h_CuAAC.png", tlc_plate([("SM", [(0.78, 0.9)]), ("co", [(0.78, 0.6), (0.31, 0.5)]), ("rxn", [(0.78, 0.25), (0.31, 0.95)])], seed=8), "image/png")
    lab.log("slaurent", e, "⏱ TLC check: 1 h done", 59)
    lab.log("slaurent", e, "TLC after 1 h (3:1 hexanes/EtOAc, UV): alkyne nearly gone, triazole at Rf 0.31", 3, attachment_id=plate["id"])
    lab.log("slaurent", e, "White precipitate forming; left stirring", 50)
    lab.log("slaurent", e, "Filtered, washed with dilute NH4OH (blue filtrate = Cu out) and water", 250)
    lab.purification("slaurent", e, method="trituration", isolated_mg=p["actual_mass_mg"], appearance="White solid",
                     notes_html="<p>Product precipitated; diluted with water, filtered, washed with dilute NH<sub>4</sub>OH (removes Cu) and water.</p>")
    lab.characterise("slaurent", e, "c1ccc(Cn2cc(-c3ccccc3)nn2)cc1", "1-Benzyl-4-phenyltriazole", mp="127–129 °C", seed=63)
    lab.html("slaurent", e, "Observations & results", f"<p>93%, no chromatography needed. Azide from {azide['eid']}. Draft: will sign after the 13C is re-run with more scans.</p>")
    lab.pins["slaurent"] = [e["id"]]


# ---------------------------------------------------------------- Alex (undergrad) and teaching labs
def _students(lab: Lab, NB):
    yield (56, 13)
    e = lab.experiment("slaurent", NB["teach"], "Aspirin (acetylsalicylic acid)", tags=["teaching", "esterification"],
                       description="Second-year teaching lab demonstration")
    p = lab.product("Acetylsalicylic acid", "CC(=O)Oc1ccccc1C(=O)O", "O=C(O)c1ccccc1O", 2000.0, 78)
    lab.reaction("slaurent", e, [
        lab.row("Salicylic acid", "reactant", basis="mass", mass_mg=2000.0, limiting=True),
        lab.row("Acetic anhydride", "reagent", basis="volume", volume_ml=5.0),
        lab.row("Phosphoric acid (85%)", "catalyst", basis="volume", volume_ml=0.25, purity=85), p], {"temperature_c": 85, "time_h": 0.25, "atmosphere": "air"})
    lab.html("slaurent", e, "Procedure", "<ol><li>Salicylic acid, acetic anhydride and 5 drops of 85% H<sub>3</sub>PO<sub>4</sub> in a 50 mL flask; warm at 85 °C for 15 min.</li><li>Add 2 mL water dropwise (destroys excess anhydride), then 20 mL cold water; cool in ice.</li><li>Filter, wash with ice water, recrystallise from ethanol/water.</li></ol>")
    lab.purification("slaurent", e, method="recrystallization", isolated_mg=p["actual_mass_mg"], appearance="White needles",
                     notes_html="<p>FeCl<sub>3</sub> test: negative (no free phenol) after recrystallisation.</p>")
    lab.characterise("slaurent", e, "CC(=O)Oc1ccccc1C(=O)O", "Aspirin", exch=11.0, mp="134–136 °C (lit. 135 °C)", ir="1750 (ester C=O), 1680 (acid C=O), 1605, 1185 cm⁻¹", hrms=None, seed=71)
    lab.sign("slaurent", e); lab.witness("mruiz", e, days=1)

    yield (50, 13)
    e = lab.experiment("slaurent", NB["teach"], "Paracetamol (4-acetamidophenol)", tags=["teaching", "amide"])
    p = lab.product("Paracetamol", "CC(=O)Nc1ccc(O)cc1", "Nc1ccc(O)cc1", 2180.0, 72)
    lab.reaction("slaurent", e, [
        lab.row("4-Aminophenol", "reactant", basis="mass", mass_mg=2180.0, limiting=True),
        lab.row("Acetic anhydride", "reagent", basis="equiv", equiv=1.2),
        lab.row("Water (deionised)", "solvent", basis="volume", volume_ml=6), p], {"temperature_c": 100, "time_h": 0.2, "atmosphere": "air"})
    lab.html("slaurent", e, "Procedure", "<ol><li>4-Aminophenol suspended in water (6 mL); acetic anhydride added; heated on a boiling water bath, 10 min (dissolves, then product starts to crystallise).</li><li>Cooled in ice, filtered, washed with ice water.</li><li>Recrystallised from water (≈ 10 mL/g) with a little charcoal.</li></ol>")
    lab.purification("slaurent", e, method="recrystallization", isolated_mg=p["actual_mass_mg"], appearance="White crystals (slightly pink crude)",
                     notes_html="<p>Crude decolourised with charcoal, recrystallised from water.</p>")
    lab.characterise("slaurent", e, "CC(=O)Nc1ccc(O)cc1", "Paracetamol", solvent="DMSO-d6", mp="168–170 °C (lit. 169 °C)", hrms=None, c13=False, seed=72)
    lab.sign("slaurent", e)

    # Alex: private notebook, ibuprofen reference with real raw FIDs
    yield (9, 15)
    nb = NB["alex"]
    e = lab.experiment("amorgan", nb, "Ibuprofen reference standard: 1H and 13C from raw FIDs", tags=["reference", "nmr", "training"],
                       description="Learning to process raw Bruker data in Benchtop; commercial ibuprofen, 600 MHz")
    lab.html("amorgan", e, "Aim", "<p>Practice NMR processing and assignment on a known compound before characterising my own products. Data: raw FIDs straight from the 600 MHz (zipped experiment folders).</p>")
    lab.reaction("amorgan", e, [{"role": "product", "name": "Ibuprofen", "smiles": "CC(C)Cc1ccc(cc1)C(C)C(=O)O", "equiv": 1}], {})
    samples = Path(__file__).resolve().parents[2] / "samples"
    items = []
    for nuc, fn in (("1H NMR", "ibuprofen_1H_rawFID.zip"), ("13C NMR", "ibuprofen_13C_rawFID.zip")):
        f = samples / fn
        if f.exists():
            a = lab.attach("amorgan", e, fn, f.read_bytes(), "application/zip")
            items.append({"technique": nuc, "conditions": "600 MHz, CDCl3" if nuc.startswith("1H") else "151 MHz, CDCl3", "data": "", "attachment_id": a["id"]})
    if items:
        lab.section("amorgan", e, "Characterisation", {"items": items})
    lab.html("amorgan", e, "Observations & results", "<p>Phasing was automatic; I nudged phase 0 a little on the 13C. All 10 carbons and 8 proton environments assigned with the prediction panel. One small singlet at 1.28 ppm doesn't belong to ibuprofen (grease?).</p>")
    lab.add_section("amorgan", e, "references", "References", {"items": [
        {"doi": "10.1021/om100106e", "title": "NMR Chemical Shifts of Trace Impurities: Common Laboratory Solvents, Organics, and Gases in "
         "Deuterated Solvents Relevant to the Organometallic Chemist",
         "authors": ["Fulmer, Gregory R.", "Miller, Alexander J. M.", "Sherden, Nathaniel H.", "Gottlieb, Hugo E.", "Nudelman, Abraham",
                     "Stoltz, Brian M.", "Bercaw, John E.", "Goldberg, Karen I."],
         "journal": "Organometallics", "year": "2010", "volume": "29", "issue": "9", "pages": "2176-2179",
         "note": "Impurity table: hydrocarbon grease at 1.26 ppm in CDCl3"},
        {"doi": "10.1021/jo971176v", "title": "NMR Chemical Shifts of Common Laboratory Solvents as Trace Impurities",
         "authors": ["Gottlieb, Hugo E.", "Kotlyar, Vadim", "Nudelman, Abraham"],
         "journal": "J. Org. Chem.", "year": "1997", "volume": "62", "issue": "21", "pages": "7512-7515", "note": ""}]})
    lab.comment("amorgan", e, "@mruiz Is the 1.28 ppm singlet grease? It doesn't match anything in ibuprofen.", minutes=20)
    lab.comment("mruiz", e, "Probably. Compare with the impurity table (Fulmer); hydrocarbon grease is ~1.26 ppm in CDCl3. Good first processing job!", minutes=400)

    yield (4, 10)
    e = lab.experiment("amorgan", nb, "Repeat of KX-102 under supervision (training)", tags=["training", "amide", "kx-series"],
                       description="Following Priya's procedure for KX-102")
    lab.html("amorgan", e, "Aim", "<p>Repeat KX-102 (4-fluoroaniline amide) to learn HATU couplings and LC-MS monitoring.</p>")
    p = lab.product("KX-102", "O=C(Nc1ccc(F)cc1)c1ccc(-c2ccncc2)cc1", "OC(=O)c1ccc(-c2ccncc2)cc1", 99.6, 69)
    lab.reaction("amorgan", e, [
        {"role": "reactant", "name": "4-(Pyridin-4-yl)benzoic acid", "smiles": "OC(=O)c1ccc(-c2ccncc2)cc1", "basis": "mass", "mass_mg": 99.6, "limiting": True},
        lab.row("4-Fluoroaniline", "reactant", basis="equiv", equiv=1.1),
        lab.row("HATU", "reagent", basis="equiv", equiv=1.2), lab.row("N,N-Diisopropylethylamine", "reagent", basis="equiv", equiv=3.0),
        lab.row("N,N-Dimethylformamide", "solvent", basis="fill"), p], {"temperature_label": "rt", "time_h": 4, "atmosphere": "N2", "target_conc_m": 0.2})
    lab.html("amorgan", e, "Procedure", "<p>Followed Priya's KX-102 record step by step (HATU activation 10 min, then 4-fluoroaniline; LC-MS at 1, 2 and 4 h). Priya checked the set-up.</p>")
    lab.html("amorgan", e, "Observations & results", "<p>69% (Priya got 76%). Lost some product in the LiCl washes: emulsion. Next time: precipitate with water like KX-101.</p>")
    lab.pins["amorgan"] = [e["id"]]


def main() -> None:
    from . import models as m
    from .config import settings
    settings.demo = True  # the example plugins are on in the demo
    settings.check_interval_hours = 0  # no daily data check of the half-built lab; the real server checks it when it starts
    m.datetime = _SimDatetime  # the simulated clock (models.utcnow reads it)
    try:
        from .main import app
        with TestClient(app) as c:
            if c.get("/api/setup").json().get("needs_setup") is not True:
                print("This database already has accounts; the demo lab needs a fresh, empty data folder.")
                sys.exit(1)
            run(c)
    finally:
        m.datetime = datetime
    print(f"Demo lab created in {settings.data_dir}.")
    print(f"Sign in as any of: {', '.join(u for u, *_ in PEOPLE)}  (password: {PW})")


if __name__ == "__main__":
    main()
