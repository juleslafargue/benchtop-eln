"""0.31: list endpoints page with `limit` and `offset` and say how many rows match in X-Total-Count.

Until 0.31, GET /api/materials stopped at 500 without saying so, and had no way to fetch the rest: a script that
listed the registry to find what was already there quietly missed everything after the 500th name."""
import pytest

pytestmark = pytest.mark.usefixtures("safety_settled")  # 500-odd materials mustn't hold up later tests' safety refresh


def _person(client, admin, username):
    client.post("/api/users", headers=admin, json={"username": username, "password": "lab-bench-pw-2026", "must_change": False,
                                                   "full_name": username.title(), "role": "scientist"})
    r = client.post("/api/auth/login", json={"username": username, "password": "lab-bench-pw-2026"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    tokens = {}
    for scope in ("read", "write"):
        t = client.post("/api/auth/tokens", headers=h, json={"name": f"paging {scope}", "scope": scope})
        assert t.status_code == 201, t.text
        tokens[scope] = {"Authorization": f"Bearer {t.json()['token']}"}
    return tokens


def _fetch_all(client, path, headers, params, page=200):
    rows, offset = [], 0
    while True:
        r = client.get(path, headers=headers, params={**params, "limit": page, "offset": offset})
        assert r.status_code == 200, r.text
        total = r.headers.get("X-Total-Count")
        assert total is not None, "no X-Total-Count header"
        rows += r.json()
        offset += page
        if offset >= int(total):
            return rows, int(total)


def test_more_than_500_materials_can_all_be_fetched(client, admin):
    tok = _person(client, admin, "pager")
    names = [f"zz Paging test {i:04d}" for i in range(520)]  # "zz": after other tests' materials in name order
    for n in names:
        assert client.post("/api/materials", headers=tok["write"], json={"name": n, "kind": "other"}).status_code == 201

    first = client.get("/api/materials", headers=tok["read"], params={"q": "zz Paging test"})
    assert first.status_code == 200
    assert len(first.json()) == 500  # the default page is the same size as before, so the app behaves as it did
    assert first.headers.get("X-Total-Count") == "520", "the caller can't tell that 20 materials are missing"

    rows, total = _fetch_all(client, "/api/materials", tok["read"], {"q": "zz Paging test"})
    assert total == 520
    assert sorted(m["name"] for m in rows) == names  # every one, none twice

    everything, total = _fetch_all(client, "/api/materials", tok["read"], {}, page=1000)
    assert total >= 520 and len(everything) == total == len({m["id"] for m in everything})


LISTS = ["/api/materials", "/api/lots", "/api/experiments", "/api/compounds", "/api/search?q=x"]


def test_paging_parameters_are_checked(client, admin):
    tok = _person(client, admin, "pagecheck")
    m = client.post("/api/materials", headers=tok["write"], json={"name": "Paging check reagent"}).json()
    lot = client.post(f"/api/materials/{m['id']}/lots", headers=tok["write"], json={"amount": 5, "unit": "g"}).json()
    for path in LISTS + [f"/api/lots/{lot['id']}/transactions"]:
        sep = "&" if "?" in path else "?"
        for bad in ("limit=0", "limit=1001", "limit=-1", "limit=lots", "offset=-1", "offset=x"):
            r = client.get(f"{path}{sep}{bad}", headers=tok["read"])
            assert r.status_code == 422, (path, bad, r.status_code)
            name = bad.split("=")[0]
            assert any(d["loc"] == ["query", name] for d in r.json()["detail"]), r.json()  # says which parameter
        ok = client.get(f"{path}{sep}limit=1000&offset=0", headers=tok["read"])
        assert ok.status_code == 200 and "X-Total-Count" in ok.headers, path


def test_lots_ledger_experiments_compounds_and_search_page(client, admin):
    tok = _person(client, admin, "pagelots")
    w, r = tok["write"], tok["read"]
    mats = [client.post("/api/materials", headers=w, json={"name": f"zz Lot paging {i}"}).json() for i in range(3)]
    for m in mats:
        for _ in range(2):
            assert client.post(f"/api/materials/{m['id']}/lots", headers=w, json={"amount": 10, "unit": "g"}).status_code == 201
    rows, total = _fetch_all(client, "/api/lots", r, {"q": "zz Lot paging"}, page=4)
    assert total == 6 and len({x["id"] for x in rows}) == 6
    assert len(client.get("/api/lots", headers=r, params={"q": "zz Lot paging"}).json()) == 6  # no limit given: all of them

    lot = rows[0]["id"]
    for _ in range(4):
        client.post(f"/api/lots/{lot}/transactions", headers=w, json={"amount": 1, "unit": "g"})
    page = client.get(f"/api/lots/{lot}/transactions", headers=r, params={"limit": 2, "offset": 1})
    assert page.headers["X-Total-Count"] == "5" and len(page.json()) == 2  # received + 4 withdrawals
    whole = client.get(f"/api/lots/{lot}/transactions", headers=r).json()
    assert [t["id"] for t in page.json()] == [t["id"] for t in whole[1:3]]

    nb = client.post("/api/notebooks", headers=w, json={"name": "Paging NB"}).json()
    for i in range(5):
        client.post("/api/experiments", headers=w, json={"title": f"Pagingword run {i}", "notebook_id": nb["id"]})
    exps, total = _fetch_all(client, "/api/experiments", r, {"notebook_id": nb["id"]}, page=2)
    assert total == 5 and len({e["id"] for e in exps}) == 5

    s = client.get("/api/search", headers=r, params={"q": "Pagingword", "limit": 2, "offset": 4})
    assert s.headers["X-Total-Experiments"] == "5" and s.headers["X-Total-Count"] == "5"
    assert s.headers["X-Total-Materials"] == "0" and s.headers["X-Total-Files"] == "0"
    assert len(s.json()["experiments"]) == 1 and set(s.json()) == {"experiments", "materials", "files", "terms"}
    empty = client.get("/api/search", headers=r, params={"q": ""})
    assert empty.headers["X-Total-Count"] == "0"

    comp = client.get("/api/compounds", headers=r)
    allc = comp.json()
    assert comp.headers["X-Total-Count"] == str(len(allc))
    one = client.get("/api/compounds", headers=r, params={"limit": 1, "offset": 0})
    assert one.json() == allc[:1] and one.headers["X-Total-Count"] == str(len(allc))


def test_the_inbox_says_how_many_are_waiting_past_200(client, admin, tmp_path):
    """Until 0.31 the Inbox list stopped at 200 without saying so: the Waiting tab said (200) with 230 waiting."""
    from datetime import datetime, timedelta

    from app import instruments
    tok = _person(client, admin, "pageinbox")
    folder = tmp_path / "plate-reader"
    folder.mkdir()
    inst = client.post("/api/instruments", headers=admin, json={"name": "Paging reader", "kind": "files", "folder": str(folder)})
    assert inst.status_code == 201, inst.text
    t0 = datetime(2001, 1, 1)  # older than other tests' arrivals, so their Inbox lists aren't pushed down
    try:
        instruments.scan_all(now=t0)
        for i in range(230):
            (folder / f"paging plate {i:03d}.csv").write_text("well,abs\n")
        instruments.scan_all(now=t0 + timedelta(seconds=10))
        instruments.scan_all(now=t0 + timedelta(seconds=200))
        mine = lambda rows: [x for x in rows if x["path"].startswith("paging plate")]  # noqa: E731
        first = client.get("/api/inbox", headers=tok["read"])
        total = int(first.headers.get("X-Total-Count", "-1"))
        assert len(first.json()) == 200 and total >= 230, "the caller can't tell more are waiting"
        rest = client.get("/api/inbox", headers=tok["read"], params={"offset": 200, "limit": 500})
        assert int(rest.headers["X-Total-Count"]) == total and len(first.json()) + len(rest.json()) == total
        assert len(mine(first.json()) + mine(rest.json())) == 230
        for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
            assert client.get("/api/inbox", headers=tok["read"], params=bad).status_code == 422, bad
    finally:  # set aside, so later tests' Inbox is as it was
        for x in mine(client.get("/api/inbox", headers=admin, params={"limit": 500}).json()):
            client.post(f"/api/inbox/{x['id']}/ignore", headers=admin)
        client.delete(f"/api/instruments/{inst.json()['id']}", headers=admin)
