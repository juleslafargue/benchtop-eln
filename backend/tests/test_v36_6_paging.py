"""v0.36.6: long lists. A record's audit trail came all at once (thousands of events for a record edited for months),
and the lab's showed the latest 150 with no way back; pickers only saw the newest few hundred records. Now the audit
trails come in pages going back (before_id, with X-Total-Count), and /api/experiments finds records by number or
title (q), so ⌘K, the SI builder and screens find older ones."""
from __future__ import annotations


def _exp(client, h, nb, title):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"], "sections": "no_chemistry"})
    assert r.status_code == 201, r.text
    return r.json()


def test_a_records_audit_trail_comes_in_pages_going_back(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Edited many times")
    entry = client.post(f"/api/experiments/{exp['id']}/entries", headers=a, json={"kind": "text", "title": "Notes"}).json()
    for i in range(25):
        assert client.put(f"/api/entries/{entry['id']}", headers=a, json={"content": {"html": f"<p>v{i}</p>"}}).status_code == 200
    everything = client.get(f"/api/experiments/{exp['id']}/audit", headers=a)
    total = int(everything.headers["X-Total-Count"])
    assert total == len(everything.json()) >= 27  # without limit: all of it, as before
    seen, before = [], None
    while True:
        r = client.get(f"/api/experiments/{exp['id']}/audit?limit=10" + (f"&before_id={before}" if before else ""), headers=a)
        assert r.status_code == 200 and int(r.headers["X-Total-Count"]) == total
        page = r.json()
        if not page:
            break
        assert len(page) <= 10
        seen += page
        before = page[-1]["id"]
    assert [e["id"] for e in seen] == [e["id"] for e in everything.json()]  # newest first, none twice, none missed


def test_the_labs_audit_trail_says_how_many_and_goes_back(client, admin):
    first = client.get("/api/audit?limit=5", headers=admin)
    total = int(first.headers["X-Total-Count"])
    rows = first.json()
    assert total > 5 and len(rows) == 5
    older = client.get(f"/api/audit?limit=5&before_id={rows[-1]['id']}", headers=admin).json()
    assert older and all(e["id"] < rows[-1]["id"] for e in older)
    assert client.get("/api/audit?limit=0", headers=admin).status_code == 422


def test_records_are_found_by_number_or_title_however_old(client, users, notebook):
    a = users["alice"]
    old = _exp(client, a, notebook, "Grignard of 4-bromotoluene, first try")
    for i in range(5):
        _exp(client, a, notebook, f"Later work {i}")
    by_title = client.get("/api/experiments?q=bromotoluene&limit=1", headers=a)
    assert [e["id"] for e in by_title.json()] == [old["id"]] and by_title.headers["X-Total-Count"] == "1"
    for q in (old["eid"], old["eid"].lower(), old["eid"].replace("EXP-", ""), str(old["id"])):
        assert old["id"] in [e["id"] for e in client.get(f"/api/experiments?q={q}", headers=a).json()], q
    assert client.get("/api/experiments?q=100%25_none", headers=a).json() == []  # % and _ are taken literally
    # only records the person can open
    private = client.post("/api/notebooks", headers=a, json={"name": "Mine only", "visibility": "private"}).json()
    hidden = _exp(client, a, private, "Unpublished bromotoluene route")
    assert hidden["id"] not in [e["id"] for e in client.get("/api/experiments?q=bromotoluene", headers=users["bob"]).json()]
