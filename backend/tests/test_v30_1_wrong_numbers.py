"""0.30.1: wrong numbers and error pages found by the 0.30 stress test.

- The dashboard says how many records there are in all ("23 you've been asked to witness"), not how many it lists (10).
- Green metrics: no "Internal Server Error" when molecular weights or masses add up to zero or can't be worked out, and
  no impossible number (a negative atom economy, a PMI under 1); a record saved with such numbers still opens, on the
  Reactions page and in its report. Impossible inputs (a negative amount, a purity of 150 %) are pointed out.
- Junk preferences (NaN, "²") are dropped instead of giving "Internal Server Error".
- Deleting a group while someone is added to it, or a notebook is made in it, gives a plain answer, never a 500.
- Benchtop always keeps an active admin: you can't demote or disable yourself, and two admins demoting each other at
  the same moment can't both succeed.
- An .eln file with folders inside folders a thousand deep is read (it ran Python out of stack), and anything
  unforeseen in a file gives a plain message and doesn't leave the upload behind.
- After Benchtop stopped in the middle of an import, trying again works at once (it said "already being imported" for
  a day), and half-written exports and imports are cleared at start-up."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest
from sqlmodel import Session, select

PW = "lab-bench-pw-2026"


@pytest.fixture(autouse=True)
def _leave_no_uploads():
    """Uploads previewed here but not imported are removed afterwards (other tests check the staging folder is empty)."""
    from app.eln import scratch_dir
    d = scratch_dir() / "imports"
    before = set(d.iterdir()) if d.is_dir() else set()
    yield
    for f in (set(d.iterdir()) if d.is_dir() else set()) - before:
        f.unlink(missing_ok=True)


def _account(client, admin, username, role="scientist"):
    r = client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                       "full_name": username.title(), "role": role})
    assert r.status_code == 201, r.text
    tok = client.post("/api/auth/login", json={"username": username, "password": PW}).json()["token"]
    return {"Authorization": "Bearer " + tok}, r.json()["id"]


class At:
    """Run `then` (another request, in a thread) the first time it's called, and wait up to a second for it. The old
    code was exposed at that moment; the fixed code takes the write lock right after, so `then` happens first."""

    def __init__(self, then):
        self.then, self.fired, self.result, self.thread = then, False, None, None

    def __call__(self):
        if self.fired:
            return
        self.fired = True
        self.thread = threading.Thread(target=lambda: setattr(self, "result", self.then()))
        self.thread.start()
        self.thread.join(1.0)

    def done(self):
        self.thread.join(30)
        return self.result


def _at_write_lock(monkeypatch, hook):
    from app import audit
    real = audit._take_write_lock

    def lock(session):
        hook()
        return real(session)
    monkeypatch.setattr(audit, "_take_write_lock", lock)


# ------------------------------------------------------------------ dashboard totals

def test_the_dashboard_counts_every_record_not_just_the_ten_it_lists(client, admin):
    author, _ = _account(client, admin, "dash_author")
    witness, wid = _account(client, admin, "dash_witness")
    nb = client.post("/api/notebooks", headers=author, json={"name": "Lots to witness"}).json()
    for i in range(13):
        e = client.post("/api/experiments", headers=author, json={"title": f"Signed {i}", "notebook_id": nb["id"]}).json()
        assert client.post(f"/api/experiments/{e['id']}/sign", headers=author, json={"password": PW, "comment": ""}).status_code == 200
        assert client.put(f"/api/experiments/{e['id']}/witness-request", headers=author, json={"witness_id": wid}).status_code == 200
    for i in range(12):
        client.post("/api/experiments", headers=author, json={"title": f"Draft {i}", "notebook_id": nb["id"]})
    w = client.get("/api/dashboard", headers=witness).json()
    assert len(w["lists"]["awaiting"]) == 10  # the list stays short
    assert w["totals"]["awaiting"] == 13
    a = client.get("/api/dashboard", headers=author).json()
    assert len(a["lists"]["open"]) == 10 and a["totals"]["open"] == 12
    assert len(a["lists"]["waiting"]) == 10 and a["totals"]["waiting"] == 13
    assert set(a["totals"]) >= {"open", "awaiting", "could_witness", "waiting", "returned", "stale"}


# ------------------------------------------------------------------ green metrics

def _rows(*rows):
    from app import chem
    return [chem.new_row(**r) for r in rows]


ODD = {
    "molecular weights add up to zero": [dict(role="reactant", mw=50, mass_mg=10), dict(role="reactant", mw=-50, mass_mg=10),
                                         dict(role="product", mw=100, actual_mass_mg=5)],
    "masses add up to zero": [dict(role="reactant", mw=50, mass_mg=10), dict(role="reactant", mw=60, mass_mg=-10),
                              dict(role="product", mw=100, actual_mass_mg=5)],
    "too big to work out": [dict(role="reactant", mw=1e-300, mass_mg=10), dict(role="product", mw=1e300, actual_mass_mg=5)],
    "too small to work out": [dict(role="reactant", mw=50, mass_mg=1e-300), dict(role="product", mw=40, actual_mass_mg=1e300)],
    "negative molecular weight": [dict(role="reactant", mw=-50, mass_mg=10), dict(role="product", mw=100, actual_mass_mg=5)],
    "negative isolated mass": [dict(role="reactant", mw=50, mass_mg=100), dict(role="product", mw=40, actual_mass_mg=-30)],
    "more product than went in": [dict(role="reactant", mw=50, mass_mg=100), dict(role="product", mw=40, actual_mass_mg=150)],
}


@pytest.mark.parametrize("case", ODD)
def test_green_metrics_never_fail_and_never_show_an_impossible_number(case):
    from app import chem
    m = chem.green_metrics(_rows(*ODD[case]))
    json.dumps(m, allow_nan=False)
    assert m.get("atom_economy_pct") is None or 0 <= m["atom_economy_pct"] <= 100
    assert m.get("rme_pct") is None or 0 <= m["rme_pct"] <= 100
    assert m.get("pmi") is None or m["pmi"] >= 1
    assert m.get("e_factor") is None or m["e_factor"] >= 0
    assert m.get("solvent_l_per_kg") is None or m["solvent_l_per_kg"] >= 0


def test_more_product_than_went_in_is_called_impossible():
    from app import chem
    m = chem.green_metrics(_rows(*ODD["more product than went in"]))
    assert "pmi" in m["impossible"] and "rme" in m["impossible"]
    assert m.get("pmi") is None and m.get("e_factor") is None
    ok = chem.green_metrics(_rows(dict(role="reactant", mw=50, mass_mg=100), dict(role="product", mw=40, actual_mass_mg=60)))
    assert ok["pmi"] == pytest.approx(1.66667, rel=1e-4) and ok["e_factor"] == pytest.approx(0.666667, rel=1e-4) and "impossible" not in ok


def test_the_calculator_and_saving_take_odd_numbers(client, users, notebook):
    h = users["alice"]
    for case, rows in ODD.items():
        rows = [{**r, "name": f"Row {i}", "basis": "mass"} for i, r in enumerate(rows)]
        r = client.post("/api/chem/stoichiometry", headers=h, json={"rows": rows})
        assert r.status_code == 200, (case, r.text)
        e = client.post("/api/experiments", headers=h, json={"title": f"Odd: {case}", "notebook_id": notebook["id"]}).json()
        r = client.post(f"/api/experiments/{e['id']}/entries", headers=h, json={"kind": "reaction", "title": "Reaction", "content": {"rows": rows}})
        assert r.status_code == 201, (case, r.text)


def test_impossible_inputs_are_pointed_out(client, users):
    rows = [{"role": "reactant", "name": "Benzoic acid", "smiles": "OC(=O)c1ccccc1", "limiting": True, "basis": "mass", "mass_mg": 122},
            {"role": "reagent", "name": "Sodium ascorbate", "mw": 198.1, "basis": "mass", "mass_mg": -5},
            {"role": "reagent", "name": "Base", "mw": 101.2, "basis": "equiv", "equiv": -1, "purity": 150, "density": 0}]
    w = client.post("/api/chem/stoichiometry", headers=users["alice"], json={"rows": rows}).json()["warnings"]
    assert "Check Sodium ascorbate: the mass can't be negative" in w
    assert "Check Base: the equivalents can't be negative" in w
    assert any("Check Base: a purity of 150 %" in x for x in w)
    assert any("Check Base: a density of 0 g/mL" in x for x in w)
    assert not any("Benzoic acid" in x for x in w)
    fine = client.post("/api/chem/stoichiometry", headers=users["alice"], json={"rows": rows[:1]}).json()["warnings"]
    assert not any(x.startswith("Check") for x in fine)


def test_a_record_stored_with_odd_numbers_still_shows(client, users, notebook):
    """Saved by an older version (or imported): the Reactions page, the record and its report still open."""
    from app.db import engine
    from app.models import Entry
    h = users["alice"]
    e = client.post("/api/experiments", headers=h, json={"title": "Stored with odd numbers", "notebook_id": notebook["id"]}).json()
    client.post(f"/api/experiments/{e['id']}/entries", headers=h, json={"kind": "reaction", "title": "Stored reaction", "content": {"rows": [
        {"role": "reactant", "name": "A", "mw": 50, "mass_mg": 10, "basis": "mass"}, {"role": "product", "name": "P", "mw": 40, "actual_mass_mg": 5}]}})
    with Session(engine) as s:
        en = s.exec(select(Entry).where(Entry.experiment_id == e["id"], Entry.title == "Stored reaction")).one()
        c = dict(en.content)
        c["rows"] = [{"role": "reactant", "name": "A", "mw": 50, "mass_mg": 10, "basis": "mass"},
                     {"role": "reactant", "name": "B", "mw": 60, "mass_mg": -10, "basis": "mass"},  # the masses add up to 0
                     {"role": "product", "name": "P", "mw": 100, "actual_mass_mg": 5}]
        c["totals"] = {"metrics": {"atom_economy_pct": -200.0, "pmi": -3.3, "e_factor": -4.3}}
        en.content = c
        s.add(en)
        s.commit()
    r = client.get("/api/reactions", headers=h)
    assert r.status_code == 200
    mine = [x for x in r.json() if x["experiment_id"] == e["id"]]
    assert mine and mine[0]["metrics"].get("rme_pct") is None and mine[0]["metrics"].get("pmi") is None
    assert client.get(f"/api/experiments/{e['id']}", headers=h).status_code == 200
    for fmt in ("html", "pdf"):
        r = client.get(f"/api/experiments/{e['id']}/export", headers=h, params={"format": fmt})
        assert r.status_code == 200, (fmt, r.text[:200])
    page = client.get(f"/api/experiments/{e['id']}/export", headers=h, params={"format": "html"}).text
    assert "Stored reaction" in page and "couldn't be shown" not in page
    assert "atom economy -" not in page


# ------------------------------------------------------------------ preferences

@pytest.mark.parametrize("body", [
    '{"preferences": {"default_notebook_id": NaN}}',
    '{"preferences": {"default_notebook_id": "\\u00b2"}}',
    '{"preferences": {"default_notebook_id": 1e400}}',
    '{"preferences": {"default_notebook_id": 99999999999999999999999999999}}',
    '{"preferences": {"pinned": [NaN, 1e400, -3, "7", true, 4]}}',
])
def test_junk_preferences_are_dropped_not_a_server_error(client, users, body):
    r = client.patch("/api/auth/me", headers={**users["bob"], "Content-Type": "application/json"}, content=body)
    assert r.status_code == 200, r.text
    prefs = r.json()["preferences"]
    assert prefs.get("default_notebook_id") is None
    assert all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in prefs.get("pinned", []))
    assert client.get("/api/dashboard", headers=users["bob"]).status_code == 200


def test_ordinary_preferences_still_work(client, users):
    nb = client.post("/api/notebooks", headers=users["bob"], json={"name": "Bob's default"}).json()
    r = client.patch("/api/auth/me", headers=users["bob"], json={"preferences": {"default_notebook_id": str(nb["id"]), "pinned": [5, 6.0]}})
    assert r.status_code == 200 and r.json()["preferences"]["default_notebook_id"] == nb["id"]
    assert r.json()["preferences"]["pinned"] == [5, 6]


# ------------------------------------------------------------------ groups

def test_deleting_a_group_while_a_notebook_is_made_in_it(client, admin, monkeypatch):
    g = client.post("/api/groups", headers=admin, json={"name": "Group deleted while a notebook is made"}).json()
    made = At(lambda: client.post("/api/notebooks", headers=admin, json={"name": "In the group", "group_id": g["id"], "visibility": "group"}))
    _at_write_lock(monkeypatch, made)
    r = client.delete(f"/api/groups/{g['id']}", headers=admin)
    nb = made.done()
    assert r.status_code == 200, r.text
    assert nb.status_code in (201, 404), nb.text
    if nb.status_code == 201:  # made first: the group's deletion made it private, as it does for any group notebook
        assert nb.json()["id"] in r.json()["notebooks"]
        from app.db import engine
        from app.models import Notebook
        with Session(engine) as s:
            made_nb = s.get(Notebook, nb.json()["id"])
            assert made_nb.visibility == "private" and made_nb.group_id is None


def test_adding_someone_to_a_group_that_is_being_deleted(client, admin, monkeypatch):
    g = client.post("/api/groups", headers=admin, json={"name": "Group deleted while someone joins"}).json()
    _, uid = _account(client, admin, "grp_newcomer")
    gone = At(lambda: client.delete(f"/api/groups/{g['id']}", headers=admin))
    _at_write_lock(monkeypatch, gone)
    r = client.put(f"/api/groups/{g['id']}/members/{uid}", headers=admin, json={"role": "member"})
    assert gone.done().status_code == 200
    assert r.status_code == 404, r.text
    assert "no longer exists" in r.json()["detail"]
    from app.db import engine
    from app.models import GroupMember
    with Session(engine) as s:
        assert not s.exec(select(GroupMember).where(GroupMember.group_id == g["id"])).all()


def test_a_notebook_in_a_group_that_is_gone_says_so(client, admin):
    g = client.post("/api/groups", headers=admin, json={"name": "Short-lived group"}).json()
    client.delete(f"/api/groups/{g['id']}", headers=admin)
    r = client.post("/api/notebooks", headers=admin, json={"name": "Too late", "group_id": g["id"], "visibility": "group"})
    assert r.status_code == 404 and "no longer exists" in r.json()["detail"]


# ------------------------------------------------------------------ admins

def test_an_admin_cant_demote_or_disable_themselves(client, admin):
    me = client.get("/api/auth/me", headers=admin).json()
    r = client.patch(f"/api/users/{me['id']}", headers=admin, json={"role": "scientist"})
    assert r.status_code == 409 and r.json()["detail"] == "You can't change your own role. Another admin can."
    r = client.patch(f"/api/users/{me['id']}", headers=admin, json={"active": False})
    assert r.status_code == 409 and r.json()["detail"] == "You can't disable your own account. Another admin can."
    assert client.get("/api/auth/me", headers=admin).json()["role"] == "admin"
    # changing your own name, or another person's role, still works
    assert client.patch(f"/api/users/{me['id']}", headers=admin, json={"full_name": me["full_name"] or "Admin"}).status_code == 200


def test_two_admins_demoting_each_other_at_once_leave_one(client, admin, monkeypatch):
    from app.db import engine
    from app.models import Role, User
    x, xid = _account(client, admin, "adm_x", role="admin")
    y, yid = _account(client, admin, "adm_y", role="admin")
    me = client.get("/api/auth/me", headers=admin).json()
    try:
        assert client.patch(f"/api/users/{me['id']}", headers=x, json={"role": "scientist"}).status_code == 200  # X and Y are the admins now
        other = At(lambda: client.patch(f"/api/users/{xid}", headers=y, json={"role": "scientist"}))
        _at_write_lock(monkeypatch, other)
        first = client.patch(f"/api/users/{yid}", headers=x, json={"role": "scientist"})
        second = other.done()
        monkeypatch.undo()
        codes = sorted([first.status_code, second.status_code])
        assert codes == [200, 409], (first.text, second.text)
        refused = first if first.status_code == 409 else second
        assert "the only active admin left" in refused.json()["detail"]
        with Session(engine) as s:
            assert s.exec(select(User).where(User.role == Role.admin, User.active == True)).all()  # noqa: E712
        # and the last one can't be disabled by a stale second admin either (disabling counts too)
        survivor = y if first.status_code == 409 else x
        last_id = yid if first.status_code == 409 else xid
        r = client.patch(f"/api/users/{last_id}", headers=survivor, json={"active": False})
        assert r.status_code == 409
    finally:
        with Session(engine) as s:  # put the test admin back, whatever happened
            u = s.get(User, me["id"])
            u.role, u.active = Role.admin, True
            s.add(u)
            s.commit()


# ------------------------------------------------------------------ .eln files

def _nodes(*extra):
    import hashlib
    data = b"time,signal\n0,1\n"
    return data, [{"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "./exp/"}]},
                  {"@id": "./exp/", "@type": "Dataset", "name": "Grignard", "hasPart": [{"@id": "./exp/trace.csv"}, *extra]},
                  {"@id": "./exp/trace.csv", "@type": "File", "name": "trace.csv", "sha256": hashlib.sha256(data).hexdigest()}]


def _staged_files():
    from app.eln import scratch_dir
    d = scratch_dir() / "imports"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_folders_nested_a_thousand_deep_are_read(client, users):
    from tests.test_v20_eln import _crate, _preview
    data, nodes = _nodes({"@id": "#d0"})
    n = 3000
    nodes += [{"@id": f"#d{k}", "@type": "Dataset", "name": f"d{k}", "hasPart": [{"@id": f"#d{k + 1}"}]} for k in range(n)]
    nodes[1]["hasPart"].append({"@id": "#loop"})
    nodes += [{"@id": "#loop", "@type": "Dataset", "name": "loop", "hasPart": [{"@id": "#loop"}, {"@id": "./exp/"}]}]
    r = _preview(client, users["bob"], _crate(nodes, {"exp/trace.csv": data}))
    assert r.status_code == 200, r.text[:300]
    item = r.json()["items"][0]
    assert item["title"] == "Grignard" and item["files"] == 1
    assert any("nested more than" in w for w in item["warnings"])


def test_an_unforeseen_problem_with_a_file_is_said_plainly_and_the_upload_is_not_kept(client, users, monkeypatch):
    from app import eln_import
    from tests.test_v20_eln import _crate, _preview
    data, nodes = _nodes()
    before = _staged_files()

    def broken(*a, **k):
        raise KeyError("something nobody foresaw")
    monkeypatch.setattr(eln_import, "preview", broken)
    r = _preview(client, users["bob"], _crate(nodes, {"exp/trace.csv": data}))
    assert r.status_code == 422
    assert "couldn't make sense of this .eln file" in r.json()["detail"]
    assert _staged_files() == before


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_after_a_crash_mid_import_trying_again_works(client, users):
    from app.eln import scratch_dir
    from tests.test_v20_eln import _crate, _preview
    data, nodes = _nodes()
    nb = client.post("/api/notebooks", headers=users["bob"], json={"name": "Import after a crash"}).json()
    for marker in (f"{_dead_pid()} 0123456789ab", f"{os.getpid()} 0123456789ab", ""):  # a stopped Benchtop; an earlier run; 0.30.0
        p = _preview(client, users["bob"], _crate(nodes, {"exp/trace.csv": data})).json()
        busy = scratch_dir() / "imports" / f"{p['token']}.importing"
        busy.write_text(marker)
        if not marker:
            old = time.time() - 120
            os.utime(busy, (old, old))
        r = client.post(f"/api/eln/import/{p['token']}", headers=users["bob"], json={"notebook_id": nb["id"], "items": ["0"]})
        assert r.status_code == 200, (marker, r.text)
        assert len(r.json()["created"]) == 1
        assert not busy.exists()


def test_importing_again_after_it_stopped_half_way_adds_only_the_rest(client, users, monkeypatch):
    """Benchtop stopped (or one record failed) after some records went in: pressing Import again mustn't copy those twice."""
    import hashlib

    from app import eln_import
    from tests.test_v20_eln import _crate, _preview
    data = b"time,signal\n0,1\n"
    nodes = [{"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "./a/"}, {"@id": "./b/"}, {"@id": "./c/"}]}]
    files = {}
    for k in "abc":
        nodes += [{"@id": f"./{k}/", "@type": "Dataset", "name": f"Record {k}", "hasPart": [{"@id": f"./{k}/trace.csv"}]},
                  {"@id": f"./{k}/trace.csv", "@type": "File", "name": "trace.csv", "sha256": hashlib.sha256(data).hexdigest()}]
        files[f"{k}/trace.csv"] = data
    nb = client.post("/api/notebooks", headers=users["bob"], json={"name": "Imported in two goes"}).json()
    p = _preview(client, users["bob"], _crate(nodes, files)).json()
    keys = [i["key"] for i in p["items"]]
    real = eln_import._import_one

    def stops_at_c(session, user, crate, it, *a, **k):
        if it.title == "Record c":
            raise OSError("Benchtop stopped here")
        return real(session, user, crate, it, *a, **k)
    monkeypatch.setattr(eln_import, "_import_one", stops_at_c)
    first = client.post(f"/api/eln/import/{p['token']}", headers=users["bob"], json={"notebook_id": nb["id"], "items": keys}).json()
    assert len(first["created"]) == 2 and len(first["failed"]) == 1
    monkeypatch.undo()
    again = client.post(f"/api/eln/import/{p['token']}", headers=users["bob"], json={"notebook_id": nb["id"], "items": keys})
    assert again.status_code == 200, again.text
    assert sorted(x["title"] for x in again.json()["created"]) == ["Record a", "Record b", "Record c"]
    assert {x["id"] for x in first["created"]} <= {x["id"] for x in again.json()["created"]}
    titles = [e["title"] for e in client.get(f"/api/experiments?notebook_id={nb['id']}", headers=users["bob"]).json()]
    assert sorted(titles) == ["Record a", "Record b", "Record c"]


def test_an_import_that_is_running_still_stops_a_second_one(client, users):
    from app import eln_import
    from app.eln import scratch_dir
    from tests.test_v20_eln import _crate, _preview
    data, nodes = _nodes()
    nb = client.post("/api/notebooks", headers=users["bob"], json={"name": "Import running"}).json()
    p = _preview(client, users["bob"], _crate(nodes, {"exp/trace.csv": data})).json()
    busy = scratch_dir() / "imports" / f"{p['token']}.importing"
    busy.write_text(f"{os.getpid()} {eln_import._RUN}")
    try:
        r = client.post(f"/api/eln/import/{p['token']}", headers=users["bob"], json={"notebook_id": nb["id"], "items": ["0"]})
        assert r.status_code == 422 and "already being imported" in r.json()["detail"]
    finally:
        busy.unlink(missing_ok=True)


def test_start_up_clears_what_an_interrupted_import_or_export_left(tmp_path):
    from app import eln_import
    from app.eln import scratch_dir
    d = scratch_dir()
    dead = _dead_pid()
    left = [d / f"export-{dead}-101010101010.eln", d / f"import-{dead}-abcdef012345.part", d / "imports" / "tok-left-behind.importing"]
    for f in left:
        f.parent.mkdir(exist_ok=True)
        f.write_text(f"{dead} 0123456789ab" if f.suffix == ".importing" else "half")
    live = d / "imports" / "tok-running-now.importing"
    live.write_text(f"{os.getpid()} {eln_import._RUN}")
    staged = d / "imports" / "tok-kept.eln"
    staged.write_text("an upload waiting for its import")
    try:
        eln_import.clear_leftovers()
        assert not any(f.exists() for f in left)
        assert live.exists() and staged.exists()  # an import running now, and an upload that can be imported again
    finally:
        for f in (live, staged):
            f.unlink(missing_ok=True)


def test_the_lifespan_runs_the_clean_up():
    import inspect

    from app import main
    assert "eln_import.clear_leftovers()" in inspect.getsource(main.lifespan)

