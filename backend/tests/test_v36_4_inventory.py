"""0.36.4: inventory amounts, duplicates and values that make no sense (found by the 0.36.2 stress test).

- Withdrawals and undos read the lot after taking the database's write lock, so two at the same moment are made one
  after the other: 60 withdrawals of 250 mg from a 10 g bottle at once were all accepted, and the bottle's amount no
  longer matched its ledger; one withdrawal undone three times at once was put back three times.
- "Is it already there?" and the new row are one step: the same structure registered twice at once (two MAT numbers),
  a product registered twice by a double click, two lots with the same new barcode, and a group created twice
  ("Internal Server Error") can't happen.
- A row's "taken from inventory" mark is kept when the section is saved (it was dropped, so the page offered Deduct
  again and the bottle was deducted twice), and only when it names a real withdrawal for that row.
- Densities, molecular weights, purities and statuses must make sense; % and _ in a search are just characters.

The races are made to happen every time: a second request runs while the first is writing (the moment the old code
had already read what it was changing), and the test waits a moment for it. With the fix, the second request waits
for the first, so they happen one after the other."""
from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.usefixtures("safety_settled")

PW = "lab-bench-pw-2026"


def _person(client, admin, username, role="scientist"):
    client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                   "full_name": username.title(), "role": role})
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def lab(client, admin):
    return {u: _person(client, admin, u) for u in ("inv_ana", "inv_ben")}


def _meanwhile(monkeypatch, then):
    """Run `then` (another request, in a thread) while this request writes its first audit event, and wait up to a
    second for it. Returns a function that waits for `then` to finish and gives its answer."""
    from app import audit
    real = audit.record
    state: dict = {"fired": False}

    def record(*a, **kw):
        if not state["fired"]:
            state["fired"] = True
            t = threading.Thread(target=lambda: state.__setitem__("result", then()))
            t.start()
            t.join(1.0)
            state["thread"] = t
        return real(*a, **kw)
    monkeypatch.setattr(audit, "record", record)

    def done():
        assert state["fired"], "the request never wrote an audit event"
        state["thread"].join(60)
        return state.get("result")
    return done


def _material(client, h, name, **kw):
    r = client.post("/api/materials", headers=h, json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _lot(client, h, mat, amount=10, unit="g", **kw):
    r = client.post(f"/api/materials/{mat['id']}/lots", headers=h, json={"amount": amount, "unit": unit, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _state(client, h, lot_id):
    """(sum of the ledger, the lot's amount, its status, number of ledger lines)"""
    ledger = client.get(f"/api/lots/{lot_id}/transactions", headers=h).json()
    lot = next(x for x in client.get("/api/lots", headers=h).json() if x["id"] == lot_id)
    return round(sum(t["delta"] for t in ledger), 9), lot["amount"], lot["status"], len(ledger)


def _send(client, method, url, h, body):
    """JSON with NaN or Infinity in it, as Python's json module writes it (the test client won't)."""
    import json
    return client.request(method, url, headers={**h, "Content-Type": "application/json"}, content=json.dumps(body))


def _out(client, h, lot_id, amount=250, unit="mg", **kw):
    return client.post(f"/api/lots/{lot_id}/transactions", headers=h, json={"amount": amount, "unit": unit, "direction": "out", **kw})


# ------------------------------------------------------------------ D1: amounts


def test_two_withdrawals_at_once_both_count(client, lab, monkeypatch):
    a, b = lab["inv_ana"], lab["inv_ben"]
    lot = _lot(client, a, _material(client, a, "Race salt A"))
    done = _meanwhile(monkeypatch, lambda: _out(client, b, lot["id"]).status_code)
    assert _out(client, a, lot["id"]).status_code == 201
    assert done() == 201
    ledger, amount, _, n = _state(client, a, lot["id"])
    assert n == 3 and ledger == pytest.approx(9.5)
    assert amount == pytest.approx(9.5), f"the bottle says {amount} g; its ledger says {ledger} g"


def test_many_withdrawals_at_once_stop_at_empty_and_match_the_ledger(client, lab):
    a, b = lab["inv_ana"], lab["inv_ben"]
    lot = _lot(client, a, _material(client, a, "Race salt B"), amount=2.5)  # room for exactly ten
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda i: _out(client, (a, b)[i % 2], lot["id"]).status_code, range(16)))
    assert codes.count(201) == 10 and codes.count(409) == 6, codes
    ledger, amount, status, _ = _state(client, a, lot["id"])
    assert ledger == pytest.approx(0) and amount == pytest.approx(0) and status == "empty"


def test_one_withdrawal_undone_twice_at_once_goes_back_once(client, lab, monkeypatch):
    a, b = lab["inv_ana"], lab["inv_ben"]
    lot = _lot(client, a, _material(client, a, "Race salt C"))
    tx = _out(client, a, lot["id"], amount=2, unit="g").json()["transaction_id"]
    done = _meanwhile(monkeypatch, lambda: client.post(f"/api/lots/transactions/{tx}/reverse", headers=b).status_code)
    first = client.post(f"/api/lots/transactions/{tx}/reverse", headers=a).status_code
    assert sorted([first, done()]) == [201, 409]
    ledger, amount, _, n = _state(client, a, lot["id"])
    assert n == 3 and ledger == pytest.approx(10) and amount == pytest.approx(10)


def test_undoing_entry_1_isnt_refused_because_entry_12_was_undone(client, lab):
    """The "already reversed" check matched "Reversal of #12" when undoing #1 (any number starting with the same digits)."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import LotTransaction
    a = lab["inv_ana"]
    lot = _lot(client, a, _material(client, a, "Prefix salt"))
    tx = _out(client, a, lot["id"], amount=1, unit="g").json()["transaction_id"]
    with Session(engine) as s:  # as if entry 10×tx+1 had been undone (its note starts with this one's number)
        s.add(LotTransaction(lot_id=lot["id"], delta=0, balance_after=9, user_id=1, note=f"Reversal of #{tx}1 (someone else's)"))
        s.commit()
    assert client.post(f"/api/lots/transactions/{tx}/reverse", headers=a).status_code == 201
    assert client.post(f"/api/lots/transactions/{tx}/reverse", headers=a).status_code == 409  # now it really was


# ------------------------------------------------------------------ D2: duplicates


def test_the_same_structure_registered_twice_at_once_gets_one_number(client, lab, monkeypatch):
    a, b = lab["inv_ana"], lab["inv_ben"]
    smiles = "C" * 23 + "O"
    done = _meanwhile(monkeypatch, lambda: client.post("/api/materials", headers=b, json={"name": "Tricosanol (Ben)", "smiles": smiles}))
    first = client.post("/api/materials", headers=a, json={"name": "Tricosanol", "smiles": smiles})
    second = done()
    assert first.status_code == 201
    assert second is not None and second.status_code == 409, getattr(second, "text", second)
    assert first.json()["mid"] in second.json()["detail"]


def _experiment(client, h, title="Inventory record"):
    nb = client.post("/api/notebooks", headers=h, json={"name": f"NB for {title}"}).json()
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]})
    assert r.status_code == 201, r.text
    return client.get(f"/api/experiments/{r.json()['id']}", headers=h).json()


def test_a_product_registered_twice_at_once_is_one_material(client, lab, monkeypatch):
    a = lab["inv_ana"]
    exp = _experiment(client, a, "Register twice")
    body = {"experiment_id": exp["id"], "name": "Amine C31", "smiles": "C" * 31 + "N", "amount_mg": 120}
    done = _meanwhile(monkeypatch, lambda: client.post("/api/materials/register-product", headers=a, json=body))
    first = client.post("/api/materials/register-product", headers=a, json=body)
    second = done()
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["material"]["id"] == second.json()["material"]["id"], "two materials for one structure"


def test_two_lots_with_the_same_new_barcode_at_once(client, lab, monkeypatch):
    a, b = lab["inv_ana"], lab["inv_ben"]
    mat = _material(client, a, "Barcode race")
    body = {"amount": 1, "unit": "g", "barcode": "DBL-36-4-0001"}
    done = _meanwhile(monkeypatch, lambda: client.post(f"/api/materials/{mat['id']}/lots", headers=b, json=body))
    first = client.post(f"/api/materials/{mat['id']}/lots", headers=a, json=body)
    second = done()
    assert first.status_code == 201
    assert second is not None and second.status_code == 409, getattr(second, "text", second)
    # "Add anyway" still adds one on purpose
    assert client.post(f"/api/materials/{mat['id']}/lots", headers=a, json={**body, "allow_duplicate": True}).status_code == 201


def test_a_group_created_twice_at_once_is_one_group_and_a_plain_409(client, admin, monkeypatch):
    done = _meanwhile(monkeypatch, lambda: client.post("/api/groups", headers=admin, json={"name": "Double-clicked group"}))
    first = client.post("/api/groups", headers=admin, json={"name": "Double-clicked group"})
    second = done()
    assert first.status_code == 201
    assert second is not None and second.status_code == 409, "the second click gave a server error"
    assert [g["name"] for g in client.get("/api/groups", headers=admin).json()].count("Double-clicked group") == 1


def test_an_account_created_twice_at_once_is_a_plain_409(client, admin, monkeypatch):
    body = {"username": "dbl_user", "password": PW, "must_change": False, "full_name": "Double", "role": "scientist"}
    done = _meanwhile(monkeypatch, lambda: client.post("/api/users", headers=admin, json=body))
    first = client.post("/api/users", headers=admin, json=body)
    second = done()
    assert first.status_code == 201
    assert second is not None and second.status_code == 409, "the second click gave a server error"


def test_two_people_opening_a_new_records_hazards_at_once(client, lab, monkeypatch):
    """The page's safety panel makes a (still empty) safety record for each new chemical: two at once both made it,
    and the second page gave a server error (the record's key is unique)."""
    from app import safety as safety_mod
    a = lab["inv_ana"]
    exp = _experiment(client, a, "Hazards twice")
    rxn = next(e for e in exp["entries"] if e["kind"] == "reaction")
    row = {"role": "reactant", "name": "Hexadecanol", "smiles": "C" * 16 + "O", "limiting": True, "basis": "mass", "mass_mg": 10}
    assert client.put(f"/api/entries/{rxn['id']}", headers=a, json={"content": {"rows": [row]}}).status_code == 200
    from sqlmodel import Session
    real_add = Session.add
    fired = {}

    def add(self, obj, *args, **kw):  # the moment the first page has found the record missing and is making it
        if isinstance(obj, safety_mod.ChemSafety) and not fired:
            fired["t"] = t = threading.Thread(target=lambda: fired.__setitem__(
                "r", client.get(f"/api/experiments/{exp['id']}/safety", headers=a).status_code))
            t.start()
            t.join(1.0)
        return real_add(self, obj, *args, **kw)
    monkeypatch.setattr(Session, "add", add)
    assert client.get(f"/api/experiments/{exp['id']}/safety", headers=a).status_code == 200
    fired["t"].join(60)
    assert fired.get("r") == 200, "the second page gave a server error"


# ------------------------------------------------------------------ D3: the "taken from inventory" mark


def _reaction_with_lot(client, h, title):
    exp = _experiment(client, h, title)
    rxn = next(e for e in exp["entries"] if e["kind"] == "reaction")
    mat = _material(client, h, f"Benzoic acid for {title}")
    lot = _lot(client, h, mat, amount=25, unit="g")
    row = {"role": "reactant", "name": "Benzoic acid", "smiles": "", "mw": 122.12, "material_id": mat["id"], "lot_id": lot["id"],
           "limiting": True, "basis": "mass", "mass_mg": 50}
    return exp, rxn, mat, lot, row


def _save_rows(client, h, rxn, rows):
    r = _send(client, "PUT", f"/api/entries/{rxn['id']}", h, {"content": {"rows": rows}})
    assert r.status_code == 200, r.text
    return r.json()["content"]["rows"]


def test_the_taken_from_inventory_mark_is_kept_when_the_section_is_saved(client, lab):
    a = lab["inv_ana"]
    exp, rxn, mat, lot, row = _reaction_with_lot(client, a, "Deduct once")
    tx = _out(client, a, lot["id"], amount=50, unit="mg", experiment_id=exp["id"]).json()["transaction_id"]
    mark = {"tx_id": tx, "amount": 50, "unit": "mg"}
    saved = _save_rows(client, a, rxn, [{**row, "deducted": mark}])
    assert saved[0].get("deducted") == mark, "the mark was dropped, so Deduct is offered again"
    # and it's still there on the next save (the page sends the section back as the server stored it)
    again = _save_rows(client, a, rxn, [{**saved[0], "notes": "weighed on balance 2"}])
    assert again[0]["deducted"] == mark
    # undo: the withdrawal is reversed and the page clears the mark
    assert client.post(f"/api/lots/transactions/{tx}/reverse", headers=a).status_code == 201
    cleared = _save_rows(client, a, rxn, [{**again[0], "deducted": None}])
    assert cleared[0].get("deducted") is None


def test_a_mark_is_kept_only_for_a_real_withdrawal_for_that_row(client, lab):
    a, b = lab["inv_ana"], lab["inv_ben"]
    exp, rxn, mat, lot, row = _reaction_with_lot(client, a, "Made-up marks")
    other_lot = _lot(client, a, mat, amount=5)
    mine = _out(client, a, lot["id"], amount=50, unit="mg", experiment_id=exp["id"]).json()["transaction_id"]
    no_exp = _out(client, a, lot["id"], amount=50, unit="mg").json()["transaction_id"]
    on_other_lot = _out(client, a, other_lot["id"], amount=50, unit="mg", experiment_id=exp["id"]).json()["transaction_id"]
    top_up = client.post(f"/api/lots/{lot['id']}/transactions", headers=a,
                         json={"amount": 1, "unit": "g", "direction": "in", "experiment_id": exp["id"]}).json()["transaction_id"]
    undone = _out(client, a, lot["id"], amount=50, unit="mg", experiment_id=exp["id"]).json()["transaction_id"]
    assert client.post(f"/api/lots/transactions/{undone}/reverse", headers=a).status_code == 201
    other = _experiment(client, b, "Someone else's record")
    theirs_lot = _lot(client, b, _material(client, b, "Their acid"))
    theirs = _out(client, b, theirs_lot["id"], amount=1, unit="g", experiment_id=other["id"]).json()["transaction_id"]
    bad = [
        {"tx_id": 999_999, "amount": 50, "unit": "mg"},  # no such entry
        {"tx_id": no_exp, "amount": 50, "unit": "mg"},  # not for this experiment
        {"tx_id": on_other_lot, "amount": 50, "unit": "mg"},  # another lot than the row's
        {"tx_id": top_up, "amount": 1, "unit": "g"},  # nothing was taken out
        {"tx_id": undone, "amount": 50, "unit": "mg"},  # undone since
        {"tx_id": theirs, "amount": 1, "unit": "g"},  # someone else's
        {"tx_id": str(mine), "amount": 50, "unit": "mg"}, {"tx_id": True, "amount": 50, "unit": "mg"},
        {"tx_id": mine, "amount": "50", "unit": "mg"}, {"tx_id": mine, "amount": -50, "unit": "mg"},
        {"tx_id": mine, "amount": float("inf"), "unit": "mg"}, {"tx_id": mine, "amount": 50, "unit": "mol"},
        {"tx_id": mine, "amount": 50}, "yes", 1, True, [mine],
    ]
    for mark in bad:
        saved = _save_rows(client, a, rxn, [{**row, "deducted": mark}])
        assert saved[0].get("deducted") is None, mark
    extra = _save_rows(client, a, rxn, [{**row, "deducted": {"tx_id": mine, "amount": 50, "unit": "mg", "html": "<b>x</b>"}}])
    assert extra[0]["deducted"] == {"tx_id": mine, "amount": 50, "unit": "mg"}  # exactly that shape


def test_a_copy_hasnt_used_anything(client, lab):
    a = lab["inv_ana"]
    exp, rxn, mat, lot, row = _reaction_with_lot(client, a, "Copied after deducting")
    tx = _out(client, a, lot["id"], amount=50, unit="mg", experiment_id=exp["id"]).json()["transaction_id"]
    _save_rows(client, a, rxn, [{**row, "deducted": {"tx_id": tx, "amount": 50, "unit": "mg"}}])
    for params in ({}, {"as_template": "true"}):
        r = client.post(f"/api/experiments/{exp['id']}/clone", headers=a, params=params)
        assert r.status_code in (200, 201), r.text
        copy = client.get(f"/api/experiments/{r.json()['id']}", headers=a).json()
        rows = next(e for e in copy["entries"] if e["kind"] == "reaction")["content"]["rows"]
        assert "deducted" not in rows[0], params


# ------------------------------------------------------------------ D4: values that make no sense


@pytest.mark.parametrize("density", [-1, 0, -0.5, 1e300, float("inf"), float("nan")])
def test_density_must_be_a_positive_number(client, lab, density):
    a = lab["inv_ana"]
    r = _send(client, "POST", "/api/materials", a, {"name": f"Bad density {density}", "density": density})
    assert r.status_code == 422, (density, r.text)
    m = _material(client, a, f"Good density for {density}", density=0.79)
    assert _send(client, "PATCH", f"/api/materials/{m['id']}", a, {"density": density}).status_code == 422
    assert client.patch(f"/api/materials/{m['id']}", headers=a, json={"density": None}).status_code == 200  # cleared is fine


def test_molecular_weight_must_be_positive(client, lab):
    a = lab["inv_ana"]
    assert client.post("/api/materials", headers=a, json={"name": "Negative MW", "mw": -5}).status_code == 422
    m = _material(client, a, "Polymer", mw=150000)
    assert client.patch(f"/api/materials/{m['id']}", headers=a, json={"mw": 0}).status_code == 422


def test_purity_status_and_amounts_must_make_sense(client, lab):
    a = lab["inv_ana"]
    m = _material(client, a, "Odd lot")
    lot = _lot(client, a, m, purity=98.5)
    for body in ({"status": "banana"}, {"status": ""}, {"purity": -50}, {"purity": 250}, {"purity": 1e300}, {"purity": float("nan")}):
        r = _send(client, "PATCH", f"/api/lots/{lot['id']}", a, body)
        assert r.status_code == 422, (body, r.text)
    for body in ({"status": "quarantined"}, {"purity": 0}, {"purity": 100}, {"purity": None}, {"status": "available"}):
        assert client.patch(f"/api/lots/{lot['id']}", headers=a, json=body).status_code == 200, body
    assert client.post(f"/api/materials/{m['id']}/lots", headers=a, json={"amount": 1, "purity": 101}).status_code == 422
    exp = _experiment(client, a, "Odd product")
    for body in ({"purity": -1}, {"amount_mg": -120}, {"amount_mg": float("inf")}):
        r = _send(client, "POST", "/api/materials/register-product", a, {"experiment_id": exp["id"], "name": "Odd product", **body})
        assert r.status_code == 422, (body, r.text)
    # a lot can't be topped up past what can be recorded
    assert client.post(f"/api/lots/{lot['id']}/transactions", headers=a,
                       json={"amount": 9.99e11, "unit": "kg", "direction": "in"}).status_code == 400


def test_a_withdrawal_never_adds_to_a_lot(client, lab):
    """Withdrawing 5 g from a 100 mL bottle whose density was -1 added 5 mL. Such densities can't be entered any more;
    one already in the database is treated as no density."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Material
    a = lab["inv_ana"]
    m = _material(client, a, "Old negative density", density=0.9)
    lot = _lot(client, a, m, amount=100, unit="mL")
    with Session(engine) as s:
        s.get(Material, m["id"]).density = -1.0
        s.commit()
    r = _out(client, a, lot["id"], amount=5, unit="g")
    assert r.status_code == 400 and "density" in r.json()["detail"]
    assert _state(client, a, lot["id"])[1] == 100


def test_percent_and_underscore_are_just_characters_in_a_search(client, lab):
    a = lab["inv_ana"]
    m = _material(client, a, "Sodium 50% w/w solution")
    _lot(client, a, m, location="Shelf_B2")
    _lot(client, a, _material(client, a, "Plain silica"), location="ShelfXB2")
    names = [x["name"] for x in client.get("/api/materials", headers=a, params={"q": "%"}).json()]
    assert names == ["Sodium 50% w/w solution"], names
    lots = client.get("/api/lots", headers=a, params={"q": "%"}).json()
    assert {x["material_name"] for x in lots} == {"Sodium 50% w/w solution"}
    assert [x["location"] for x in client.get("/api/lots", headers=a, params={"location": "f_b"}).json()] == ["Shelf_B2"]
    assert client.get("/api/materials", headers=a, params={"q": "50% W/W"}).json()[0]["name"] == "Sodium 50% w/w solution"


def test_bad_values_already_in_the_database_dont_stop_pages_loading(client, lab):
    """Values these checks now refuse may be in older labs' data: the pages still load, and other changes still save."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import Lot, Material
    a = lab["inv_ana"]
    m = _material(client, a, "Old odd values")
    lot = _lot(client, a, m)
    with Session(engine) as s:
        mm, ll = s.get(Material, m["id"]), s.get(Lot, lot["id"])
        mm.density, mm.mw = -1.0, -5.0
        ll.status, ll.purity = "banana", 250.0
        s.commit()
    assert client.get(f"/api/materials/{m['id']}", headers=a).status_code == 200
    assert client.get("/api/materials", headers=a).status_code == 200
    assert any(x["id"] == lot["id"] for x in client.get("/api/lots", headers=a).json())
    assert client.get("/api/scan", headers=a, params={"code": lot["barcode"]}).json()["lots"][0]["status"] == "banana"
    r = client.patch(f"/api/lots/{lot['id']}", headers=a, json={"location": "Fridge 3", "status": "banana"})  # unchanged
    assert r.status_code == 200 and r.json()["location"] == "Fridge 3"
    assert client.patch(f"/api/materials/{m['id']}", headers=a, json={"notes": "checked"}).status_code == 200
    assert not math.isnan(client.get(f"/api/materials/{m['id']}", headers=a).json()["density"])
