"""0.25: free disk space. Admins are warned early; the last of it is kept for saving; a full disk is said plainly.

- the admin's dashboard and Admin → Backups say when a disk Benchtop uses is low, and what to do; nothing when there's
  room, nothing for scientists, nothing when BENCHTOP_DISK_WARN_GB=0
- the backups' disk is named on its own when it's a different one, with its own advice
- an upload, an instrument file or an .eln import that would leave less than 1 GB free is refused before anything is
  written (the instrument file stays in its folder and arrives once there's room)
- a backup that wouldn't fit is refused before it starts, and the dashboard says why
- regressions: a disk that fills up during an upload, or while an entry is saved, gave "Internal Server Error"
"""
from __future__ import annotations

import errno
import io
import os
import shutil
import zipfile
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event

from app import storage
from app.config import settings
from app.db import engine

PW = "lab-bench-pw-2026"
GB = 10**9
Usage = namedtuple("Usage", "total used free")


@pytest.fixture()
def disk(monkeypatch):
    """Pretend the disks have this much free space: {"data": bytes, "backups": bytes} (backups default to data)."""
    from app import diskspace
    free = {"data": 200 * GB}
    real_usage = shutil.disk_usage

    def usage(p):
        real_usage(p)  # still fails like the real one for a folder that isn't there
        p, back = Path(p).resolve(), settings.backup_dir.resolve()
        n = free["backups"] if "backups" in free and (p == back or back in p.parents) else free["data"]
        return Usage(500 * GB, 500 * GB - n, n)

    monkeypatch.setattr(diskspace.shutil, "disk_usage", usage)
    monkeypatch.setattr(settings, "disk_warn_gb", 5.0)
    return free


def dash(client, h) -> list[str]:
    return client.get("/api/dashboard", headers=h).json()["system"].get("disk_warnings", [])


@pytest.fixture()
def exp(client, users):
    h = users["alice"]
    nb = client.post("/api/notebooks", headers=h, json={"name": "Disk space"}).json()
    return client.post("/api/experiments", headers=h, json={"title": "Big files", "notebook_id": nb["id"]}).json()


def leftovers() -> list[Path]:
    return list((settings.data_dir / ".incoming").glob("*.part")) if (settings.data_dir / ".incoming").is_dir() else []


# ------------------------------------------------------------------ admins are warned early, and told what to do

def test_plenty_of_room_shows_nothing(client, admin, disk):
    disk["data"] = 80 * GB
    assert dash(client, admin) == []
    d = client.get("/api/admin/backups", headers=admin).json()["disk"]
    assert d["warnings"] == [] and d["places"][0]["free"] == 80 * GB
    assert d["places"][0]["label"] == "Benchtop's data and the backups"


def test_a_low_disk_is_on_the_admins_dashboard_with_what_to_do(client, admin, users, disk):
    disk["data"] = int(3.2 * GB)
    (w,) = dash(client, admin)
    assert w.startswith("Only 3.2 GB free on the disk with Benchtop's data and the backups")
    assert str(settings.data_dir) in w and "saving, uploads and backups fail" in w and "Delete files you no longer need" in w
    assert "refused" not in w, "uploads still work at 3.2 GB"
    assert dash(client, users["alice"]) == [], "scientists don't get the admin's warnings"
    assert client.get("/api/admin/backups", headers=admin).json()["disk"]["warnings"] == [w]


def test_nearly_full_says_uploads_are_refused(client, admin, disk):
    disk["data"] = 400 * 10**6
    (w,) = dash(client, admin)
    assert w.startswith("Only 400 MB free") and "Until there's 1.0 GB free, new uploads and instrument files are refused" in w


def test_turned_off_by_the_setting(client, admin, users, exp, disk, monkeypatch):
    monkeypatch.setattr(settings, "disk_warn_gb", 0.0)
    disk["data"] = 100 * 10**6
    assert dash(client, admin) == []
    r = client.post(f"/api/experiments/{exp['id']}/attachments", headers=users["alice"], files={"file": ("a.csv", b"1,2\n", "text/csv")})
    assert r.status_code == 201, "with the setting at 0 nothing is refused either"


def test_a_backup_disk_of_its_own_is_named_on_its_own(client, admin, disk, monkeypatch, tmp_path):
    from app import diskspace
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "backups")
    settings.backup_dir.mkdir()
    real = diskspace._device
    monkeypatch.setattr(diskspace, "_device", lambda p: 999 if Path(p) == settings.backup_dir else real(p))
    disk.update(data=50 * GB, backups=2 * GB)
    (w,) = dash(client, admin)
    assert w.startswith("Only 2.0 GB free on the disk with the backups") and "backups fail" in w and "BENCHTOP_BACKUP_DIR" in w
    places = client.get("/api/admin/backups", headers=admin).json()["disk"]["places"]
    assert [p["label"] for p in places] == ["Benchtop's data", "The backups"]


def test_a_big_database_needs_room_for_its_backup(client, admin, disk, monkeypatch):
    from app import diskspace
    monkeypatch.setattr(diskspace, "database_bytes", lambda: 4 * GB)
    disk["data"] = 7 * GB  # above 5 GB, but a backup takes the database's size twice over
    assert len(dash(client, admin)) == 1


# ------------------------------------------------------------------ the last of it is kept for saving

def test_an_upload_that_would_nearly_fill_the_disk_is_refused_plainly(client, users, exp, disk):
    h = users["alice"]
    before = client.get(f"/api/experiments/{exp['id']}", headers=h).json()
    disk["data"] = int(1.0005 * GB)
    r = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("big.raw", os.urandom(1_000_000), "application/octet-stream")})
    assert r.status_code == 507, r.text
    assert "wasn't saved" in r.json()["detail"] and "nearly full" in r.json()["detail"] and "Benchtop admin" in r.json()["detail"]
    after = client.get(f"/api/experiments/{exp['id']}", headers=h).json()
    assert after["attachments"] == before["attachments"] and after["updated_at"] == before["updated_at"], "nothing was added"
    assert not leftovers()
    disk["data"] = 2 * GB  # a small file still fits
    assert client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("small.csv", b"1,2\n", "text/csv")}).status_code == 201


def test_regression_a_disk_that_fills_during_an_upload_is_said_plainly(client, users, exp, monkeypatch):
    """Regression (before 0.25): "Internal Server Error", with no hint that the disk was full."""
    h = users["alice"]
    real_open = open

    def full_open(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        if str(path).endswith(".part") and "w" in mode:
            def write(_):
                raise OSError(errno.ENOSPC, "No space left on device")
            f.write = write
        return f

    monkeypatch.setattr("builtins.open", full_open)
    r = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("nmr.zip", b"PK" + os.urandom(5000), "application/zip")})
    monkeypatch.undo()
    assert r.status_code == 507, r.text
    assert "disk" in r.json()["detail"] and "full" in r.json()["detail"] and "Benchtop admin" in r.json()["detail"]
    assert client.get(f"/api/experiments/{exp['id']}", headers=h).json()["attachments"] == []
    assert not leftovers()


@pytest.fixture()
def capped_database():
    """Call it, and SQLite acts as if the disk were full: the database can't grow any more (a real "database or disk
    is full", not a pretend one)."""
    def cap(dbapi_con, _):
        n = dbapi_con.execute("PRAGMA page_count").fetchone()[0]
        dbapi_con.execute(f"PRAGMA max_page_count = {n}")

    def start():
        engine.dispose()
        event.listen(engine, "connect", cap)

    yield start
    if event.contains(engine, "connect", cap):
        event.remove(engine, "connect", cap)
    engine.dispose()


def test_regression_saving_an_entry_on_a_full_disk_says_so(client, users, exp, capped_database):
    """Regression (before 0.25): "Internal Server Error" on the section, with no hint why."""
    h = users["alice"]
    e = client.post(f"/api/experiments/{exp['id']}/entries", headers=h, json={"kind": "text", "title": "Notes", "content": {"html": "<p>x</p>"}}).json()
    capped_database()
    big = "<p>" + os.urandom(150_000).hex() + "</p>"
    r = client.put(f"/api/entries/{e['id']}", headers=h, json={"title": "Notes", "content": {"html": big}, "expected_version": e["version"]})
    assert r.status_code == 507, r.text
    msg = r.json()["detail"]
    assert msg.startswith("Not saved: the disk Benchtop keeps its data on is full") and "still on this page" in msg


def test_other_server_errors_are_still_server_errors(client, users, exp, monkeypatch):
    def broken(*a, **kw):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(storage, "save", broken)
    with pytest.raises(OSError):  # the test client raises what a real server logs and answers with a 500
        client.post(f"/api/experiments/{exp['id']}/attachments", headers=users["alice"], files={"file": ("a.csv", b"1\n", "text/csv")})


def test_an_eln_import_that_wouldnt_fit_is_refused(client, users, disk):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/ro-crate-metadata.json", "{}")
        z.writestr("x/big.bin", os.urandom(200_000))
    disk["data"] = GB + 100_000
    r = client.post("/api/eln/preview", headers=users["alice"], files={"file": ("lab.eln", buf.getvalue(), "application/zip")})
    assert r.status_code == 507, r.text
    assert r.json()["detail"].startswith("The .eln file wasn't saved: the disk Benchtop keeps its data on is nearly full")


def test_an_instrument_file_waits_in_its_folder_until_theres_room(client, admin, users, exp, disk, tmp_path):
    from app import instruments
    folder = tmp_path / "hplc"
    folder.mkdir()
    r = client.post("/api/instruments", headers=admin, json={"name": "HPLC (disk test)", "kind": "files", "folder": str(folder)})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    t0 = datetime(2031, 1, 1, 9, 0)
    try:
        instruments.scan_all(now=t0)
        (folder / f"{exp['eid']} run.csv").write_bytes(os.urandom(300_000))
        instruments.scan_all(now=t0 + timedelta(seconds=60))
        disk["data"] = GB + 100_000
        instruments.scan_all(now=t0 + timedelta(seconds=400))
        err = next(i for i in client.get("/api/instruments", headers=admin).json()["instruments"] if i["id"] == iid)["last_error"]
        assert "nearly full" in err and "tries again every minute" in err
        assert client.get(f"/api/experiments/{exp['id']}", headers=users["alice"]).json()["attachments"] == []
        assert (folder / f"{exp['eid']} run.csv").exists() and not leftovers()
        disk["data"] = 50 * GB
        instruments.scan_all(now=t0 + timedelta(seconds=500))
        assert len(client.get(f"/api/experiments/{exp['id']}", headers=users["alice"]).json()["attachments"]) == 1
    finally:
        client.delete(f"/api/instruments/{iid}", headers=admin)


def test_a_backup_that_wouldnt_fit_is_refused_before_it_starts(client, admin, disk, monkeypatch, tmp_path):
    from app import backup
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(settings, "backup_interval_hours", 24.0)
    disk["data"] = GB + 1000
    with pytest.raises(backup.BackupError, match="there isn't room for a backup"):
        backup.create_backup("manual")
    assert not list((tmp_path / "backups").rglob("*.zip*")) and not list((tmp_path / "backups").rglob(".snapshot-*")), "nothing was written"
    (w,) = [w for w in backup.health()["warnings"] if "last backup failed" in w]
    assert "Free some space on that disk" in w and "Nothing was written" in w
    assert any("last backup failed" in w for w in client.get("/api/dashboard", headers=admin).json()["system"]["backup_warnings"])
    disk["data"] = 50 * GB
    assert backup.create_backup("manual")["name"].endswith(".zip")
    assert not any("failed" in w for w in backup.health()["warnings"])


# ------------------------------------------------------------------ email: once when it runs low, once if nearly full

@pytest.fixture()
def outbox(monkeypatch, client, admin):
    from app import diskspace, mailer, sitesettings
    sent = []
    monkeypatch.setattr(mailer, "synchronous", True)
    monkeypatch.setattr(mailer, "transport", lambda msg, cfg: sent.append(msg))
    sitesettings.update("email", {"host": "smtp.example.org", "from_address": "eln@example.org", "password": "s3cret-app-pw"})
    client.patch("/api/auth/me", headers=admin, json={"email": "admin@example.org"})
    (settings.data_dir / diskspace.STATE).unlink(missing_ok=True)
    yield sent
    (settings.data_dir / diskspace.STATE).unlink(missing_ok=True)
    client.patch("/api/auth/me", headers=admin, json={"email": "", "preferences": {"email_notify": {"system": True}}})
    sitesettings.update("email", {"host": "", "from_address": ""})


def body(msg) -> str:
    return msg.get_body(("plain",)).get_content()


def test_admins_get_one_email_when_a_disk_runs_low_and_one_if_nearly_full(client, admin, disk, outbox, monkeypatch):
    from app import diskspace
    disk["data"] = 50 * GB
    diskspace.email_if_low()
    assert outbox == [], "plenty of room: nothing to say"
    disk["data"] = 3 * GB
    diskspace.email_if_low()
    assert len(outbox) == 1 and outbox[0]["To"] == "admin@example.org"
    assert outbox[0]["Subject"] == "Benchtop's disk is running low on space"
    assert "Only 3.0 GB free on the disk with Benchtop's data" in body(outbox[0]) and "/admin" in body(outbox[0])
    disk["data"] = int(2.5 * GB)
    diskspace.email_if_low()
    assert len(outbox) == 1, "still low, an hour later: no second email"
    disk["data"] = 500 * 10**6
    diskspace.email_if_low()
    assert len(outbox) == 2 and outbox[1]["Subject"] == "Benchtop's disk is nearly full"
    assert "new uploads and instrument files are refused" in body(outbox[1])
    disk["data"] = 3 * GB
    diskspace.email_if_low()
    assert len(outbox) == 2, "a little better again: nothing to say"
    disk["data"] = 50 * GB
    diskspace.email_if_low()
    disk["data"] = 3 * GB
    diskspace.email_if_low()
    assert len(outbox) == 3, "low again after there was room: told again"


def test_no_disk_email_when_turned_off_in_the_admins_settings_or_in_the_demo(client, admin, disk, outbox, monkeypatch):
    from app import diskspace
    r = client.patch("/api/auth/me", headers=admin, json={"preferences": {"email_notify": {"system": False}}})
    assert r.json()["preferences"]["email_notify"]["system"] is False, "the setting is kept"
    disk["data"] = 3 * GB
    diskspace.email_if_low()
    assert outbox == []
    client.patch("/api/auth/me", headers=admin, json={"preferences": {"email_notify": {"system": True}}})
    monkeypatch.setattr(settings, "demo", True)  # the demo lab never emails
    diskspace.email_if_low()
    assert outbox == []
    monkeypatch.setattr(settings, "demo", False)
    monkeypatch.setattr(settings, "disk_warn_gb", 0.0)  # warnings off: no email either
    diskspace.email_if_low()
    assert outbox == []
