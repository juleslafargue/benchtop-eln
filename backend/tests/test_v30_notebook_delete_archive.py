"""0.30: deleting an empty notebook, and archiving one that holds experiments.

Records are never deleted, so a notebook can be deleted only while no experiment of any kind is in it (drafts, signed,
witnessed, voided, locked, templates); only its owner or an admin can do it, never a read-only account, and the audit
trail keeps its history (the hash chain still verifies). Anything that pointed at it (a person's default notebook) is
cleared. A notebook with experiments can be archived: it leaves the sidebar, nothing new goes into it (the server
refuses, not only the app), and its experiments stay readable, searchable and editable as before."""
import pytest
from sqlmodel import Session

PW = "lab-bench-pw-2026"


def _account(client, admin, username, role="scientist"):
    r = client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                       "full_name": username.title(), "role": role})
    assert r.status_code == 201, r.text
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": username, "password": PW}).json()["token"]}
    return h, r.json()["id"]


@pytest.fixture(scope="module")
def lab(client, admin):
    return {name: _account(client, admin, name, "viewer" if name == "nd_viewer" else "scientist")
            for name in ("nd_owner", "nd_other", "nd_viewer")}


def _nb(client, h, name, **kw):
    r = client.post("/api/notebooks", headers=h, json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _exp(client, h, nb_id, title="An experiment", **kw):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb_id, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _listed(client, h, archived=False):
    return {n["id"]: n for n in client.get("/api/notebooks", headers=h, params={"include_archived": archived}).json()}


def _chain_ok(client, admin):
    v = client.get("/api/audit/verify", headers=admin).json()
    assert v["ok"], v


# ------------------------------------------------------------------ deleting

def test_owner_deletes_an_empty_notebook(client, admin, lab):
    owner, _ = lab["nd_owner"]
    nb = _nb(client, owner, "Test notebook (delete me)", visibility="private")
    assert nb["empty"] is True and _listed(client, owner)[nb["id"]]["empty"] is True
    r = client.delete(f"/api/notebooks/{nb['id']}", headers=owner)
    assert r.status_code == 200, r.text
    assert nb["id"] not in _listed(client, owner, archived=True)
    assert client.delete(f"/api/notebooks/{nb['id']}", headers=owner).status_code == 404
    # the audit trail keeps it: what it was called, its audience; and the chain still verifies
    evs = client.get("/api/audit", headers=admin, params={"limit": 50, "object_type": "notebook"}).json()
    ev = next(e for e in evs if e["action"] == "delete" and e["object_type"] == "notebook" and e["object_id"] == nb["id"])
    assert ev["details"]["name"] == "Test notebook (delete me)" and ev["details"]["visibility"] == "private"
    assert "group_id" in ev["details"]
    _chain_ok(client, admin)


def test_admin_deletes_someone_elses_empty_notebook(client, admin, lab):
    owner, _ = lab["nd_owner"]
    nb = _nb(client, owner, "Made by mistake")
    assert client.delete(f"/api/notebooks/{nb['id']}", headers=admin).status_code == 200
    assert nb["id"] not in _listed(client, owner, archived=True)
    _chain_ok(client, admin)


def test_unknown_notebook_is_404(client, lab):
    assert client.delete("/api/notebooks/987654", headers=lab["nd_owner"][0]).status_code == 404


def test_another_scientist_and_a_viewer_cannot_delete(client, lab):
    owner, other, viewer = lab["nd_owner"][0], lab["nd_other"][0], lab["nd_viewer"][0]
    nb = _nb(client, owner, "Open to everyone, owned by nd_owner")
    r = client.delete(f"/api/notebooks/{nb['id']}", headers=other)
    assert r.status_code == 403, r.text
    assert client.delete(f"/api/notebooks/{nb['id']}", headers=viewer).status_code == 403
    # a private notebook someone can't open isn't confirmed to exist
    hidden = _nb(client, owner, "Private one", visibility="private")
    assert client.delete(f"/api/notebooks/{hidden['id']}", headers=other).status_code == 404
    assert {nb["id"], hidden["id"]} <= set(_listed(client, owner))


def _signed(client, h, nb_id, title):
    e = _exp(client, h, nb_id, title)
    assert client.post(f"/api/experiments/{e['id']}/sign", headers=h, json={"password": PW}).status_code == 200
    return e


@pytest.mark.parametrize("kind", ["open", "signed", "witnessed", "voided", "locked", "template"])
def test_a_notebook_with_any_experiment_cannot_be_deleted(client, admin, lab, kind):
    owner, _ = lab["nd_owner"]
    other, _ = lab["nd_other"]
    nb = _nb(client, owner, f"Holds a {kind} experiment")
    if kind == "template":
        e = _exp(client, owner, nb["id"], "A template kept here", is_template=True)
    elif kind == "open":
        e = _exp(client, owner, nb["id"])
    else:
        e = _signed(client, owner, nb["id"], f"{kind} record")
        if kind == "witnessed":
            assert client.post(f"/api/experiments/{e['id']}/witness", headers=other, json={"password": PW}).status_code == 200
        elif kind == "voided":
            assert client.post(f"/api/experiments/{e['id']}/void", headers=owner,
                               json={"password": PW, "comment": "Wrong batch"}).status_code == 200
        elif kind == "locked":  # as an import of a record signed in another ELN arrives
            from app.db import engine
            from app.models import Experiment, ExperimentStatus
            with Session(engine) as s:
                x = s.get(Experiment, e["id"])
                x.status = ExperimentStatus.locked
                s.add(x)
                s.commit()
    expected = {"locked": "locked", "template": "open"}.get(kind, kind)
    assert client.get(f"/api/experiments/{e['id']}", headers=owner).json()["status"] == expected
    assert _listed(client, owner)[nb["id"]]["empty"] is False
    for h in (owner, admin):
        r = client.delete(f"/api/notebooks/{nb['id']}", headers=h)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "This notebook has experiments, so it can't be deleted. Archive it instead."
    assert client.get(f"/api/experiments/{e['id']}", headers=owner).status_code == 200
    assert nb["id"] in _listed(client, owner)


def test_experiments_moved_out_dont_count(client, admin, lab):
    owner, _ = lab["nd_owner"]
    old, new = _nb(client, owner, "Started here"), _nb(client, owner, "Moved to here")
    e = _exp(client, owner, old["id"])
    assert client.patch(f"/api/experiments/{e['id']}", headers=owner, json={"notebook_id": new["id"]}).status_code == 200
    assert _listed(client, owner)[old["id"]]["empty"] is True
    assert client.delete(f"/api/notebooks/{old['id']}", headers=owner).status_code == 200
    assert client.get(f"/api/experiments/{e['id']}", headers=owner).json()["notebook_id"] == new["id"]
    assert client.delete(f"/api/notebooks/{new['id']}", headers=owner).status_code == 409
    _chain_ok(client, admin)


def test_deleting_clears_peoples_default_notebook(client, admin, lab):
    owner, _ = lab["nd_owner"]
    other, _ = lab["nd_other"]
    nb, keep = _nb(client, owner, "Someone's default"), _nb(client, owner, "Still here")
    for h in (owner, other):
        assert client.patch("/api/auth/me", headers=h, json={"preferences": {"default_notebook_id": nb["id"], "theme": "dark"}}).status_code == 200
    assert client.delete(f"/api/notebooks/{nb['id']}", headers=owner).status_code == 200
    for h in (owner, other):
        prefs = client.get("/api/auth/me", headers=h).json()["preferences"]
        assert prefs.get("default_notebook_id") is None, prefs
        assert prefs["theme"] == "dark", "other preferences were lost"
    # a default pointing at another notebook is left alone
    assert client.patch("/api/auth/me", headers=other, json={"preferences": {"default_notebook_id": keep["id"]}}).status_code == 200
    gone = _nb(client, owner, "Another one")
    assert client.delete(f"/api/notebooks/{gone['id']}", headers=owner).status_code == 200
    assert client.get("/api/auth/me", headers=other).json()["preferences"]["default_notebook_id"] == keep["id"]
    _chain_ok(client, admin)


def test_a_deleted_groups_notebook_and_its_group(client, admin, lab):
    """A group notebook goes without touching the group; the group's count follows."""
    owner, owner_id = lab["nd_owner"]
    g = client.post("/api/groups", headers=admin, json={"name": "Delete-test group"}).json()
    assert client.put(f"/api/groups/{g['id']}/members/{owner_id}", headers=admin, json={"role": "member"}).status_code == 200
    nb = _nb(client, owner, "Group notebook", group_id=g["id"], visibility="group")
    count = lambda: next(x for x in client.get("/api/groups", headers=admin).json() if x["id"] == g["id"])["notebook_count"]  # noqa: E731
    assert count() == 1
    assert client.delete(f"/api/notebooks/{nb['id']}", headers=owner).status_code == 200
    assert count() == 0


# ------------------------------------------------------------------ archiving

def test_archive_and_unarchive(client, admin, lab):
    owner, other = lab["nd_owner"][0], lab["nd_other"][0]
    nb = _nb(client, owner, "Finished project")
    draft = _exp(client, owner, nb["id"], "Draft still open")
    signed = _signed(client, owner, nb["id"], "Signed result in the finished project")
    hash_before = client.get(f"/api/experiments/{signed['id']}/verify", headers=owner).json()
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=other, json={"archived": True}).status_code == 403
    r = client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True
    # out of the main list, still in the full one
    assert nb["id"] not in _listed(client, owner) and _listed(client, owner, archived=True)[nb["id"]]["archived"]
    # its records: readable, signatures intact, the draft still editable and signable
    d = client.get(f"/api/experiments/{signed['id']}", headers=owner).json()
    assert d["status"] == "signed" and d["notebook_archived"] is True
    assert client.get(f"/api/experiments/{signed['id']}/verify", headers=owner).json() == hash_before
    full = client.get(f"/api/experiments/{draft['id']}", headers=owner).json()
    assert full["can_edit"] and full["status"] == "open"
    sec = full["entries"][0]
    r = client.put(f"/api/entries/{sec['id']}", headers=owner, json={"content": {"html": "<p>Still writing</p>"}, "expected_version": sec["version"]})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/experiments/{draft['id']}/sign", headers=owner, json={"password": PW}).status_code == 200
    # unarchive: back in the list, open to new experiments
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=admin, json={"archived": False}).json()["archived"] is False
    assert nb["id"] in _listed(client, owner)
    _exp(client, owner, nb["id"], "New after unarchiving")
    _chain_ok(client, admin)


def test_nothing_new_goes_into_an_archived_notebook(client, admin, lab):
    owner, _ = lab["nd_owner"]
    nb, other_nb = _nb(client, owner, "Archived, closed to new work"), _nb(client, owner, "Open notebook")
    signed = _signed(client, owner, nb["id"], "Signed before archiving")
    elsewhere = _exp(client, owner, other_nb["id"], "Lives elsewhere")
    eln = client.get(f"/api/experiments/{signed['id']}/export.eln", headers=owner).content
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True}).status_code == 200
    msg = "“Archived, closed to new work” is archived, so no new experiments can go into it."

    r = client.post("/api/experiments", headers=owner, json={"title": "New", "notebook_id": nb["id"]})
    assert r.status_code == 409 and r.json()["detail"].startswith(msg), r.text
    r = client.post("/api/experiments", headers=admin, json={"title": "Admin too", "notebook_id": nb["id"]})
    assert r.status_code == 409
    r = client.post("/api/experiments", headers=owner, json={"title": "Template", "notebook_id": nb["id"], "is_template": True})
    assert r.status_code == 409
    # moved in from another notebook
    r = client.patch(f"/api/experiments/{elsewhere['id']}", headers=owner, json={"notebook_id": nb["id"]})
    assert r.status_code == 409 and client.get(f"/api/experiments/{elsewhere['id']}", headers=owner).json()["notebook_id"] == other_nb["id"]
    # a copy (Duplicate, Repeat at another scale) would land in the same notebook; a template copy goes nowhere, so it's fine
    assert client.post(f"/api/experiments/{signed['id']}/clone", headers=owner).status_code == 409
    assert client.post(f"/api/experiments/{signed['id']}/clone", headers=owner, params={"scale": 2}).status_code == 409
    assert client.post(f"/api/experiments/{signed['id']}/clone", headers=owner, params={"as_template": True}).status_code == 201
    # a follow-up chooses its notebook: not the archived one
    r = client.post(f"/api/experiments/{signed['id']}/follow-up", headers=owner, json={"notebook_id": nb["id"]})
    assert r.status_code == 409 and r.json()["detail"].startswith(msg)
    assert client.post(f"/api/experiments/{signed['id']}/follow-up", headers=owner, json={}).status_code == 400
    assert client.post(f"/api/experiments/{signed['id']}/follow-up", headers=owner, json={"notebook_id": other_nb["id"]}).status_code == 201
    # a .eln import
    p = client.post("/api/eln/preview", headers=owner, files={"file": ("x.eln", eln, "application/zip")}).json()
    r = client.post(f"/api/eln/import/{p['token']}", headers=owner, json={"notebook_id": nb["id"], "items": [i["key"] for i in p["items"]]})
    assert r.status_code == 409 and r.json()["detail"].startswith(msg), r.text
    # nothing arrived
    assert [e["id"] for e in client.get("/api/experiments", headers=owner, params={"notebook_id": nb["id"]}).json()] == [signed["id"]]
    # moving out of an archived notebook is allowed (a draft); signed records stay put as always
    draft = _exp(client, owner, other_nb["id"], "Draft to keep")
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": False}).status_code == 200
    assert client.patch(f"/api/experiments/{draft['id']}", headers=owner, json={"notebook_id": nb["id"]}).status_code == 200
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True}).status_code == 200
    assert client.patch(f"/api/experiments/{draft['id']}", headers=owner, json={"notebook_id": other_nb["id"]}).status_code == 200


def test_archived_notebooks_experiments_are_still_found(client, lab):
    owner, other = lab["nd_owner"][0], lab["nd_other"][0]
    nb = _nb(client, owner, "Old project zirconocene")
    e = _signed(client, owner, nb["id"], "Hydrozirconation of 1-octyne quokka")
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True}).status_code == 200
    for h in (owner, other):
        hits = client.get("/api/search", headers=h, params={"q": "quokka"}).json()["experiments"]
        assert [x["id"] for x in hits] == [e["id"]]
        hits = client.get("/api/search", headers=h, params={"q": "zirconocene"}).json()["experiments"]
        assert e["id"] in [x["id"] for x in hits], "not found by its notebook's name"
        assert e["id"] in [x["id"] for x in client.get("/api/experiments", headers=h, params={"notebook_id": nb["id"]}).json()]
    # its .eln export still works
    assert client.get(f"/api/notebooks/{nb['id']}/export.eln", headers=owner).status_code == 200


def test_an_archived_empty_notebook_can_still_be_deleted(client, admin, lab):
    owner, _ = lab["nd_owner"]
    nb = _nb(client, owner, "Archived and empty")
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True}).status_code == 200
    assert client.delete(f"/api/notebooks/{nb['id']}", headers=owner).status_code == 200
    _chain_ok(client, admin)
