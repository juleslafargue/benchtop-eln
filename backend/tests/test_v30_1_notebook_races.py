"""0.30.1: notebooks deleted, archived or changed at the same moment as something else (found by the 0.30 stress test).

- A deleted notebook's number is never given to a new one (database step 6), so a late or repeated request for the
  deleted notebook can't reach another one.
- Deleting or changing a notebook reads it after taking the write lock: a second click, or a request that waited
  while another deleted it, gets "not found", never a 500, a second audit event, or someone else's new notebook.
- People who can't open a notebook get 404 for changing it too (not 403, which says it exists).
- A default notebook for new experiments must exist, be one you can open and not be archived; set at the moment the
  notebook is deleted, it doesn't end up pointing nowhere.
- New experiments (created, moved, duplicated, scaled, followed up, imported) check the notebook under the write lock
  just before they go in: it can't be deleted (500 FOREIGN KEY failed) or archived in between. A record that can't be
  imported says why plainly and leaves none of its files behind.

The races are made to happen every time: another request runs at the exact moment the old code was exposed, and the
test waits a moment for it. With the fix, that request waits for the lock instead, so the two happen one after the
other, in either order, and both outcomes are fine."""
from __future__ import annotations

import hashlib
import threading

import pytest
from sqlmodel import Session, select

PW = "lab-bench-pw-2026"


def _account(client, admin, username, role="scientist"):
    r = client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                       "full_name": username.title(), "role": role})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": username, "password": PW}).json()["token"]}


@pytest.fixture(scope="module")
def lab(client, admin):
    return {name: _account(client, admin, name) for name in ("nr_owner", "nr_other")}


def _nb(client, h, name, **kw):
    r = client.post("/api/notebooks", headers=h, json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _exists(nb_id):
    from app.db import engine
    from app.models import Notebook
    with Session(engine) as s:
        return s.get(Notebook, nb_id)


def _events(client, admin, nb_id, action):
    evs = client.get("/api/audit", headers=admin, params={"limit": 500, "object_type": "notebook"}).json()
    return [e for e in evs if e["object_id"] == nb_id and e["action"] == action]


class At:
    """Run `then` (another request, in a thread) the first time `fire` is reached, and wait up to a second for it.
    The old code was exposed there; the fixed code holds the write lock by then, so `then` waits for this request."""

    def __init__(self, then):
        self.then, self.fired, self.finished_inside, self.result = then, False, None, None

    def __call__(self):
        if self.fired:
            return
        self.fired = True
        t = threading.Thread(target=lambda: setattr(self, "result", self.then()))
        t.start()
        t.join(1.0)
        self.finished_inside = not t.is_alive()
        self.thread = t

    def done(self):
        self.thread.join(30)
        return self.result


def _at_write_lock(monkeypatch, hook):
    """`hook` runs just before this request takes the write lock (the old code had read the notebook by then)."""
    from app import audit
    real = audit._take_write_lock

    def lock(session):
        hook()
        return real(session)
    monkeypatch.setattr(audit, "_take_write_lock", lock)


def _after_access_check(monkeypatch, nb_id, hook):
    """`hook` runs just after this request checked that the person can open the notebook (the old code then inserted
    the new experiment without looking at the notebook again)."""
    from app import access
    real = access.Access.can_read_notebook

    def check(self, nb):
        ok = real(self, nb)
        if nb.id == nb_id:
            hook()
        return ok
    monkeypatch.setattr(access.Access, "can_read_notebook", check)


# ------------------------------------------------------------------ numbers

def test_a_deleted_notebooks_number_is_never_given_again(client, lab):
    owner, other = lab["nr_owner"], lab["nr_other"]
    first = _nb(client, owner, "Made by mistake")
    assert client.delete(f"/api/notebooks/{first['id']}", headers=owner).status_code == 200
    second = _nb(client, other, "Someone else's new notebook", visibility="private")
    assert second["id"] > first["id"], "the deleted notebook's number was given to the next one"
    # the old number stays dead: late requests for it find nothing, and don't touch the new notebook
    assert client.delete(f"/api/notebooks/{first['id']}", headers=owner).status_code == 404
    assert client.patch(f"/api/notebooks/{first['id']}", headers=owner, json={"archived": True}).status_code == 404
    assert _exists(second["id"]).archived is False


def test_the_notebook_table_never_reuses_numbers_and_still_guards_its_experiments(client):
    from sqlalchemy import text

    from app.db import engine
    with engine.connect() as c:
        sql = c.execute(text("SELECT sql FROM sqlite_master WHERE name='notebook'")).scalar()
        assert "AUTOINCREMENT" in sql.upper()
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1


# ------------------------------------------------------------------ deleting and changing

def test_a_late_delete_doesnt_reach_someone_elses_new_notebook(client, admin, lab, monkeypatch):
    owner, other = lab["nr_owner"], lab["nr_other"]
    nb = _nb(client, owner, "Double-clicked")
    made = {}

    def meanwhile():  # the first click deletes it, and a colleague makes a new private notebook
        r1 = client.delete(f"/api/notebooks/{nb['id']}", headers=owner)
        made["new"] = _nb(client, other, "Colleague's private notebook", visibility="private")
        return r1.status_code
    at = At(meanwhile)
    _at_write_lock(monkeypatch, at)
    r = client.delete(f"/api/notebooks/{nb['id']}", headers=owner)  # the second click
    monkeypatch.undo()
    assert at.done() == 200 and r.status_code == 404, r.text
    assert _exists(made["new"]["id"]) is not None, "the late delete deleted a colleague's new notebook"
    assert len(_events(client, admin, nb["id"], "delete")) == 1, "a double click wrote the delete twice"
    v = client.get("/api/audit/verify", headers=admin).json()
    assert v["ok"], v


def test_a_change_racing_a_delete_is_404_not_500_and_never_reaches_a_new_notebook(client, admin, lab, monkeypatch):
    owner, other = lab["nr_owner"], lab["nr_other"]
    nb = _nb(client, owner, "Archive or delete?")
    made = {}

    def meanwhile():
        r1 = client.delete(f"/api/notebooks/{nb['id']}", headers=owner)
        made["new"] = _nb(client, other, "Brand-new notebook", visibility="private")
        return r1.status_code
    at = At(meanwhile)
    _at_write_lock(monkeypatch, at)
    r = client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True, "name": "Renamed"})
    monkeypatch.undo()
    assert at.done() == 200
    assert r.status_code == 404, r.text
    new = _exists(made["new"]["id"])
    assert new.name == "Brand-new notebook" and new.archived is False
    assert not _events(client, admin, nb["id"], "update")


def test_changing_a_notebook_you_cant_open_says_not_found(client, lab):
    owner, other = lab["nr_owner"], lab["nr_other"]
    hidden = _nb(client, other, "Private to nr_other", visibility="private")
    for body in ({"archived": True}, {"name": "Mine now"}):
        r = client.patch(f"/api/notebooks/{hidden['id']}", headers=owner, json=body)
        assert r.status_code == 404 and r.json()["detail"] == f"Notebook {hidden['id']} not found", r.text
    assert client.patch("/api/notebooks/99999999", headers=owner, json={"archived": True}).status_code == 404
    shared = _nb(client, other, "Everyone can see this one")
    r = client.patch(f"/api/notebooks/{shared['id']}", headers=owner, json={"archived": True})
    assert r.status_code == 403  # seen, but not theirs


# ------------------------------------------------------------------ default notebook

def test_a_default_notebook_must_be_one_you_can_put_experiments_in(client, lab):
    owner, other = lab["nr_owner"], lab["nr_other"]
    me = lambda: client.get("/api/auth/me", headers=owner).json()["preferences"].get("default_notebook_id")  # noqa: E731
    mine = _nb(client, owner, "My default")
    assert client.patch("/api/auth/me", headers=owner, json={"preferences": {"default_notebook_id": mine["id"]}}).status_code == 200
    hidden = _nb(client, other, "Not yours", visibility="private")
    archived = _nb(client, owner, "Archived one")
    client.patch(f"/api/notebooks/{archived['id']}", headers=owner, json={"archived": True})
    gone = _nb(client, owner, "Deleted one")
    client.delete(f"/api/notebooks/{gone['id']}", headers=owner)
    for bad, words in ((99999999, "no longer exists"), (hidden["id"], "no longer exists"), (gone["id"], "no longer exists"),
                       (archived["id"], "is archived")):
        r = client.patch("/api/auth/me", headers=owner, json={"preferences": {"default_notebook_id": bad}})
        assert r.status_code == 422 and words in r.json()["detail"], r.text
        assert me() == mine["id"]
    assert client.patch("/api/auth/me", headers=owner, json={"preferences": {"default_notebook_id": None}}).status_code == 200
    assert me() is None


def test_a_default_set_while_the_notebook_is_deleted_doesnt_point_nowhere(client, lab, monkeypatch):
    owner = lab["nr_owner"]
    nb = _nb(client, owner, "Deleted while chosen")
    at = At(lambda: client.delete(f"/api/notebooks/{nb['id']}", headers=owner).status_code)
    _at_write_lock(monkeypatch, at)
    r = client.patch("/api/auth/me", headers=owner, json={"preferences": {"default_notebook_id": nb["id"]}})
    monkeypatch.undo()
    at.done()
    left = client.get("/api/auth/me", headers=owner).json()["preferences"].get("default_notebook_id")
    assert left is None or _exists(left) is not None, r.text


def test_another_preference_saved_while_the_default_is_deleted_doesnt_bring_it_back(client, lab, monkeypatch):
    owner = lab["nr_owner"]
    nb = _nb(client, owner, "Default, then deleted")
    client.patch("/api/auth/me", headers=owner, json={"preferences": {"default_notebook_id": nb["id"]}})
    at = At(lambda: client.delete(f"/api/notebooks/{nb['id']}", headers=owner).status_code)
    _at_write_lock(monkeypatch, at)
    r = client.patch("/api/auth/me", headers=owner, json={"preferences": {"theme": "dark"}})
    monkeypatch.undo()
    assert at.done() == 200 and r.status_code == 200
    prefs = client.get("/api/auth/me", headers=owner).json()["preferences"]
    assert prefs.get("theme") == "dark" and prefs.get("default_notebook_id") is None


# ------------------------------------------------------------------ new experiments at the same moment

def _signed(client, h, nb_id):
    e = client.post("/api/experiments", headers=h, json={"title": "Signed one", "notebook_id": nb_id}).json()
    assert client.post(f"/api/experiments/{e['id']}/sign", headers=h, json={"password": PW}).status_code == 200
    return e["id"]


WAYS = {
    "create": lambda client, h, nb, src, sig, there: client.post("/api/experiments", headers=h, json={"title": "New", "notebook_id": nb}),
    "move": lambda client, h, nb, src, sig, there: client.patch(f"/api/experiments/{there}", headers=h, json={"notebook_id": nb}),
    "duplicate": lambda client, h, nb, src, sig, there: client.post(f"/api/experiments/{src}/clone", headers=h),
    "scale": lambda client, h, nb, src, sig, there: client.post(f"/api/experiments/{src}/clone?scale=2", headers=h),
    "follow-up": lambda client, h, nb, src, sig, there: client.post(f"/api/experiments/{sig}/follow-up", headers=h, json={}),
}


@pytest.mark.parametrize("way", list(WAYS))
@pytest.mark.parametrize("what", ["delete", "archive"])
def test_a_new_experiment_never_lands_in_a_notebook_deleted_or_archived_at_that_moment(client, lab, monkeypatch, way, what):
    owner = lab["nr_owner"]
    nb = _nb(client, owner, f"{what} during {way}")
    elsewhere = _nb(client, owner, f"elsewhere for {way}")
    there = client.post("/api/experiments", headers=owner, json={"title": "To move", "notebook_id": elsewhere["id"]}).json()["id"]
    src = sig = None
    if way in ("duplicate", "scale", "follow-up"):
        if what == "delete":
            pytest.skip("a notebook holding the record being copied can't be deleted")
        src = client.post("/api/experiments", headers=owner, json={"title": "Source", "notebook_id": nb["id"]}).json()["id"]
        sig = _signed(client, owner, nb["id"])
    other = (lambda: client.delete(f"/api/notebooks/{nb['id']}", headers=owner)) if what == "delete" else \
        (lambda: client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True}))
    at = At(lambda: other().status_code)
    _after_access_check(monkeypatch, nb["id"], at)
    r = WAYS[way](client, owner, nb["id"], src, sig, there)
    monkeypatch.undo()
    theirs = at.done()
    assert at.fired
    assert r.status_code < 500, r.text
    if r.status_code in (200, 201):
        # it went in first, and the delete or archive came after it (so a delete was refused: it holds an experiment)
        assert not at.finished_inside, f"the experiment went into a notebook that was {what}d a moment before"
        assert theirs == (409 if what == "delete" else 200)
    else:
        assert r.status_code in (404, 409), r.text
        assert ("archived" in r.json()["detail"]) if what == "archive" else ("not found" in r.json()["detail"])
    from app.db import engine
    from app.models import Experiment, Notebook
    with Session(engine) as s:
        ids = {n.id for n in s.exec(select(Notebook))}
        assert not [e.id for e in s.exec(select(Experiment)) if e.notebook_id is not None and e.notebook_id not in ids]


@pytest.mark.parametrize("what", ["delete", "archive"])
def test_an_import_into_a_notebook_deleted_or_archived_meanwhile_says_so_and_keeps_none_of_its_files(client, lab, monkeypatch, what):
    from app import storage
    from tests.test_v20_eln import _crate, _preview
    owner = lab["nr_owner"]
    nb = _nb(client, owner, f"Import target, {what}d meanwhile")
    data = f"time,signal\n0,1\n{what} race {nb['id']}\n".encode()
    sha = hashlib.sha256(data).hexdigest()
    crate = _crate([{"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "./exp/"}]},
                    {"@id": "./exp/", "@type": "Dataset", "name": f"Imported while {what}d", "text": "<p>notes</p>",
                     "hasPart": [{"@id": "./exp/trace.csv"}]},
                    {"@id": "./exp/trace.csv", "@type": "File", "name": "trace.csv", "encodingFormat": "text/csv", "sha256": sha}],
                   {"exp/trace.csv": data})
    tok = _preview(client, owner, crate).json()["token"]
    other = (lambda: client.delete(f"/api/notebooks/{nb['id']}", headers=owner)) if what == "delete" else \
        (lambda: client.patch(f"/api/notebooks/{nb['id']}", headers=owner, json={"archived": True}))
    at = At(lambda: other().status_code)
    _after_access_check(monkeypatch, nb["id"], at)
    r = client.post(f"/api/eln/import/{tok}", headers=owner, json={"notebook_id": nb["id"], "items": ["0"]})
    monkeypatch.undo()
    assert at.done() == 200 and at.finished_inside  # the import checked the notebook, then copied the file: room for it
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["created"] == [] and len(res["failed"]) == 1
    msg = res["failed"][0]["error"]
    assert ("was deleted meanwhile" in msg) if what == "delete" else ("is archived" in msg), msg
    assert "Error" not in msg and "constraint" not in msg.lower()
    from app.db import engine
    from app.models import Attachment, Experiment
    with Session(engine) as s:
        assert not s.exec(select(Experiment).where(Experiment.notebook_id == nb["id"])).all()
        assert not s.exec(select(Attachment).where(Attachment.sha256 == sha)).all()
    assert not storage.blob_path(sha).exists(), "the failed import left its file in the store"
    client.delete(f"/api/eln/import/{tok}", headers=owner)


def test_a_failed_import_keeps_files_something_else_uses(client, lab, monkeypatch):
    """The same file is already attached to another record: a failed import doesn't remove it."""
    from app import storage
    from tests.test_v20_eln import _crate, _preview
    owner = lab["nr_owner"]
    nb = _nb(client, owner, "Shared file target")
    keep = _nb(client, owner, "Holds the file")
    exp = client.post("/api/experiments", headers=owner, json={"title": "Has the file", "notebook_id": keep["id"]}).json()
    data = b"time,signal\n0,1\nshared between two records\n"
    sha = hashlib.sha256(data).hexdigest()
    crate = _crate([{"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "./exp/"}]},
                    {"@id": "./exp/", "@type": "Dataset", "name": "Shares a file", "hasPart": [{"@id": "./exp/shared.csv"}]},
                    {"@id": "./exp/shared.csv", "@type": "File", "name": "shared.csv", "sha256": sha}],
                   {"exp/shared.csv": data})
    tok = _preview(client, owner, crate).json()["token"]

    def meanwhile():  # once the import has stored the file: someone attaches the same file elsewhere, and the notebook goes
        client.post(f"/api/experiments/{exp['id']}/attachments", headers=owner, files={"file": ("shared.csv", data, "text/csv")})
        return client.delete(f"/api/notebooks/{nb['id']}", headers=owner).status_code
    at = At(meanwhile)
    _at_write_lock(monkeypatch, at)
    r = client.post(f"/api/eln/import/{tok}", headers=owner, json={"notebook_id": nb["id"], "items": ["0"]})
    monkeypatch.undo()
    assert at.done() == 200 and r.json()["failed"]
    assert storage.blob_path(sha).is_file() and storage.intact(storage.blob_path(sha), sha)
    assert client.get(f"/api/experiments/{exp['id']}", headers=owner).json()["attachments"]
    client.delete(f"/api/eln/import/{tok}", headers=owner)


def test_an_unexpected_error_while_importing_is_said_plainly(client, lab, monkeypatch):
    from app import eln_import
    from tests.test_v20_eln import _crate, _preview
    owner = lab["nr_owner"]
    nb = _nb(client, owner, "Import that breaks")
    crate = _crate([{"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "./exp/"}]},
                    {"@id": "./exp/", "@type": "Dataset", "name": "Breaks", "text": "<p>x</p>"}], {})
    tok = _preview(client, owner, crate).json()["token"]

    def broken(*a, **k):
        from sqlalchemy.exc import IntegrityError
        raise IntegrityError("INSERT INTO experiment …", {}, Exception("FOREIGN KEY constraint failed"))
    monkeypatch.setattr(eln_import, "_new_entry", broken)
    r = client.post(f"/api/eln/import/{tok}", headers=owner, json={"notebook_id": nb["id"], "items": ["0"]})
    monkeypatch.undo()
    msg = r.json()["failed"][0]["error"]
    assert "IntegrityError" not in msg and "FOREIGN KEY" not in msg and "nothing of it was kept" in msg, msg
    client.delete(f"/api/eln/import/{tok}", headers=owner)
