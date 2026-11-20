"""0.18.3: fixes from stress-testing instrument folders, backups and sample codes.

Each test failed before its fix (see the commit). They use the real code paths: folders on disk, the scan,
the API, and a second thread playing a colleague."""
import errno
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app import backup, instruments
from app.config import settings
from app.db import engine
from app.models import Entry, InstrumentFile, Notification

PW = "lab-bench-pw-2026"
T0 = datetime(2038, 1, 1, 9, 0, 0)


def scan(seconds: float):
    return instruments.scan_all(now=T0 + timedelta(seconds=seconds))


def new_exp(client, h, nb, title="Stress exp"):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def atts(client, h, exp):
    return client.get(f"/api/experiments/{exp['id']}", headers=h).json()["attachments"]


@pytest.fixture()
def watch(client, admin, tmp_path):
    made = []

    def _watch(kind="files", name="Stress folder", **kw):
        d = tmp_path / f"share{len(made)}"
        d.mkdir()
        r = client.post("/api/instruments", headers=admin, json={"name": name, "kind": kind, "folder": str(d), **kw})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        scan(0)
        return d, r.json()["id"]
    yield _watch
    for i in made:
        client.delete(f"/api/instruments/{i}", headers=admin)


def colleague_saves_while(client, h, nb, during):
    """Run `during()` while a colleague saves; returns how long the save took (it must not wait for the import)."""
    out = {}

    def save():
        t = time.monotonic()
        out["status"] = client.post("/api/experiments", headers=h, json={"title": "Saved meanwhile", "notebook_id": nb["id"]}).status_code
        out["took"] = time.monotonic() - t
    th = threading.Thread(target=save)
    th.start()
    th.join(timeout=2.5)
    finished_in_time = not th.is_alive()
    during()
    th.join()
    return finished_in_time, out


# ------------------------------------------------------------------ nobody waits for an import

def test_saves_go_through_while_a_big_file_is_copied_in(client, users, notebook, watch, monkeypatch):
    """Regression: the scan held the database's write lock while copying the next arrival in, so colleagues'
    saves waited and then failed with "database is locked"."""
    h = users["alice"]
    exp = new_exp(client, h, notebook)
    d, _ = watch()
    (d / f"{exp['eid']} first.csv").write_text("1")
    (d / f"{exp['eid']} second.csv").write_text("2")
    scan(10)
    real, seen = instruments.pack, {}

    def slow_pack(rel, p, kind):
        if rel.endswith("second.csv"):  # the first arrival is already attached; this copy "takes a while"
            seen["in_time"], seen["save"] = colleague_saves_while(client, h, notebook, lambda: None)
        return real(rel, p, kind)
    monkeypatch.setattr(instruments, "pack", slow_pack)
    scan(300)
    assert seen["in_time"] and seen["save"]["status"] == 201, seen
    assert len(atts(client, h, exp)) == 2


def test_a_first_look_at_a_big_folder_doesnt_hold_the_database(client, users, notebook, watch, monkeypatch):
    """Regression: noting thousands of files already in a new folder was one long transaction."""
    d, _ = watch()
    for i in range(1200):
        (d / f"old {i}.csv").write_text(str(i))
    real_discover, seen = instruments.discover, {}

    def discover(root, kind, problems=None):
        for n, item in enumerate(real_discover(root, kind, problems)):
            if n == 900:
                seen["in_time"], seen["save"] = colleague_saves_while(client, users["alice"], notebook, lambda: None)
            yield item
    with Session(engine) as s:
        inst = s.exec(select(instruments.Instrument).where(instruments.Instrument.folder == str(d))).one()
        inst.baseline_at = None  # look again as if just added
        s.add(inst)
        s.commit()
    monkeypatch.setattr(instruments, "discover", discover)
    scan(5)
    assert seen["in_time"] and seen["save"]["status"] == 201, seen


# ------------------------------------------------------------------ files that change, move or repeat

def test_a_file_that_grows_while_being_copied_is_taken_later_in_full(client, users, notebook, watch, monkeypatch):
    """Regression: a run that started writing again after its quiet time was attached half-finished."""
    h = users["alice"]
    exp = new_exp(client, h, notebook)
    d, _ = watch()
    f = d / f"{exp['eid']} run.ms"
    f.write_bytes(b"first half ")
    scan(10)
    real = instruments.pack

    def pack_while_it_grows(rel, p, kind):
        out = real(rel, p, kind)
        with open(f, "ab") as fh:
            fh.write(b"second half")
        os.utime(f, (time.time() + 5, time.time() + 5))
        return out
    monkeypatch.setattr(instruments, "pack", pack_while_it_grows)
    scan(300)
    monkeypatch.setattr(instruments, "pack", real)
    assert atts(client, h, exp) == [], "the half-finished copy was attached"
    scan(400)
    scan(700)
    a = atts(client, h, exp)
    assert len(a) == 1 and client.get(f"/api/attachments/{a[0]['id']}", headers=h).content == b"first half second half"


def test_a_renamed_file_isnt_attached_twice(client, users, notebook, watch):
    """Regression: renaming a file in the watched folder (or copying it in again) attached it a second time."""
    h = users["alice"]
    exp = new_exp(client, h, notebook)
    d, _ = watch()
    (d / f"{exp['eid']} HPLC.pdf").write_bytes(b"%PDF the report")
    scan(10)
    scan(300)
    (d / f"{exp['eid']} HPLC.pdf").rename(d / f"{exp['eid']} HPLC final.pdf")
    scan(400)
    scan(700)
    a = atts(client, h, exp)
    assert len(a) == 1, [x["filename"] for x in a]
    with Session(engine) as s:
        row = s.exec(select(InstrumentFile).where(InstrumentFile.path == f"{exp['eid']} HPLC final.pdf")).one()
        assert row.state == "attached" and row.rule == "duplicate" and "already in" in row.note
        sec = s.exec(select(Entry).where(Entry.experiment_id == exp["id"], Entry.title == instruments.SECTION_TITLE)).one()
        assert sec.content["attachment_ids"] == [a[0]["id"]]


def test_a_file_removed_before_it_settled_isnt_left_arriving(client, users, admin, notebook, watch):
    d, iid = watch()
    (d / "mistake.csv").write_text("x")
    scan(10)
    (d / "mistake.csv").unlink()
    scan(300)
    info = next(i for i in client.get("/api/instruments", headers=admin).json()["instruments"] if i["id"] == iid)
    assert "settling" not in info["counts"] and info["counts"].get("vanished") == 1
    (d / "mistake.csv").write_text("back again")  # and if it comes back, it arrives normally
    scan(400)
    scan(700)
    assert any(i["path"] == "mistake.csv" for i in client.get("/api/inbox", headers=admin).json())


def test_a_whole_sequence_gives_one_notification_not_hundreds(client, users, notebook, watch):
    h = users["alice"]
    exp = new_exp(client, h, notebook)
    client.post("/api/notifications/read", headers=h, json={"all": True})
    d, _ = watch(kind="chromatography")
    for k in range(25):
        run = d / f"{exp['eid']}-inj{k:02d}.D"
        run.mkdir()
        (run / "data.ms").write_bytes(os.urandom(64))
    scan(10)
    scan(400)
    assert len(atts(client, h, exp)) == 25
    mine = [n for n in client.get("/api/notifications", headers=h).json()["items"]
            if n["kind"] == "instrument" and exp["eid"] in n["text"]]
    assert len(mine) == 1 and "25 instrument runs" in mine[0]["text"], [n["text"] for n in mine]


# ------------------------------------------------------------------ problems an admin is told about

def test_a_full_disk_during_an_import_is_reported_and_clears(client, users, admin, notebook, watch, monkeypatch):
    """Regression: the arrival silently stayed 'arriving' and the Admin page said nothing."""
    h = users["alice"]
    exp = new_exp(client, h, notebook)
    d, iid = watch()
    (d / f"{exp['eid']}.csv").write_text("x")
    scan(10)
    real = instruments._store

    def full(tmp):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(instruments, "_store", full)
    scan(300)

    def err():
        return next(i for i in client.get("/api/instruments", headers=admin).json()["instruments"] if i["id"] == iid)["last_error"]
    assert "disk" in err() and "full" in err()
    assert not list((settings.data_dir / ".incoming").glob("*.part")), "a half-written copy was left behind"
    monkeypatch.setattr(instruments, "_store", real)
    scan(360)
    assert err() == "" and len(atts(client, h, exp)) == 1


def test_a_failed_backup_says_why_until_one_works(client, tmp_path, monkeypatch):
    """Regression: a backup that failed (a full disk) only showed up days later as 'the newest backup is old'."""
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(settings, "backup_interval_hours", 24.0)
    real = backup._fingerprint

    def full(db):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(backup, "_fingerprint", full)
    with pytest.raises(OSError):
        backup.maybe_backup()
    assert any("last backup failed" in w and "disk is full" in w for w in backup.health()["warnings"])
    monkeypatch.setattr(backup, "_fingerprint", real)
    assert backup.maybe_backup() is not None
    assert not any("failed" in w for w in backup.health()["warnings"])


def test_leftovers_from_interruptions_are_cleared_after_a_day(client, tmp_path, monkeypatch):
    """Regression: a backup or import cut short (Benchtop stopped, the Mac off) left its pieces forever."""
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "backups")
    settings.backup_dir.mkdir()
    old = time.time() - 3 * 86400
    stale = [settings.backup_dir / ".snapshot-1-2-3.db", settings.backup_dir / ".benchtop-backup-x.zip.part",
             settings.data_dir / ".incoming" / "1-2-3.part"]
    (settings.data_dir / ".incoming").mkdir(exist_ok=True)
    restore_dir = settings.data_dir / ".restore-20000101-000000-1-2"
    restore_dir.mkdir()
    (restore_dir / "x").write_text("x")
    fresh = settings.backup_dir / ".snapshot-9-9-9.db"  # a backup that's running right now
    for p in [*stale, fresh]:
        p.write_text("x")
    for p in [*stale, restore_dir]:
        os.utime(p, (old, old))
    backup.prune()
    instruments.scan_all(now=T0)
    assert not any(p.exists() for p in [*stale, restore_dir])
    assert fresh.exists()


# ------------------------------------------------------------------ sample-code initials

def test_two_people_cant_claim_the_same_initials_at_once(client, users):
    """Regression: two people choosing the same initials at the same moment both got them."""
    gate, out = threading.Barrier(2), {}

    def go(u):
        gate.wait()
        out[u] = client.put("/api/auth/me/sample-prefix", headers=users[u], json={"prefix": "QZ"}).status_code
    ts = [threading.Thread(target=go, args=(u,)) for u in ("alice", "bob")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    try:
        assert sorted(out.values()) == [200, 409], out
    finally:
        for u in ("alice", "bob"):
            client.put("/api/auth/me/sample-prefix", headers=users[u], json={"prefix": None})


# ------------------------------------------------------------------ who may do what

@pytest.fixture(scope="module")
def cast(client, admin, users):
    """alice owns a private notebook in group G (bob is a member, gus leads it); dana may edit and eve may view
    alice's experiments by share; carl is in no group; vic has a read-only account."""
    def person(u):
        client.post("/api/users", headers=admin, json={"username": u, "password": PW, "must_change": False,
                                                      "full_name": u.title(), "role": "scientist"})
        return {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": u, "password": PW}).json()["token"]}
    people = {**users, **{u: person(u) for u in ("gus", "dana", "eve", "carl2")}}
    people["carl"] = people.pop("carl2")
    ids = {u["username"]: u["id"] for u in client.get("/api/users", headers=admin).json()}
    g = client.post("/api/groups", headers=admin, json={"name": "Matrix group G"}).json()
    for u, role in (("alice", "member"), ("bob", "member"), ("gus", "lead")):
        assert client.put(f"/api/groups/{g['id']}/members/{ids[u]}", headers=admin, json={"role": role}).status_code == 200
    nb = client.post("/api/notebooks", headers=people["alice"], json={"name": "Alice private (matrix)", "group_id": g["id"],
                                                                      "visibility": "private"}).json()
    return {"h": people, "ids": ids, "group": g, "nb": nb, "admin": admin}


def test_who_may_see_and_do_what(client, cast, tmp_path):
    h, ids, a = cast["h"], cast["ids"], cast["h"]["alice"]
    admin = cast["admin"]
    signed = new_exp(client, a, cast["nb"], "Matrix signed")
    draft = new_exp(client, a, cast["nb"], "Matrix draft")
    for e in (signed, draft):
        client.put(f"/api/experiments/{e['id']}/shares/{ids['dana']}", headers=a, json={"permission": "edit"})
        client.put(f"/api/experiments/{e['id']}/shares/{ids['eve']}", headers=a, json={"permission": "view"})
    assert client.post(f"/api/experiments/{signed['id']}/sign", headers=a, json={"password": PW}).status_code == 200
    assert client.put("/api/auth/me/sample-prefix", headers=a, json={"prefix": "QM"}).status_code == 200
    try:
        share = tmp_path / "matrix"
        share.mkdir()
        r = client.post("/api/instruments", headers=admin, json={"name": "Matrix", "kind": "files", "folder": str(share),
                                                                "audience": [cast["group"]["id"]]})
        iid = r.json()["id"]
        scan(0)
        (share / f"{signed['eid']} late HRMS.pdf").write_bytes(b"%PDF signed")
        (share / "unlabelled group file.csv").write_bytes(b"group")
        (share / "QM-999 unknown code.csv").write_bytes(b"code")
        (share / f"{draft['eid']} attached.csv").write_bytes(b"attached")
        scan(10)
        scan(300)
        items = {i["path"]: i["id"] for i in client.get("/api/inbox", headers=admin).json()}
        i_signed, i_group, i_code = (items[f"{signed['eid']} late HRMS.pdf"], items["unlabelled group file.csv"],
                                     items["QM-999 unknown code.csv"])
        att = atts(client, a, draft)[0]

        def sees(u, item):
            return client.get(f"/api/inbox/{item}/files/0", headers=h[u] if u != "admin" else admin).status_code == 200
        expect = {  # who sees each Inbox item (list and download agree)
            i_signed: {"alice", "gus", "dana", "eve", "admin"},  # readers of the signed record
            i_group: {"alice", "bob", "gus", "admin"},            # the folder's audience: group G
            i_code: {"alice", "admin"},                           # the code's owner
        }
        everyone = ["alice", "bob", "gus", "dana", "eve", "carl", "vic", "admin"]
        for item, allowed in expect.items():
            for u in everyone:
                listed = any(i["id"] == item for i in client.get("/api/inbox", headers=h[u] if u != "admin" else admin).json())
                assert sees(u, item) == (u in allowed) == listed, (item, u)
        # follow-ups of the signed record: anyone who can read it and write; not read-only accounts or outsiders
        # (read-only accounts are turned away from every change before anything is looked up: 403)
        for u, code in (("vic", 403), ("carl", 404), ("bob", 404)):
            assert client.post(f"/api/inbox/{i_signed}/follow-up", headers=h[u], json={}).status_code == code, u
            assert client.post(f"/api/experiments/{signed['id']}/follow-up", headers=h[u], json={}).status_code in (403, 404), u
        # assigning the group's file into alice's private draft: only people who may edit it
        # (eve isn't in group G, so for her the group's file doesn't exist: 404)
        for u, code in (("bob", 404), ("gus", 403), ("eve", 404), ("carl", 404), ("vic", 403)):
            assert client.post(f"/api/inbox/{i_group}/assign", headers=h[u], json={"experiment_id": draft["id"]}).status_code == code, u
        # "Not mine": people who may edit the draft
        for u, code in (("bob", 404), ("carl", 404), ("gus", 403), ("eve", 403), ("vic", 403)):
            assert client.post(f"/api/attachments/{att['id']}/not-mine", headers=h[u]).status_code == code, u
        # sample codes on alice's draft: not for those who can't edit it
        for u, code in (("carl", 404), ("eve", 403), ("gus", 403), ("vic", 403)):
            assert client.post(f"/api/experiments/{draft['id']}/samples", headers=h[u], json={}).status_code == code, u
        # instrument settings: admins only
        for u in ("alice", "gus", "vic"):
            assert client.get("/api/instruments", headers=h[u]).status_code == 403
            assert client.post("/api/instruments/scan", headers=h[u]).status_code == 403
            assert client.post("/api/instruments/check", headers=h[u], json={"folder": str(share)}).status_code == 403
            assert client.patch(f"/api/instruments/{iid}", headers=h[u], json={"enabled": False}).status_code == 403
        # and the people allowed can: dana (edit share) sends it back; alice assigns the group file
        assert client.post(f"/api/attachments/{att['id']}/not-mine", headers=h["dana"]).status_code == 200
        assert client.post(f"/api/inbox/{i_group}/assign", headers=a, json={"experiment_id": draft["id"]}).status_code == 200
        r = client.post(f"/api/inbox/{i_signed}/follow-up", headers=h["eve"], json={"notebook_id":
                        client.post("/api/notebooks", headers=h["eve"], json={"name": "Eve's"}).json()["id"]})
        assert r.status_code == 201, r.text
        client.delete(f"/api/instruments/{iid}", headers=admin)
    finally:
        client.put("/api/auth/me/sample-prefix", headers=a, json={"prefix": None})
