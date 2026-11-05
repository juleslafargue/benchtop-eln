"""The API contract: what docs/API.md promises to scripts, checked with personal access tokens (0.31).

docs/API.md says its endpoints, parameters, fields and error codes stay stable across releases. Each test here calls
documented endpoints with a `bt_…` token and checks the documented fields are there with the documented types, so a
field renamed or removed by accident fails here before it reaches a release. The Python examples in the file's
inventory, registry, sample-code and search sections are run as written, too.

If you change something documented on purpose: change docs/API.md and this file together, and say so in the release
notes."""
import io
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import doi, instruments
from app.main import app

pytestmark = pytest.mark.usefixtures("safety_settled")  # see conftest.py

PW = "lab-bench-pw-2026"
DOC =Path(__file__).resolve().parents[2] / "docs" / "API.md"
NUM = (int, float)
NONE = type(None)
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")

# ------------------------------------------------------------------ documented shapes

USER = {"id": int, "username": str, "full_name": str}
EXPERIMENT = {"id": int, "eid": str, "title": str, "description": str, "status": str, "notebook_id": (int, NONE),
              "notebook_name": (str, NONE), "is_template": bool, "template_id": (int, NONE), "tags": list, "owner": dict,
              "created_at": "time", "updated_at": "time"}
EXPERIMENT_LISTED = {**EXPERIMENT, "sample_codes": list}
MATERIAL = {"id": int, "mid": str, "name": str, "kind": str, "cas": str, "smiles": str, "formula": str, "mw": (*NUM, NONE),
            "inchikey": str, "density": (*NUM, NONE), "synonyms": str, "hazards": str, "notes": str, "created_at": "time",
            "lot_count": int, "available_lots": int}
LOT = {"id": int, "material_id": int, "barcode": str, "supplier": str, "catalog_number": str, "purity": (*NUM, NONE),
       "amount": NUM, "unit": str, "location": str, "status": str, "received_at": "time", "expires_at": ("time", NONE)}
LOT_LISTED = {**LOT, "material_name": str, "mid": str}
LEDGER = {"id": int, "delta": NUM, "balance_after": NUM, "note": str, "created_at": "time", "user": dict,
          "experiment": (dict, NONE)}
COMPOUND = {"name": str, "smiles": str, "formula": str, "mw": NUM, "logp": NUM, "tpsa": NUM, "hbd": int, "hba": int,
            "rotatable_bonds": int, "heavy_atoms": int, "aromatic_rings": int, "fsp3": NUM, "qed": (*NUM, NONE),
            "lipinski_violations": list, "veber_violations": list, "experiments": list, "best_yield": (*NUM, NONE),
            "material": (dict, NONE)}
COMPOUND_EXPERIMENT = {"id": int, "eid": str, "title": str, "status": str, "date": "time", "yield_pct": (*NUM, NONE)}
SEARCH_EXPERIMENT = {**EXPERIMENT, "score": NUM, "match": str}
SEARCH_MATERIAL = {"id": int, "mid": str, "name": str, "smiles": str, "formula": str, "mw": (*NUM, NONE), "cas": str,
                   "kind": str, "score": NUM}
SEARCH_FILE = {"id": int, "filename": str, "size": int, "experiment_id": int, "eid": str, "title": str, "created_at": "time"}
REFERENCE = {k: str for k in ("doi", "title", "journal", "year", "volume", "issue", "pages", "note")} | {"authors": list}


def shaped(obj, shape, what="answer"):
    """obj has every field in shape, each of the documented type ("time": UTC, ISO 8601, ending in Z)."""
    assert isinstance(obj, dict), f"{what}: expected an object, got {obj!r}"
    for field, kinds in shape.items():
        assert field in obj, f"{what}: documented field {field!r} is missing (has {sorted(obj)})"
        kinds = kinds if isinstance(kinds, tuple) else (kinds,)
        v = obj[field]
        ok = False
        for k in kinds:
            if k == "time":
                ok = ok or (isinstance(v, str) and bool(TIME.match(v)))
            elif k is int:
                ok = ok or (isinstance(v, int) and not isinstance(v, bool))
            elif k is float:
                ok = ok or (isinstance(v, (int, float)) and not isinstance(v, bool))
            else:
                ok = ok or isinstance(v, k)
        assert ok, f"{what}: field {field!r} is {v!r}, documented as {kinds}"
    return obj


def all_shaped(rows, shape, what):
    assert isinstance(rows, list), f"{what}: expected a list, got {type(rows).__name__}"
    for r in rows:
        shaped(r, shape, what)
    return rows


# ------------------------------------------------------------------ people and tokens


def _person(client, admin, username, role="scientist"):
    client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                   "full_name": username.title(), "role": role})
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    session = {"Authorization": f"Bearer {r.json()['token']}"}
    out = {"session": session, "id": r.json()["user"]["id"] if "user" in r.json() else None}
    for scope in ("read", "write"):
        if scope == "write" and role == "viewer":
            continue
        t = client.post("/api/auth/tokens", headers=session, json={"name": f"contract {scope}", "scope": scope})
        assert t.status_code == 201, t.text
        assert t.json()["token"].startswith("bt_")
        out[scope] = {"Authorization": f"Bearer {t.json()['token']}"}
    return out


@pytest.fixture(scope="module")
def me(client, admin):
    return _person(client, admin, "contract")


@pytest.fixture(scope="module")
def other(client, admin):
    """Someone else, with a private notebook: their experiment is one `me` can't open."""
    p = _person(client, admin, "contract2")
    nb = client.post("/api/notebooks", headers=p["session"], json={"name": "Contract private", "visibility": "private"}).json()
    exp = client.post("/api/experiments", headers=p["write"], json={"title": "Not yours", "notebook_id": nb["id"]}).json()
    return {**p, "notebook": nb, "experiment": exp}


@pytest.fixture(scope="module")
def nb(client, me):
    r = client.post("/api/notebooks", headers=me["session"], json={"name": "Contract notebook"})
    assert r.status_code in (200, 201), r.text
    return r.json()


def new_exp(client, me, nb, title):
    r = client.post("/api/experiments", headers=me["write"], json={"title": title, "notebook_id": nb["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def total(r):
    assert "X-Total-Count" in r.headers, f"{r.request.url}: no X-Total-Count header"
    return int(r.headers["X-Total-Count"])


# ------------------------------------------------------------------ tokens: who can do what


WRITES = [  # every documented endpoint that changes something; a read-only token gets 403 before anything else
    ("POST", "/api/materials"), ("PATCH", "/api/materials/1"), ("POST", "/api/materials/1/lots"), ("PATCH", "/api/lots/1"),
    ("POST", "/api/lots/1/transactions"), ("POST", "/api/lots/transactions/1/reverse"),
    ("POST", "/api/materials/register-product"), ("POST", "/api/experiments/1/attachments"),
    ("POST", "/api/inbox/1/assign"), ("POST", "/api/inbox/1/follow-up"), ("POST", "/api/inbox/1/ignore"),
    ("POST", "/api/inbox/1/unignore"), ("POST", "/api/experiments/1/follow-up"), ("POST", "/api/attachments/1/not-mine"),
    ("POST", "/api/eln/preview"), ("POST", "/api/eln/import/x"), ("DELETE", "/api/eln/import/x"),
]


def test_read_only_tokens_get_403_on_every_documented_write(client, me):
    for method, path in WRITES:
        r = client.request(method, path, headers=me["read"], json={})
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json()["detail"] == "This API token is read-only"


def test_a_bad_token_gets_401_and_human_only_actions_get_403(client, me, nb):
    for bad in ("bt_not-a-real-token", "garbage"):
        assert client.get("/api/materials", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    exp = new_exp(client, me, nb, "Tokens never sign")
    for action in ("sign", "witness", "void", "withdraw"):
        r = client.post(f"/api/experiments/{exp['id']}/{action}", headers=me["write"], json={"password": PW})
        assert r.status_code == 403, (action, r.status_code)
    assert client.get("/api/auth/tokens", headers=me["write"]).status_code == 403
    assert isinstance(r.json()["detail"], str)


# ------------------------------------------------------------------ paging


def test_documented_limits_defaults_and_maximum(client):
    spec = client.get("/openapi.json").json()["paths"]
    documented = {"/api/experiments": 200, "/api/materials": 500, "/api/lots": None, "/api/lots/{lot_id}/transactions": None,
                  "/api/compounds": None, "/api/search": 100}
    for path, default in documented.items():
        params = {p["name"]: p for p in spec[path]["get"]["parameters"]}
        lim, off = params["limit"]["schema"], params["offset"]["schema"]
        lim = next((s for s in lim.get("anyOf", [lim]) if s.get("type") == "integer"), lim)
        assert (lim.get("minimum"), lim.get("maximum")) == (1, 1000), path
        assert params["limit"]["schema"].get("default") == default, path
        assert off.get("minimum") == 0 and off.get("default") == 0, path


def test_paging_with_x_total_count(client, me, nb):
    for i in range(5):
        new_exp(client, me, nb, f"Contractpage {i}")
    r = client.get("/api/experiments", headers=me["read"], params={"notebook_id": nb["id"], "limit": 2})
    n = total(r)
    assert n >= 5 and len(r.json()) == 2
    seen = list(r.json())
    offset = 2
    while offset < n:
        page = client.get("/api/experiments", headers=me["read"], params={"notebook_id": nb["id"], "limit": 2, "offset": offset})
        assert total(page) == n
        seen += page.json()
        offset += 2
    assert len(seen) == n == len({e["id"] for e in seen})
    past = client.get("/api/experiments", headers=me["read"], params={"notebook_id": nb["id"], "offset": n + 10})
    assert past.json() == [] and total(past) == n  # past the end: an empty page, same total
    for bad in ({"limit": 0}, {"limit": 1001}, {"offset": -1}):
        r = client.get("/api/experiments", headers=me["read"], params=bad)
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["query", next(iter(bad))]


# ------------------------------------------------------------------ experiments, attachments, Inbox, .eln, DOI


def test_experiments_list(client, me, nb, other):
    exp = new_exp(client, me, nb, "Contract listed")
    r = client.get("/api/experiments", headers=me["read"], params={"notebook_id": nb["id"]})
    rows = all_shaped(r.json(), EXPERIMENT_LISTED, "GET /api/experiments")
    shaped(next(e for e in rows if e["id"] == exp["id"])["owner"], USER, "experiment owner")
    assert all(e["notebook_id"] == nb["id"] for e in rows)
    assert all(e["status"] == "open" for e in client.get("/api/experiments", headers=me["read"], params={"status": "open"}).json())
    mine = client.get("/api/experiments", headers=me["read"], params={"owner_id": exp["owner"]["id"]}).json()
    assert mine and all(e["owner"]["id"] == exp["owner"]["id"] for e in mine)
    tpl = client.post("/api/experiments", headers=me["write"], json={"title": "Contract template", "is_template": True}).json()
    assert tpl["id"] in [e["id"] for e in client.get("/api/experiments", headers=me["read"], params={"templates": "true"}).json()]
    everything = client.get("/api/experiments", headers=me["read"], params={"limit": 1000}).json()
    assert other["experiment"]["id"] not in [e["id"] for e in everything]  # only experiments you can open


def test_attachment_upload(client, me, nb):
    exp = new_exp(client, me, nb, "Contract upload")
    r = client.post(f"/api/experiments/{exp['id']}/attachments", headers=me["write"],
                    files={"file": ("run42_HPLC.csv", io.BytesIO(b"t,mAU\n0,1\n"), "text/csv")})
    assert r.status_code in (200, 201), r.text
    got = client.get(f"/api/experiments/{exp['id']}", headers=me["read"]).json()
    assert [a["filename"] for a in got["attachments"]] == ["run42_HPLC.csv"]


T0 = datetime(2031, 3, 1, 9, 0, 0)


def _arrive(folder: Path, name: str, data: bytes):
    instruments.scan_all(now=T0)
    (folder / name).write_bytes(data)
    instruments.scan_all(now=T0 + timedelta(seconds=10))
    instruments.scan_all(now=T0 + timedelta(seconds=200))


def test_inbox_follow_ups_and_not_mine(client, admin, me, nb, tmp_path):
    folder = tmp_path / "plate-reader"
    folder.mkdir()
    inst = client.post("/api/instruments", headers=admin, json={"name": "Contract reader", "kind": "files", "folder": str(folder)})
    assert inst.status_code == 201, inst.text
    try:
        # a file with no experiment number waits in the Inbox
        _arrive(folder, "contract unlabelled.csv", b"well,abs\nA1,0.5\n")
        r = client.get("/api/inbox", headers=me["read"], params={"state": "inbox"})
        item = next(i for i in r.json() if i["path"] == "contract unlabelled.csv")
        shaped(item, {"id": int, "path": str, "state": str, "files": list, "matched": list, "follow_up": bool}, "Inbox item")
        assert client.get(f"/api/inbox/{item['id']}/files/0", headers=me["read"]).content == b"well,abs\nA1,0.5\n"
        assert client.get(f"/api/inbox/{item['id']}/files/9", headers=me["read"]).status_code == 404
        assert client.post(f"/api/inbox/{item['id']}/ignore", headers=me["write"]).json()["state"] == "ignored"
        assert any(i["id"] == item["id"] for i in client.get("/api/inbox", headers=me["read"], params={"state": "ignored"}).json())
        assert client.post(f"/api/inbox/{item['id']}/unignore", headers=me["write"]).json()["state"] == "inbox"
        draft = new_exp(client, me, nb, "Contract assign target")
        r = client.post(f"/api/inbox/{item['id']}/assign", headers=me["write"], json={"experiment_id": draft["id"]})
        assert r.status_code == 200, r.text
        assert any(i["id"] == item["id"] for i in client.get("/api/inbox", headers=me["read"], params={"state": "attached"}).json())
        att = client.get(f"/api/experiments/{draft['id']}", headers=me["read"]).json()["attachments"][0]
        r = client.post(f"/api/attachments/{att['id']}/not-mine", headers=me["write"])  # back to the Inbox
        assert r.status_code == 200, r.text
        assert any(i["id"] == item["id"] for i in client.get("/api/inbox", headers=me["read"]).json())

        # a file naming a signed record: a follow-up, with the file in it
        signed = new_exp(client, me, nb, "Contract signed")
        assert client.post(f"/api/experiments/{signed['id']}/sign", headers=me["session"], json={"password": PW}).status_code == 200
        _arrive(folder, f"{signed['eid']} late HRMS.pdf", b"%PDF contract")
        item = next(i for i in client.get("/api/inbox", headers=me["read"]).json() if signed["eid"] in i["path"])
        assert item["follow_up"] is True
        r = client.post(f"/api/inbox/{item['id']}/follow-up", headers=me["write"], json={"notebook_id": nb["id"]})
        assert r.status_code == 201, r.text
        shaped(r.json(), {"id": int, "eid": str}, "follow-up")
        r = client.post(f"/api/experiments/{signed['id']}/follow-up", headers=me["write"], json={})
        assert r.status_code == 201, r.text
        shaped(r.json(), {"id": int, "eid": str}, "follow-up")
    finally:
        client.delete(f"/api/instruments/{inst.json()['id']}", headers=admin)


def test_eln_exchange(client, me, nb):
    exp = new_exp(client, me, nb, "Contract exchange")
    for path in (f"/api/experiments/{exp['id']}/export.eln", f"/api/notebooks/{nb['id']}/export.eln", "/api/export.eln"):
        r = client.get(path, headers=me["read"])
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.eln+zip"), path
        assert zipfile.is_zipfile(io.BytesIO(r.content))
    data = client.get(f"/api/experiments/{exp['id']}/export.eln", headers=me["read"]).content
    p = client.post("/api/eln/preview", headers=me["write"], files={"file": ("x.eln", io.BytesIO(data), "application/zip")})
    assert p.status_code == 200, p.text
    p = shaped(p.json(), {"token": str, "items": list}, "eln preview")
    for it in p["items"]:
        shaped(it, {"key": str, "already": (dict, NONE)}, "eln preview item")
    assert p["items"][0]["already"] is None
    r = client.post(f"/api/eln/import/{p['token']}", headers=me["write"], json={"notebook_id": nb["id"], "items": [p["items"][0]["key"]]})
    assert r.status_code == 200, r.text
    shaped(r.json(), {"created": list, "failed": list}, "eln import")
    p2 = client.post("/api/eln/preview", headers=me["write"], files={"file": ("x.eln", io.BytesIO(data), "application/zip")}).json()
    assert client.delete(f"/api/eln/import/{p2['token']}", headers=me["write"]).status_code == 204


def _doi_org(request: httpx.Request) -> httpx.Response:
    if request.url.host == "doi.org":
        if "nope" in request.url.path:
            return httpx.Response(404, text="DOI Not Found")
        if "junk" in request.url.path:
            return httpx.Response(302, headers={"location": "https://api.crossref.org/v1/works/junk/transform"})
        return httpx.Response(302, headers={"location": "https://api.crossref.org/v1/works/10.1021%2Fjo971176v/transform"})
    if "junk" in request.url.path:
        return httpx.Response(200, text="<html>Not JSON</html>")
    return httpx.Response(200, json={"type": "article-journal", "DOI": "10.1021/jo971176v", "title": "NMR Chemical Shifts",
                                     "author": [{"given": "Hugo E.", "family": "Gottlieb"}], "container-title": "J. Org. Chem.",
                                     "issued": {"date-parts": [[1997]]}, "volume": "62", "issue": "21", "page": "7512-7515"})


def test_doi_lookup(client, admin, me):
    assert client.get("/api/doi", headers=me["write"], params={"q": "not a doi"}).status_code == 400
    assert client.get("/api/doi", headers=me["write"], params={"q": "10.1021/jo971176v"}).status_code == 503  # off in tests
    viewer = _person(client, admin, "contractviewer", role="viewer")
    assert client.get("/api/doi", headers=viewer["read"], params={"q": "10.1021/jo971176v"}).status_code == 403
    # a read-only *account* is refused; a scientist's read token isn't (until 0.31 docs/API.md said it was)
    assert client.get("/api/doi", headers=me["read"], params={"q": "10.1021/jo971176v"}).status_code == 503
    doi.set_transport(httpx.MockTransport(_doi_org))
    try:
        r = client.get("/api/doi", headers=me["write"], params={"q": "https://doi.org/10.1021/jo971176v"})
        assert r.status_code == 200, r.text
        item = shaped(r.json(), REFERENCE, "DOI item")
        assert item["doi"] == "10.1021/jo971176v" and all(isinstance(a, str) for a in item["authors"])
        assert client.get("/api/doi", headers=me["write"], params={"q": "10.1234/nope"}).status_code == 404
        assert client.get("/api/doi", headers=me["write"], params={"q": "10.1234/junk"}).status_code == 502
    finally:
        doi.set_transport(None)


# ------------------------------------------------------------------ inventory


def test_materials(client, me):
    w, r = me["write"], me["read"]
    # structures no other test registers: the registry is shared by the whole test run, and a structure goes in once
    made = client.post("/api/materials", headers=w, json={"name": "Contract diethyloctylamine", "cas": "4088-37-9",
                                                          "smiles": "CCCCCCCCN(CC)CC", "density": 0.78})
    assert made.status_code == 201, made.text
    m = shaped(made.json(), MATERIAL, "POST /api/materials")
    assert m["mid"] == f"MAT-{m['id']:05d}"
    dup = client.post("/api/materials", headers=w, json={"name": "same again", "smiles": m["smiles"]})
    assert dup.status_code == 409 and m["mid"] in dup.json()["detail"]
    assert client.post("/api/materials", headers=w, json={"name": "Bad", "smiles": "C1CC"}).status_code == 422
    assert client.post("/api/materials", headers=w, json={"cas": "64-17-5"}).status_code == 422  # name is needed
    assert client.post("/api/materials", headers=w, json={"name": "Contract name only"}).status_code == 201

    rows = client.get("/api/materials", headers=r, params={"q": m["cas"]})
    assert m["id"] in [x["id"] for x in all_shaped(rows.json(), MATERIAL, "GET /api/materials")] and total(rows) >= 1
    by_kind = client.get("/api/materials", headers=r, params={"kind": "reagent", "limit": 1000}).json()
    assert by_kind and all(x["kind"] == "reagent" for x in by_kind)
    for q in (m["name"][3:12], m["formula"]):  # part of the name; the formula
        assert m["id"] in [x["id"] for x in client.get("/api/materials", headers=r, params={"q": q, "limit": 1000}).json()]
    p = client.patch(f"/api/materials/{m['id']}", headers=w, json={"synonyms": "contract base", "notes": "keep dry"})
    assert p.status_code == 200 and p.json()["synonyms"] == "contract base"
    assert m["id"] in [x["id"] for x in client.get("/api/materials", headers=r, params={"q": "contract base"}).json()]
    assert client.patch(f"/api/materials/{m['id']}", headers=w, json={"smiles": "C1CC"}).status_code == 422
    assert client.patch("/api/materials/99999999", headers=w, json={"notes": "x"}).status_code == 404
    one = client.get(f"/api/materials/{m['id']}", headers=r)
    shaped(one.json(), {**MATERIAL, "lots": list, "used_in": list}, "GET /api/materials/{id}")
    assert client.get("/api/materials/99999999", headers=r).status_code == 404


def test_lots_usage_and_reversal(client, me, nb, other):
    w, r = me["write"], me["read"]
    m = client.post("/api/materials", headers=w, json={"name": "Contract hexanes", "kind": "solvent", "density": 0.66}).json()
    solid = client.post("/api/materials", headers=w, json={"name": "Contract solid, no density"}).json()

    made = client.post(f"/api/materials/{m['id']}/lots", headers=w, json={
        "barcode": "CT-0001", "amount": 100, "unit": "mL", "location": "Contract cabinet B2", "supplier": "Sigma-Aldrich",
        "catalog_number": "296090", "purity": 98.5, "expires_at": "2030-01-01T00:00:00"})
    assert made.status_code == 201, made.text
    lot = shaped(made.json(), LOT, "POST /api/materials/{id}/lots")
    assert lot["status"] == "available" and lot["expires_at"] == "2030-01-01T00:00:00Z"
    auto = client.post(f"/api/materials/{solid['id']}/lots", headers=w, json={"amount": 5, "unit": "g"}).json()
    assert auto["barcode"] == f"{solid['mid']}-L{auto['id']:04d}"
    assert client.post(f"/api/materials/{m['id']}/lots", headers=w, json={"amount": 5, "unit": "pints"}).status_code == 400
    assert client.post(f"/api/materials/{m['id']}/lots", headers=w, json={"amount": 0, "unit": "g"}).status_code == 400
    assert client.post("/api/materials/99999999/lots", headers=w, json={"amount": 5, "unit": "g"}).status_code == 404
    assert client.get(f"/api/materials/{m['id']}", headers=r).json()["lots"][0]["barcode"] == "CT-0001"

    listed = client.get("/api/lots", headers=r, params={"q": "CT-000"})
    shaped(next(x for x in all_shaped(listed.json(), LOT_LISTED, "GET /api/lots") if x["id"] == lot["id"]), LOT_LISTED)
    assert total(listed) == len(listed.json())
    for params in ({"q": "Contract hexanes"}, {"location": "contract cabinet"}, {"status": "available", "q": "CT-0001"}):
        assert lot["id"] in [x["id"] for x in client.get("/api/lots", headers=r, params=params).json()], params
    assert lot["id"] not in [x["id"] for x in client.get("/api/lots", headers=r, params={"status": "empty", "q": "CT-0001"}).json()]

    p = client.patch(f"/api/lots/{lot['id']}", headers=w, json={"location": "Contract cabinet B3"})
    assert p.status_code == 200 and shaped(p.json(), LOT)["location"] == "Contract cabinet B3"
    assert client.patch("/api/lots/99999999", headers=w, json={"location": "x"}).status_code == 404

    # usage: 1.32 g of hexanes is 2 mL at 0.66 g/mL
    exp = new_exp(client, me, nb, "Contract usage")
    used = client.post(f"/api/lots/{lot['id']}/transactions", headers=w,
                       json={"amount": 1.32, "unit": "g", "experiment_id": exp["id"], "note": "column"})
    assert used.status_code == 201, used.text
    shaped(used.json(), {"lot": dict, "transaction_id": int}, "usage")
    assert shaped(used.json()["lot"], LOT)["amount"] == 98
    back = client.post(f"/api/lots/{lot['id']}/transactions", headers=w, json={"amount": 1, "unit": "mL", "direction": "in"})
    assert back.status_code == 201 and back.json()["lot"]["amount"] == 99
    bad = [({"amount": 1, "unit": "pints"}, 400), ({"amount": 1, "unit": "mL", "direction": "sideways"}, 400),
           ({"amount": -1, "unit": "mL"}, 400), ({"amount": 1, "unit": "mL", "experiment_id": 99999999}, 404),
           ({"amount": 1, "unit": "mL", "experiment_id": other["experiment"]["id"]}, 404), ({"amount": 500, "unit": "mL"}, 409)]
    for body, code in bad:
        res = client.post(f"/api/lots/{lot['id']}/transactions", headers=w, json=body)
        assert res.status_code == code, (body, res.status_code, res.text)
    assert "left in CT-0001" in client.post(f"/api/lots/{lot['id']}/transactions", headers=w,
                                            json={"amount": 500, "unit": "mL"}).json()["detail"]
    assert client.post(f"/api/lots/{auto['id']}/transactions", headers=w, json={"amount": 1, "unit": "mL"}).status_code == 400
    assert client.post("/api/lots/99999999/transactions", headers=w, json={"amount": 1, "unit": "g"}).status_code == 404

    ledger = client.get(f"/api/lots/{lot['id']}/transactions", headers=r)
    rows = all_shaped(ledger.json(), LEDGER, "ledger")
    assert total(ledger) == len(rows) == 3 and rows[0]["id"] > rows[-1]["id"]  # newest first
    usage = next(t for t in rows if t["id"] == used.json()["transaction_id"])
    assert usage["delta"] == -2 and usage["experiment"] == {"id": exp["id"], "eid": exp["eid"]} and usage["note"] == "column"
    shaped(usage["user"], USER, "ledger user")
    assert client.get("/api/lots/99999999/transactions", headers=r).status_code == 404

    rev = client.post(f"/api/lots/transactions/{used.json()['transaction_id']}/reverse", headers=w)
    assert rev.status_code == 201 and shaped(rev.json()["lot"], LOT)["amount"] == 101
    assert client.post(f"/api/lots/transactions/{used.json()['transaction_id']}/reverse", headers=w).status_code == 409
    assert client.post(f"/api/lots/transactions/{rev.json()['transaction_id']}/reverse", headers=w).status_code == 409
    assert client.post("/api/lots/transactions/99999999/reverse", headers=w).status_code == 404
    # not enough left to undo a top-up: 101 mL, use 100.5, then undo the 1 mL put back
    client.post(f"/api/lots/{lot['id']}/transactions", headers=w, json={"amount": 100.5, "unit": "mL"})
    assert client.post(f"/api/lots/transactions/{back.json()['transaction_id']}/reverse", headers=w).status_code == 409
    # used up: empty
    last = client.post(f"/api/lots/{lot['id']}/transactions", headers=w, json={"amount": 0.5, "unit": "mL"}).json()["lot"]
    assert last["amount"] == 0 and last["status"] == "empty"


def test_register_product(client, me, nb, other):
    w = me["write"]
    exp = new_exp(client, me, nb, "Contract product")
    r = client.post("/api/materials/register-product", headers=w, json={
        "experiment_id": exp["id"], "name": "Contract KX-107", "smiles": "O=C(c1ccccc1)N1CCOCC1", "amount_mg": 182,
        "purity": 98, "location": "Freezer 2"})
    assert r.status_code == 201, r.text
    mat, lot = shaped(r.json()["material"], MATERIAL), shaped(r.json()["lot"], LOT)
    assert mat["kind"] == "product" and lot["amount"] == 182 and lot["unit"] == "mg" and lot["barcode"].startswith(mat["mid"])
    again = client.post("/api/materials/register-product", headers=w, json={
        "experiment_id": exp["id"], "name": "renamed", "smiles": "C1COCCN1C(=O)c1ccccc1"})
    assert again.status_code == 201 and again.json()["material"]["id"] == mat["id"] and again.json()["lot"] is None
    bad = [({"experiment_id": 99999999, "name": "x"}, 404), ({"experiment_id": other["experiment"]["id"], "name": "x"}, 404),
           ({"experiment_id": exp["id"], "name": "x", "smiles": "C1CC"}, 422)]
    for body, code in bad:
        assert client.post("/api/materials/register-product", headers=w, json=body).status_code == code, body


# ------------------------------------------------------------------ compound registry, sample codes, search


def test_compounds(client, me, nb):
    client.post("/api/materials/register-product", headers=me["write"], json={
        "experiment_id": new_exp(client, me, nb, "Contract compound")["id"], "name": "Contract trimethoxybenzene", "smiles": "COc1ccc(OC)c(OC)c1"})
    r = client.get("/api/compounds", headers=me["read"])
    rows = all_shaped(r.json(), COMPOUND, "GET /api/compounds")
    assert total(r) == len(rows)
    row = next(c for c in rows if c["smiles"] == "COc1ccc(OC)c(OC)c1")
    shaped(row["material"], {"id": int, "mid": str}, "compound material")
    assert all(isinstance(v, str) for c in rows for v in c["lipinski_violations"] + c["veber_violations"])
    for c in rows:
        for e in c["experiments"]:
            shaped(e, COMPOUND_EXPERIMENT, "compound experiment")


def test_sample_code_lookup(client, me, other):
    assert client.put("/api/auth/me/sample-prefix", headers=me["session"], json={"prefix": "CTR"}).status_code == 200
    private = client.post("/api/notebooks", headers=me["session"], json={"name": "Contract samples", "visibility": "private"}).json()
    exp = new_exp(client, me, private, "Contract sample")
    s = client.post(f"/api/experiments/{exp['id']}/samples", headers=me["write"], json={})
    assert s.status_code == 201, s.text
    code = s.json()["code"]
    assert code == "CTR-001"
    for typed in ("CTR-001", "ctr-1", "CTR_001", "CTR 001", "CTR001"):
        r = client.get("/api/samples/lookup", headers=me["read"], params={"code": typed})
        assert r.status_code == 200, (typed, r.text)
        assert r.json()["code"] == "CTR-001"
        assert shaped(r.json()["experiment"], EXPERIMENT, "sample lookup")["id"] == exp["id"]
    assert client.get("/api/samples/lookup", headers=me["read"], params={"code": "CTR-999"}).status_code == 404  # never given out
    assert client.get("/api/samples/lookup", headers=other["read"], params={"code": "CTR-001"}).status_code == 404  # can't open it
    assert client.delete(f"/api/samples/{s.json()['id']}", headers=me["write"]).status_code == 200
    assert client.get("/api/samples/lookup", headers=me["read"], params={"code": "CTR-001"}).status_code == 404  # taken off


def test_search(client, me, nb, other):
    exp = new_exp(client, me, nb, "Contractsearch Suzuki coupling")
    client.post(f"/api/experiments/{exp['id']}/attachments", headers=me["write"],
                files={"file": ("contractsearch.csv", io.BytesIO(b"x"), "text/csv")})
    client.post("/api/materials", headers=me["write"], json={"name": "Contractsearch boronic acid", "smiles": "OB(O)c1ccc(C)cc1"})
    r = client.get("/api/search", headers=me["read"], params={"q": "contractsearch"})
    body = r.json()
    assert set(body) == {"experiments", "materials", "files", "terms"}
    hit = shaped(all_shaped(body["experiments"], SEARCH_EXPERIMENT, "search experiments")[0], {"snippet": str})
    assert hit["id"] == exp["id"]
    all_shaped(body["materials"], SEARCH_MATERIAL, "search materials")
    all_shaped(body["files"], SEARCH_FILE, "search files")
    assert body["terms"] == ["contractsearch"]
    assert r.headers["X-Total-Experiments"] == "1" and r.headers["X-Total-Materials"] == "1" and r.headers["X-Total-Files"] == "1"
    assert total(r) == 1
    phrase = client.get("/api/search", headers=me["read"], params={"q": '"Suzuki coupling" contractsearch'}).json()
    assert phrase["terms"] == ["suzuki coupling", "contractsearch"] and phrase["experiments"][0]["id"] == exp["id"]
    assert client.get("/api/search", headers=me["read"], params={"q": "contractsearch", "status": "signed"}).json()["experiments"] == []
    assert client.get("/api/search", headers=me["read"], params={"q": "contractsearch", "notebook_id": nb["id"]}).json()["experiments"]
    assert client.get("/api/search", headers=other["read"], params={"q": "contractsearch", "mine": "true"}).json()["experiments"] == []
    assert client.get("/api/search", headers=other["read"], params={"q": "Not yours"}).json()["experiments"]
    assert not any(e["id"] == other["experiment"]["id"]
                   for e in client.get("/api/search", headers=me["read"], params={"q": "Not yours"}).json()["experiments"])
    empty = client.get("/api/search", headers=me["read"], params={"q": ""}).json()
    assert empty == {"experiments": [], "materials": [], "files": [], "terms": []}

    sub = client.get("/api/search", headers=me["read"], params={"q": "OB(O)c1ccccc1", "mode": "substructure"})
    assert sub.status_code == 200
    all_shaped(sub.json()["materials"], SEARCH_MATERIAL, "substructure materials")
    assert any(m["name"] == "Contractsearch boronic acid" for m in sub.json()["materials"])
    sim = client.get("/api/search", headers=me["read"], params={"q": "OB(O)c1ccc(C)cc1", "mode": "similarity", "threshold": 0.6})
    assert sim.status_code == 200 and all(m["score"] >= 0.6 for m in sim.json()["materials"])
    assert client.get("/api/search", headers=me["read"], params={"q": "x", "mode": "sideways"}).status_code == 400
    assert client.get("/api/search", headers=me["read"], params={"q": "C1CC((", "mode": "substructure"}).status_code == 422


# ------------------------------------------------------------------ the examples in docs/API.md, run as written


def _examples() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    part = text[text.index("## Paging"):text.index("## Webhooks")]
    return re.findall(r"```python\n(.*?)```", part, re.S)


def test_the_documented_examples_run(client, me, tmp_path, monkeypatch):
    m = client.post("/api/materials", headers=me["write"], json={"name": "Contract example acid", "cas": "50-00-0"}).json()
    client.post(f"/api/materials/{m['id']}/lots", headers=me["write"], json={"barcode": "SA-0042", "amount": 100, "unit": "g"})
    (tmp_path / "bottles.csv").write_text(
        "name,cas,barcode,amount,unit,location,supplier\n"
        "Contract example acid,50-00-0,SA-0042,100,g,B2,Sigma-Aldrich\n"  # registered already: skipped
        "Contract example acid,50-00-0,SA-0043,25,g,B2,Sigma-Aldrich\n"  # a second bottle of a material already there
        "Contract example ester,141-78-6,SA-0044,2.5,L,Solvent cabinet,Fisher\n"  # a new material
        "Contract example mixture,,SA-0045,10,mL,Fridge,\n", encoding="utf-8")  # no CAS: found by name
    monkeypatch.chdir(tmp_path)
    blocks = _examples()
    assert len(blocks) >= 6
    with TestClient(app, headers=me["write"]) as bt:
        ns = {"BT": bt}
        for code in blocks:
            exec(compile(code, str(DOC), "exec"), ns)  # noqa: S102
        # and the CSV loader did what it says
        lots = {x["barcode"]: x for x in bt.get("/api/lots", params={"q": "SA-004"}).json()}
        assert set(lots) >= {"SA-0042", "SA-0043", "SA-0044", "SA-0045"}
        assert lots["SA-0043"]["material_id"] == m["id"] and lots["SA-0042"]["amount"] == 100 - 1.25
        assert len([x for x in lots.values() if x["barcode"] == "SA-0042"]) == 1
        ns["__name__"] = "again"
        exec(compile(blocks[[i for i, b in enumerate(blocks) if "bottles.csv" in b][0]], str(DOC), "exec"), ns)  # noqa: S102
        assert len(bt.get("/api/lots", params={"q": "SA-004"}).json()) == len(lots)  # a second run adds nothing


# ------------------------------------------------------------------ every documented endpoint is checked here

COVERED = {
    "GET /api/experiments", "GET /api/inbox", "GET /api/inbox/{id}/files/{n}", "POST /api/inbox/{id}/assign",
    "POST /api/inbox/{id}/follow-up", "POST /api/inbox/{id}/ignore", "POST /api/experiments/{id}/follow-up",
    "POST /api/attachments/{id}/not-mine", "GET /api/experiments/{id}/export.eln", "GET /api/notebooks/{id}/export.eln",
    "GET /api/export.eln", "POST /api/eln/preview", "POST /api/eln/import/{token}", "DELETE /api/eln/import/{token}",
    "GET /api/doi", "GET /api/materials", "GET /api/materials/{id}", "POST /api/materials", "PATCH /api/materials/{id}",
    "POST /api/materials/{id}/lots", "GET /api/lots", "PATCH /api/lots/{id}", "POST /api/lots/{id}/transactions",
    "GET /api/lots/{id}/transactions", "POST /api/lots/transactions/{id}/reverse", "POST /api/materials/register-product",
    "GET /api/compounds", "GET /api/samples/lookup", "GET /api/search",
}


def test_every_endpoint_in_the_docs_is_checked_here():
    found = {f"{m} {p}" for m, p in re.findall(r"`(GET|POST|PATCH|PUT|DELETE) (/api/[^`?\s]*)", DOC.read_text(encoding="utf-8"))}
    assert found - COVERED == set(), "documented in docs/API.md but not checked in this file"
    assert COVERED - found == set(), "checked here but no longer in docs/API.md"
