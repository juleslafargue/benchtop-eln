"""0.36.4: instrument folders that showed people what they shouldn't see (found by the 0.36.2 stress test).

- An arrival over the size limit is matched like any other before the limit is applied: it isn't copied, but only
  the people it would have gone to see it (with why), and the record's owner is told. It was shown to the folder's
  whole audience, whatever private record its name pointed to, and hidden from that record's owner.
- A watched folder that has been replaced by a link (to Benchtop's data folder, /etc…) isn't read: it copied
  benchtop.db into the Inbox. Benchtop's data, backup and second-copy folders can't be watched, or be inside one.
- An Inbox note that names several records names only those the reader can open.
- Two "Not mine" clicks at once send the file back once (one audit event)."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import instruments
from app.config import settings

PW = "lab-bench-pw-2026"
T0 = datetime(2031, 6, 1, 9, 0, 0)


def scan(seconds: float):
    return instruments.scan_all(now=T0 + timedelta(seconds=seconds))


def _person(client, admin, username):
    client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                   "full_name": username.title(), "role": "scientist"})
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def lab(client, admin):
    return {u: _person(client, admin, u) for u in ("ins_owner", "ins_other")}


@pytest.fixture()
def add(client, admin):
    made = []

    def _add(folder: Path, **kw):
        r = client.post("/api/instruments", headers=admin, json={"name": kw.pop("name", "Size test"), "kind": "files",
                                                                  "folder": str(folder), **kw})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()
    yield _add
    for i in made:
        client.delete(f"/api/instruments/{i}", headers=admin)


def _exp(client, h, title, visibility="private"):
    nb = client.post("/api/notebooks", headers=h, json={"name": f"NB {title}", "visibility": visibility}).json()
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def _inbox(client, h):
    return {i["path"]: i for i in client.get("/api/inbox", headers=h, params={"limit": 500}).json()}


def _notes(client, h):
    return [n["text"] for n in client.get("/api/notifications", headers=h).json()["items"]]


def test_an_arrival_too_big_to_copy_goes_only_to_who_it_was_for(client, admin, lab, tmp_path, add, monkeypatch):
    monkeypatch.setattr(settings, "instrument_max_mb", 1)
    owner, other = lab["ins_owner"], lab["ins_other"]
    exp = _exp(client, owner, "Private run")
    folder = tmp_path / "big"
    folder.mkdir()
    add(folder)
    scan(0)
    name = f"{exp['eid']} PRIVATE-NAME big.raw"
    (folder / name).write_bytes(b"x" * (1024 * 1024 + 1))
    (folder / "nobody's small.csv").write_text("1,2\n")
    scan(10)
    scan(400)
    assert name not in _inbox(client, other), "someone who can't open the record sees its file"
    mine = _inbox(client, owner)
    assert name in mine, "the record's owner doesn't see the file their record's number is on"
    assert "limit" in mine[name]["note"] and exp["eid"] in mine[name]["note"]
    assert [m["eid"] for m in mine[name]["matched"]] == [exp["eid"]]
    assert "nobody's small.csv" in _inbox(client, other)  # an unmatched file still goes to everyone
    assert any(name in t and "too big" in t for t in _notes(client, owner)), "the owner wasn't told"
    assert not any("PRIVATE-NAME" in t for t in _notes(client, other))
    assert client.post(f"/api/inbox/{mine[name]['id']}/ignore", headers=other).status_code == 404  # can't set it aside
    assert not client.get(f"/api/experiments/{exp['id']}", headers=owner).json()["attachments"]  # still not copied


def test_a_watched_folder_replaced_by_a_link_isnt_read(client, admin, tmp_path, add):
    folder = tmp_path / "share" / "nmr"
    folder.mkdir(parents=True)
    inst = add(folder)
    scan(0)
    os.rmdir(folder)
    os.symlink(settings.data_dir, folder)  # someone with write access to the share points it at Benchtop's data
    scan(60)
    res = scan(400)
    assert "link" in res[inst["id"]].get("error", ""), res
    assert not any("benchtop.db" in p for p in _inbox(client, admin))
    shown = next(i for i in client.get("/api/instruments", headers=admin).json()["instruments"] if i["id"] == inst["id"])
    assert "link" in shown["last_error"]
    assert client.get(f"/api/instruments/{inst['id']}/old", headers=admin).status_code == 409
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "passwd.csv").write_text("secret\n")
    os.unlink(folder)
    os.symlink(other, folder)  # or anywhere else
    scan(500)
    scan(900)
    assert "passwd.csv" not in _inbox(client, admin)
    os.unlink(folder)
    folder.mkdir()  # put back as it was: read again
    (folder / "after.csv").write_text("1\n")
    scan(1000)
    scan(1400)
    assert "error" not in scan(1500)[inst["id"]]


def test_benchtops_own_folders_cant_be_watched(client, admin, tmp_path, monkeypatch):
    backups = tmp_path / "lab-share" / "benchtop-backups"
    backups.mkdir(parents=True)
    monkeypatch.setattr(settings, "backup_dir", backups)
    for f in (backups, tmp_path / "lab-share", settings.data_dir):
        r = client.post("/api/instruments", headers=admin, json={"name": "Nope", "kind": "files", "folder": str(f)})
        assert r.status_code == 400 and "outside Benchtop" in r.json()["detail"], (f, r.text)
    with pytest.raises(instruments.FolderRefused):
        instruments.watched_root(type("I", (), {"folder": str(backups)})())


def test_a_note_naming_several_records_names_only_those_the_reader_can_open(client, admin, lab, tmp_path, add):
    from sqlmodel import Session

    from app.db import engine
    from app.models import InstrumentFile
    owner, other = lab["ins_owner"], lab["ins_other"]
    shared = _exp(client, owner, "Shared run", visibility="everyone")
    private = _exp(client, owner, "Private run 2")
    note = f"The name points to more than one experiment (JL-013 ({shared['eid']}), TQ-007 ({private['eid']})), so Benchtop didn't choose."
    (tmp_path / "multi").mkdir()
    iid = add(tmp_path / "multi")["id"]
    with Session(engine) as s:
        row = InstrumentFile(instrument_id=iid, path="JL-013 TQ-007 run.csv", fingerprint="x", size=1, state="inbox",
                             first_seen_at=T0, changed_at=T0, note=note, matched_ids=[shared["id"], private["id"]])
        s.add(row)
        s.commit()
        rid = row.id
    try:
        seen = _inbox(client, other)["JL-013 TQ-007 run.csv"]
        assert private["eid"] not in seen["note"] and shared["eid"] in seen["note"], seen["note"]
        assert [m["eid"] for m in seen["matched"]] == [shared["eid"]]
        assert _inbox(client, owner)["JL-013 TQ-007 run.csv"]["note"] == note  # the owner can open both
    finally:
        with Session(engine) as s:
            s.delete(s.get(InstrumentFile, rid))
            s.commit()


def test_not_mine_clicked_twice_at_once_sends_it_back_once(client, admin, lab, tmp_path, add, monkeypatch):
    owner = lab["ins_owner"]
    exp = _exp(client, owner, "Not mine twice", visibility="everyone")
    folder = tmp_path / "nm"
    folder.mkdir()
    add(folder)
    scan(0)
    (folder / f"{exp['eid']} typo.csv").write_text("not mine\n")
    scan(10)
    scan(400)
    att = client.get(f"/api/experiments/{exp['id']}", headers=owner).json()["attachments"][0]
    from app import audit
    real = audit._take_write_lock
    other: dict = {}

    def lock(session):  # the second click arrives while the first is on its way to the lock
        if not other:
            other["t"] = t = threading.Thread(target=lambda: other.__setitem__(
                "r", client.post(f"/api/attachments/{att['id']}/not-mine", headers=owner).status_code))
            t.start()
            t.join(1.0)
        return real(session)
    monkeypatch.setattr(audit, "_take_write_lock", lock)
    first = client.post(f"/api/attachments/{att['id']}/not-mine", headers=owner).status_code
    other["t"].join(60)
    monkeypatch.undo()
    assert sorted([first, other["r"]]) == [200, 400], (first, other["r"])
    events = [e for e in client.get("/api/audit", headers=admin, params={"limit": 500}).json()
              if e["action"] == "not_mine" and e["object_id"] == exp["id"]]
    assert len(events) == 1, events
