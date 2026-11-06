"""0.32: scanning a bottle's barcode.

GET /api/scan?code=… answers with every lot that has exactly that barcode (in any case, without the spaces or the
Enter a scanner adds), with its material and hazard pictograms; when no lot has it, with the record the code names
(EXP-, MAT-) or the experiment a sample code was handed to, if the person may open it.

A lot can't be given a barcode another lot already has (409, naming the other lot) unless the request says so with
`allow_duplicate`: a supplier's barcode is often the same on every bottle of a batch, a container label is not."""
import pytest

pytestmark = pytest.mark.usefixtures("safety_settled")

PW = "lab-bench-pw-2026"


def _person(client, admin, username):
    client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                   "full_name": username.title(), "role": "scientist"})
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    out = {"session": {"Authorization": f"Bearer {r.json()['token']}"}}
    for scope in ("read", "write"):
        t = client.post("/api/auth/tokens", headers=out["session"], json={"name": f"scan {scope}", "scope": scope})
        assert t.status_code == 201, t.text
        out[scope] = {"Authorization": f"Bearer {t.json()['token']}"}
    return out


@pytest.fixture(scope="module")
def me(client, admin):
    return _person(client, admin, "scanner")


@pytest.fixture(scope="module")
def toluene(client, me):
    m = client.post("/api/materials", headers=me["write"], json={"name": "Scan toluene", "kind": "solvent",
                                                                  "smiles": "Cc1ccccc1C", "density": 0.87})
    assert m.status_code == 201, m.text
    return m.json()


def _lot(client, me, mat, barcode, **kw):
    return client.post(f"/api/materials/{mat['id']}/lots", headers=me["write"],
                       json={"amount": 100, "unit": "mL", "barcode": barcode, **kw})


def _scan(client, headers, code):
    r = client.get("/api/scan", headers=headers, params={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


def test_one_lot_exact_any_case_and_trimmed(client, me, toluene):
    lot = _lot(client, me, toluene, "V32-VIM-000123", location="Flammables cabinet 2").json()
    for code in ("V32-VIM-000123", "v32-vim-000123", "  V32-VIM-000123\r\n", "\tV32-VIM-000123\x00"):
        found = _scan(client, me["read"], code)  # a read-only token is enough
        assert [x["id"] for x in found["lots"]] == [lot["id"]], code
        assert found["code"] == code.strip(" \r\n\t\x00") and found["record"] is None
    x = found["lots"][0]
    assert x["material"]["name"] == "Scan toluene" and x["material"]["id"] == toluene["id"]
    assert x["material"]["mid"] == toluene["mid"] and x["material"]["smiles"] and x["material"]["kind"] == "solvent"
    assert x["location"] == "Flammables cabinet 2" and x["amount"] == 100 and x["status"] == "available"
    assert "safety" in x
    # exact: part of a barcode is not a match (the Inventory search box is for that)
    assert _scan(client, me["read"], "V32-VIM-00012")["lots"] == []
    assert _scan(client, me["read"], "VIM-000123")["lots"] == []


def test_no_lot_and_an_empty_code(client, me):
    found = _scan(client, me["read"], "V32-NOTHING-HERE")
    assert found == {"code": "V32-NOTHING-HERE", "lots": [], "record": None}
    assert client.get("/api/scan", headers=me["read"], params={"code": " \r\n"}).status_code == 422


def test_duplicate_barcode_is_refused_unless_allowed(client, admin, me, toluene):
    first = _lot(client, me, toluene, "V32-SUPPLIER-BATCH-7").json()
    again = _lot(client, me, toluene, "v32-supplier-batch-7 ")  # same code, other case, a trailing space
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == f"{toluene['mid']}-L{first['id']:04d} (Scan toluene) already has this barcode."
    ok = _lot(client, me, toluene, "V32-SUPPLIER-BATCH-7 ", allow_duplicate=True)
    assert ok.status_code == 201, ok.text
    assert ok.json()["barcode"] == "V32-SUPPLIER-BATCH-7"  # stored without the space
    third = _lot(client, me, toluene, "V32-SUPPLIER-BATCH-7")
    assert third.status_code == 409 and "and 1 other lot already have this barcode." in third.json()["detail"]
    # scanning it shows every bottle, to pick from
    found = _scan(client, me["read"], "V32-SUPPLIER-BATCH-7")
    assert [x["id"] for x in found["lots"]] == [first["id"], ok.json()["id"]]
    # the audit trail says it was a deliberate duplicate
    events = client.get("/api/audit", headers=admin, params={"object_type": "lot", "username": "scanner"}).json()
    made = next(e for e in events if e["object_id"] == ok.json()["id"] and e["action"] == "create")
    assert made["details"]["same_barcode_as"] == [first["id"]]
    # a lot made without a barcode still gets its own
    auto = client.post(f"/api/materials/{toluene['id']}/lots", headers=me["write"], json={"amount": 1, "unit": "mL"})
    assert auto.status_code == 201 and auto.json()["barcode"] == f"{toluene['mid']}-L{auto.json()['id']:04d}"


def test_changing_a_barcode(client, me, toluene):
    a = _lot(client, me, toluene, "V32-CHANGE-A").json()
    b = _lot(client, me, toluene, "V32-CHANGE-B").json()
    r = client.patch(f"/api/lots/{b['id']}", headers=me["write"], json={"barcode": "V32-CHANGE-A"})
    assert r.status_code == 409 and r.json()["detail"] == f"{toluene['mid']}-L{a['id']:04d} (Scan toluene) already has this barcode."
    assert client.get("/api/scan", headers=me["read"], params={"code": "V32-CHANGE-B"}).json()["lots"][0]["id"] == b["id"]
    r = client.patch(f"/api/lots/{b['id']}", headers=me["write"], json={"barcode": "V32-CHANGE-A", "allow_duplicate": True})
    assert r.status_code == 200 and r.json()["barcode"] == "V32-CHANGE-A"
    # its own barcode isn't a duplicate of itself; a new one is stored trimmed
    assert client.patch(f"/api/lots/{a['id']}", headers=me["write"], json={"barcode": "V32-CHANGE-A"}).status_code == 200
    r = client.patch(f"/api/lots/{a['id']}", headers=me["write"], json={"barcode": " V32-CHANGE-C\n", "location": "Shelf 9"})
    assert r.status_code == 200 and r.json()["barcode"] == "V32-CHANGE-C" and r.json()["location"] == "Shelf 9"
    assert [x["id"] for x in _scan(client, me["read"], "V32-CHANGE-C")["lots"]] == [a["id"]]
    # cleared: Benchtop's own barcode again
    r = client.patch(f"/api/lots/{a['id']}", headers=me["write"], json={"barcode": ""})
    assert r.status_code == 200 and r.json()["barcode"] == f"{toluene['mid']}-L{a['id']:04d}"
    # a read-only token can't change it
    assert client.patch(f"/api/lots/{a['id']}", headers=me["read"], json={"barcode": "V32-X"}).status_code == 403


def test_upc_and_gs1_codes_match_however_they_arrive(client, me, toluene):
    upc = _lot(client, me, toluene, "036000291452").json()  # UPC-A, as printed
    assert [x["id"] for x in _scan(client, me["read"], "0036000291452")["lots"]] == [upc["id"]]  # read as EAN-13
    ean = _lot(client, me, toluene, "0012345678905").json()
    assert [x["id"] for x in _scan(client, me["read"], "012345678905")["lots"]] == [ean["id"]]
    gs1 = _lot(client, me, toluene, "(01)09506000134352(10)V32LOT").json()  # typed as printed
    raw = "0109506000134352\x1d10V32LOT"  # as a scanner sends the same Data Matrix
    assert [x["id"] for x in _scan(client, me["read"], raw)["lots"]] == [gs1["id"]]
    assert [x["id"] for x in _scan(client, me["read"], "(01)09506000134352(10)v32lot")["lots"]] == [gs1["id"]]
    # and it counts as the same barcode when another bottle is added
    assert _lot(client, me, toluene, raw).status_code == 409


def test_record_and_sample_codes(client, admin, me, toluene):
    nb = client.post("/api/notebooks", headers=me["session"], json={"name": "Scan notebook"}).json()
    exp = client.post("/api/experiments", headers=me["write"], json={"title": "Scan me", "notebook_id": nb["id"]}).json()
    found = _scan(client, me["read"], exp["eid"].lower())
    assert found["lots"] == [] and found["record"]["kind"] == "experiment" and found["record"]["id"] == exp["id"]
    found = _scan(client, me["read"], toluene["mid"])
    assert found["record"] == {**found["record"], "kind": "material", "id": toluene["id"], "code": toluene["mid"]}
    # a sample code: the experiment it was handed to
    assert client.put("/api/auth/me/sample-prefix", headers=me["session"], json={"prefix": "SCN"}).status_code == 200
    sc = client.post(f"/api/experiments/{exp['id']}/samples", headers=me["session"], json={})
    assert sc.status_code == 201, sc.text
    found = _scan(client, me["read"], sc.json()["code"].replace("-", "–"))  # a word processor's dash, as ⌘K takes it
    assert found["record"]["kind"] == "sample" and found["record"]["id"] == exp["id"]
    assert found["record"]["code"] == sc.json()["code"] and found["record"]["experiment"] == exp["eid"]


def test_records_you_cant_open_stay_hidden(client, admin, me):
    other = _person(client, admin, "scanner2")
    nb = client.post("/api/notebooks", headers=other["session"], json={"name": "Scan private", "visibility": "private"}).json()
    exp = client.post("/api/experiments", headers=other["write"], json={"title": "Not yours", "notebook_id": nb["id"]}).json()
    assert client.put("/api/auth/me/sample-prefix", headers=other["session"], json={"prefix": "SCX"}).status_code == 200
    sc = client.post(f"/api/experiments/{exp['id']}/samples", headers=other["session"], json={}).json()
    assert _scan(client, other["read"], exp["eid"])["record"]["id"] == exp["id"]
    assert _scan(client, other["read"], sc["code"])["record"]["id"] == exp["id"]
    for code in (exp["eid"], sc["code"]):
        found = _scan(client, me["read"], code)
        assert found == {"code": code, "lots": [], "record": None}, code
