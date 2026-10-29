"""Fixes from a first-time-user review of 0.28: the dashboard's witness list, finding an experiment by its sample code,
atom economy over 100 %, the Inbox in "Customise menu"."""
import pytest

PW = "lab-bench-pw-2026"


def _account(client, admin, username, role="scientist"):
    r = client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                       "full_name": username.title(), "role": role})
    assert r.status_code == 201, r.text
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": username, "password": PW}).json()["token"]}
    return h, r.json()["id"]


@pytest.fixture(scope="module")
def lab(client, admin):
    """An author, the colleague she asks to witness, someone else, a newcomer and a read-only account."""
    return {name: _account(client, admin, name, "viewer" if name == "rv_viewer" else "scientist")
            for name in ("rv_author", "rv_asked", "rv_other", "rv_newcomer", "rv_viewer")}


def _signed(client, h, nb, title, witness_id=None):
    e = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb}).json()
    body = {"password": PW} | ({"witness_id": witness_id} if witness_id else {})
    r = client.post(f"/api/experiments/{e['id']}/sign", headers=h, json=body)
    assert r.status_code == 200, r.text
    return e["id"]


# ------------------------------------------------------------------ B4: "Waiting for my witness signature"

def test_dashboard_lists_only_what_you_were_asked_to_witness(client, lab):
    """0.28 listed (and counted) every signed record by others you could open, so a newcomer's first dashboard said
    "1 waiting for your witness signature" for a record nobody had asked them about."""
    (author, _), (asked, asked_id), (other, other_id) = lab["rv_author"], lab["rv_asked"], lab["rv_other"]
    nb = client.post("/api/notebooks", headers=author, json={"name": "Witness lists"}).json()["id"]
    for_asked = _signed(client, author, nb, "Asked of rv_asked", asked_id)
    for_other = _signed(client, author, nb, "Asked of rv_other", other_id)
    nobody = _signed(client, author, nb, "Nobody asked")

    def lists(h):
        d = client.get("/api/dashboard", headers=h).json()["lists"]
        return [e["id"] for e in d["awaiting"]], [e["id"] for e in d.get("could_witness", [])]

    awaiting, could = lists(asked)
    assert for_asked in awaiting and all(e["assigned_to_me"] for e in client.get("/api/dashboard", headers=asked).json()["lists"]["awaiting"])
    assert nobody not in awaiting and for_other not in awaiting, "records nobody asked me about are counted as waiting for me"
    assert nobody in could, "a record nobody was asked to witness isn't offered apart"
    assert for_other not in could and for_asked not in could, "a record whose author asked someone (else) is offered to all"

    awaiting, could = lists(lab["rv_newcomer"][0])  # nobody has asked the newcomer anything
    assert not set(awaiting) & {for_asked, for_other, nobody}
    assert nobody in could and for_other not in could

    awaiting, could = lists(lab["rv_viewer"][0])  # a read-only account can't witness at all
    assert not set(awaiting) & {for_asked, for_other, nobody} and not set(could) & {for_asked, for_other, nobody}

    # the author's own list of records waiting for a witness is unchanged
    waiting = client.get("/api/dashboard", headers=author).json()["lists"]["waiting"]
    assert {for_asked, for_other, nobody} <= {e["id"] for e in waiting}
    assert not set(lists(author)[0]) & {for_asked, for_other, nobody}


# ------------------------------------------------------------------ B5: finding an experiment by its sample code

def test_search_finds_an_experiment_by_its_sample_code(client, lab):
    """0.28: searching "NN-001" said nothing matched; sample codes weren't searched at all."""
    (me, _), (other, _) = lab["rv_newcomer"], lab["rv_other"]
    assert client.put("/api/auth/me/sample-prefix", headers=me, json={"prefix": "RVN"}).status_code == 200
    nb = client.post("/api/notebooks", headers=me, json={"name": "Codes"}).json()["id"]
    e = client.post("/api/experiments", headers=me, json={"title": "Recrystallised product", "notebook_id": nb}).json()
    code = client.post(f"/api/experiments/{e['id']}/samples", headers=me, json={}).json()["code"]
    assert code == "RVN-001"
    # another record that only mentions the code (in its title, three times) comes after the one it was handed to
    mention = client.post("/api/experiments", headers=me, json={"title": "Second crop of RVN-001 RVN-001 RVN-001", "notebook_id": nb}).json()
    for q in ("RVN-001", "rvn-1", "RVN_001", "RVN 001", "rvn-001 recrystallised"):
        hits = client.get("/api/search", headers=me, params={"q": q}).json()["experiments"]
        assert hits and hits[0]["id"] == e["id"], f"{q}: {[h['eid'] for h in hits]}"
        if q != "rvn-001 recrystallised":
            assert mention["id"] in [h["id"] for h in hits[1:]], q
    assert client.get("/api/search", headers=me, params={"q": "RVN-001"}).json()["experiments"][0]["match"] == "Sample code"
    # the ⌘K box gets each experiment's codes with the list of experiments
    listed = {x["id"]: x for x in client.get("/api/experiments", headers=me).json()}
    assert listed[e["id"]].get("sample_codes") == ["RVN-001"] and listed[mention["id"]].get("sample_codes") == []

    # sharing rules still apply: in a private notebook, nobody else finds it by its code
    private = client.post("/api/notebooks", headers=me, json={"name": "Mine only", "visibility": "private"}).json()["id"]
    hidden = client.post("/api/experiments", headers=me, json={"title": "Private work", "notebook_id": private}).json()
    hidden_code = client.post(f"/api/experiments/{hidden['id']}/samples", headers=me, json={}).json()["code"]
    assert client.get("/api/search", headers=me, params={"q": hidden_code}).json()["experiments"][0]["id"] == hidden["id"]
    assert hidden["id"] not in [h["id"] for h in client.get("/api/search", headers=other, params={"q": hidden_code}).json()["experiments"]]
    # a code taken off its experiment no longer finds it
    sid = next(s["id"] for s in client.get(f"/api/experiments/{hidden['id']}", headers=me).json()["samples"] if s["code"] == hidden_code)
    assert client.delete(f"/api/samples/{sid}", headers=me).status_code == 200
    assert hidden["id"] not in [h["id"] for h in client.get("/api/search", headers=me, params={"q": hidden_code}).json()["experiments"]]


# ------------------------------------------------------------------ B6: atom economy over 100 %

SALICYLIC, AC2O, ASPIRIN = "OC(=O)c1ccccc1O", "CC(=O)OC(C)=O", "CC(=O)Oc1ccccc1C(=O)O"


def _aspirin(anhydride_role):
    return [{"role": "reactant", "name": "Salicylic acid", "smiles": SALICYLIC, "limiting": True, "basis": "mass", "mass_mg": 138.1},
            {"role": anhydride_role, "name": "Acetic anhydride", "smiles": AC2O, "equiv": 3, "basis": "equiv"},
            {"role": "product", "name": "Aspirin", "smiles": ASPIRIN, "equiv": 1, "actual_mass_mg": 160}]


def test_atom_economy_is_never_over_100_percent(client, lab):
    """0.28 showed "AE 130%" for aspirin (and 139 % for paracetamol) when acetic anhydride, a co-reactant, was marked
    as Reagent: only Reactant rows count, so the product weighed more than the reactants."""
    h = lab["rv_author"][0]
    m = client.post("/api/chem/stoichiometry", headers=h, json={"rows": _aspirin("reagent")}).json()["totals"]["metrics"]
    assert "atom_economy_pct" not in m and "atom_economy" in m["impossible"], m
    assert "rme_pct" not in m and "rme" in m["impossible"], "RME (160 mg from 138 mg of reactants) is just as impossible"
    assert m["pmi"] > 1  # everything in the table counts here, whatever its role
    m = client.post("/api/chem/stoichiometry", headers=h, json={"rows": _aspirin("reactant")}).json()["totals"]["metrics"]
    assert m["atom_economy_pct"] == pytest.approx(74.9, abs=0.2) and "impossible" not in m
    assert 0 < m["rme_pct"] <= 100
    # the Reactions page works it out the same way
    nb = client.post("/api/notebooks", headers=h, json={"name": "Aspirin"}).json()["id"]
    e = client.post("/api/experiments", headers=h, json={"title": "Aspirin, anhydride as reagent", "notebook_id": nb}).json()
    client.post(f"/api/experiments/{e['id']}/entries", headers=h, json={"kind": "reaction", "title": "Acetylation", "content": {"rows": _aspirin("reagent")}})
    row = next(r for r in client.get("/api/reactions", headers=h).json() if r["experiment_id"] == e["id"])
    assert "atom_economy_pct" not in row["metrics"] and row["metrics"]["impossible"] == ["atom_economy", "rme"]


# ------------------------------------------------------------------ B9: safety data with PubChem lookups off

def test_safety_data_is_unavailable_not_fetching_when_pubchem_is_off(client, lab, admin):
    """With lookups off (as in these tests) nothing will ever be fetched: 0.28 said "Fetching…" for ever, and
    "Record hazards in notebook" stayed greyed out while anything was still "pending"."""
    from app import pubchem
    assert not pubchem.enabled()
    h = lab["rv_author"][0]
    nb = client.post("/api/notebooks", headers=h, json={"name": "Safety offline"}).json()["id"]
    e = client.post("/api/experiments", headers=h, json={"title": "Never looked up", "notebook_id": nb}).json()
    rows = [{"role": "reactant", "name": "2-Bromo-5-nitrothiophene", "smiles": "O=[N+]([O-])c1ccc(Br)s1", "limiting": True, "basis": "mass", "mass_mg": 100}]
    client.post(f"/api/experiments/{e['id']}/entries", headers=h, json={"kind": "reaction", "title": "Rxn", "content": {"rows": rows}})
    a = client.get(f"/api/experiments/{e['id']}/safety", headers=h).json()
    assert a["pubchem_enabled"] is False
    assert [c["safety"]["status"] for c in a["chemicals"]] == ["unavailable"]
    assert a["pending"] == 0, "the hazards can't be recorded while something is said to be on its way"
    m = client.post("/api/materials", headers=admin, json={"name": "Never looked up either", "smiles": "O=C1CCC(=O)N1Br"}).json()
    assert client.get(f"/api/materials/{m['id']}", headers=h).json()["safety"]["status"] == "unavailable"
    assert next(x for x in client.get("/api/materials", headers=h).json() if x["id"] == m["id"])["safety"]["status"] == "unavailable"


def test_a_material_used_in_records_you_cant_open(client, lab, admin):
    """0.28: a newcomer saw "Not linked to any reaction tables yet" for HATU while the lot's ledger listed EXP-00016…21
    as links (to "not found": those records are in group notebooks the newcomer can't open)."""
    (author, _), (newcomer, _) = lab["rv_author"], lab["rv_newcomer"]
    m = client.post("/api/materials", headers=admin, json={"name": "Coupling reagent RV", "smiles": "CN(C)C(=O)N(C)C"}).json()
    lot = client.post(f"/api/materials/{m['id']}/lots", headers=admin, json={"amount": 10, "unit": "g"}).json()
    private = client.post("/api/notebooks", headers=author, json={"name": "Author only", "visibility": "private"}).json()["id"]
    e = client.post("/api/experiments", headers=author, json={"title": "Private coupling", "notebook_id": private}).json()
    rows = [{"role": "reagent", "name": m["name"], "smiles": m["smiles"], "material_id": m["id"], "lot_id": lot["id"], "mass_mg": 500, "basis": "mass"}]
    client.post(f"/api/experiments/{e['id']}/entries", headers=author, json={"kind": "reaction", "title": "Rxn", "content": {"rows": rows}})
    assert client.post(f"/api/lots/{lot['id']}/transactions", headers=author,
                       json={"amount": 500, "unit": "mg", "direction": "out", "experiment_id": e["id"], "note": "in reaction"}).status_code in (200, 201)

    mine = client.get(f"/api/materials/{m['id']}", headers=author).json()
    assert [u["id"] for u in mine["used_in"]] == [e["id"]] and mine["used_in_hidden"] == 0
    theirs = client.get(f"/api/materials/{m['id']}", headers=newcomer).json()
    assert theirs["used_in"] == [] and theirs["used_in_hidden"] == 1, "says it's in no reaction table"
    tx = client.get(f"/api/lots/{lot['id']}/transactions", headers=newcomer).json()
    assert [t["experiment"] for t in tx if t["experiment"]] == [{"id": None, "eid": e["eid"]}], "links to a record they can't open"
    assert client.get(f"/api/lots/{lot['id']}/transactions", headers=author).json()[0]["experiment"] == {"id": e["id"], "eid": e["eid"]}


# ------------------------------------------------------------------ B7: the Inbox in "Customise menu"

def test_the_inbox_can_be_moved_and_hidden_in_the_menu(client, lab):
    """0.28 dropped "inbox" from the saved sidebar order and hidden list, so it couldn't be moved or hidden."""
    h = lab["rv_other"][0]
    order = ["inbox", "dashboard", "search", "reactions"]
    prefs = client.patch("/api/auth/me", headers=h, json={"preferences": {"nav": order, "nav_hidden": ["inbox", "groups"]}}).json()["preferences"]
    assert prefs["nav"] == order and prefs["nav_hidden"] == ["inbox", "groups"]
    assert client.get("/api/auth/me", headers=h).json()["preferences"]["nav_hidden"] == ["inbox", "groups"]


def test_the_server_knows_every_item_of_the_sidebar():
    """The sidebar's items (frontend/src/components/MainNav.tsx) and what the server keeps (NAV_KEYS) must match."""
    import re
    from pathlib import Path

    from app.routers.auth import NAV_KEYS
    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "MainNav.tsx").read_text()
    items = src[src.index("const ITEMS = ["):src.index("] as const")]
    assert re.findall(r"\{ key: '([a-z]+)'", items) == list(NAV_KEYS)


# ------------------------------------------------------------------ batch 2: R6, P4, M2

def test_unnamed_rows_are_called_by_formula_or_place_never_by_smiles(client, lab):
    """R6: rows from a pasted reaction SMILES have no names. 0.28's messages listed them by SMILES ("E-factor/PMI need a
    mass for O=C(O)c1ccccc1…") and the risk table called them "reactant". Now: the formula, else "Reactant 2"."""
    from app.chem import compute_stoichiometry, green_metrics, row_label
    rows = [{"role": "reactant", "smiles": "O=C(O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 122.1},
            {"role": "reactant", "smiles": "NCc1ccccc1", "equiv": 1.1, "basis": "equiv"},
            {"role": "reactant", "smiles": "CCO", "equiv": 1, "basis": "equiv"},
            {"role": "product", "smiles": "O=C(NCc1ccccc1)c1ccccc1", "equiv": 1, "actual_mass_mg": 150}]
    out = compute_stoichiometry(rows)
    for r in out["rows"]:
        if r["smiles"] == "CCO":
            r["mass_mg"] = None  # a row without a mass: E-factor and PMI say which row they need
    missing = green_metrics(out["rows"])["missing_mass"]
    assert missing == ["C2H6O"], missing
    assert row_label(out["rows"][0], out["rows"]) == "C7H6O2"
    lone = {"role": "reactant", "smiles": "not a smiles"}
    assert row_label(lone, [{"role": "reactant"}, lone]) == "Reactant 2"
    bad = compute_stoichiometry([{"role": "reactant", "smiles": "C1CC(", "limiting": True, "mass_mg": 10, "basis": "mass"}])
    assert bad["warnings"] and all("C1CC(" not in w for w in bad["warnings"]), bad["warnings"]
    # the risk assessment's rows
    h = lab["rv_newcomer"][0]
    nb = client.post("/api/notebooks", headers=h, json={"name": "Pasted SMILES"}).json()["id"]
    e = client.post("/api/experiments", headers=h, json={"title": "Amide from SMILES", "notebook_id": nb}).json()
    client.post(f"/api/experiments/{e['id']}/entries", headers=h, json={"kind": "reaction", "title": "Pasted", "content": {"rows": out["rows"]}})
    names = [c["name"] for c in client.get(f"/api/experiments/{e['id']}/safety", headers=h).json()["chemicals"]]
    assert names and all(n and "c1ccccc1" not in n and n != "reactant" for n in names), names


def test_lookups_turned_off_say_so_plainly(client, lab, admin):
    """P4: 0.28 told scientists "PubChem lookups are turned off on this server (BENCHTOP_PUBCHEM=off)". The setting's
    name is for the admin, who can change it."""
    r = client.get("/api/lookup", params={"q": "sodium borohydride"}, headers=lab["rv_newcomer"][0])
    assert r.status_code == 503 and "turned off" in r.json()["detail"] and "BENCHTOP_" not in r.json()["detail"]
    r = client.get("/api/lookup", params={"q": "sodium borohydride"}, headers=admin)
    assert r.status_code == 503 and "BENCHTOP_PUBCHEM=off" in r.json()["detail"]


def test_signing_with_empty_sections_covers_what_it_always_did(client, lab):
    """M2: the sign dialog now lists the sections still empty. It's only shown: signing a record with empty sections
    still works, the dialog sends what it sent before (password and comment), and the signature's hash is the one
    the existing code computes for the record as it stands, before and after signing."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Experiment
    from app.services import content_hash
    h = lab["rv_author"][0]
    nb = client.post("/api/notebooks", headers=h, json={"name": "Empty sections"}).json()["id"]
    e = client.post("/api/experiments", headers=h, json={"title": "Signed as it was", "notebook_id": nb}).json()
    entries = client.get(f"/api/experiments/{e['id']}", headers=h).json()["entries"]
    assert entries and all(not (x["content"].get("html") or x["content"].get("rows")) for x in entries if x["kind"] in ("text", "reaction"))
    with Session(engine) as s:
        before = content_hash(s, s.get(Experiment, e["id"]))
    r = client.post(f"/api/experiments/{e['id']}/sign", headers=h, json={"password": PW, "comment": ""})  # SignDialog's body
    assert r.status_code == 200, r.text
    sig = r.json()["signatures"][-1]
    with Session(engine) as s:
        after = content_hash(s, s.get(Experiment, e["id"]))
    assert sig["content_hash"] == before == after
    v = client.get(f"/api/experiments/{e['id']}/verify", headers=h).json()
    assert v["intact"] and v["content_hash"] == sig["content_hash"]
