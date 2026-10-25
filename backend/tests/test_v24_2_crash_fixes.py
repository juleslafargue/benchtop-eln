"""0.24.2: what the pull-the-plug tests found (tests/crash_test.py kills Benchtop at random moments, then starts it again).

- Two uploads at the same moment froze the whole server for 30 s, and then one failed: the upload held the
  database's write lock while it waited for its file, on the thread every request goes through.
- An upload cut off part-way left half a file in the store; uploading the same file again was skipped as "already
  there", so the record pointed at the broken file for good. Files are now written in full, then renamed into place.
- A restore cut off part-way left no database at all: Benchtop then started on an empty lab. Now the next start
  finishes the restore.
- An update cut off while it swapped the program files left a mix of two versions, and ./benchtop update then said
  "You have the newest version". Now Benchtop won't start on the mix, and says to run ./benchtop rollback.
- Stopped at the wrong moment of its very first start, Benchtop kept an empty key for signing sign-ins.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from tests.test_v19_updates import needs_tools, venv, version_of, zip_lab  # noqa: F401 (fixtures)
from tests.test_v24_upgrades import BACKEND, env_for, server

PW = "crash-test-pw-2026"


# ------------------------------------------------------------------ uploads

def _setup(base: str) -> tuple[dict, int]:
    r = httpx.post(base + "/api/setup", json={"full_name": "Hannah Weiss", "username": "hweiss", "password": PW,
                                              "load_demo": False}, timeout=30)
    assert r.status_code == 201, r.text
    h = {"Authorization": f"Bearer {httpx.post(base + '/api/auth/login', json={'username': 'hweiss', 'password': PW}).json()['token']}"}
    nb = httpx.post(base + "/api/notebooks", headers=h, json={"name": "Uploads"}).json()
    exp = httpx.post(base + "/api/experiments", headers=h, json={"title": "Many files", "notebook_id": nb["id"]}).json()
    return h, exp["id"]


def test_uploads_at_the_same_moment_dont_freeze_the_server(tmp_path):
    with server(tmp_path / "data") as (base, log):
        h, exp_id = _setup(base)
        results: list[tuple[int, float]] = []

        def upload(i: int) -> None:
            with httpx.Client(base_url=base, headers=h, timeout=120) as c:
                for j in range(3):
                    t = time.monotonic()
                    r = c.post(f"/api/experiments/{exp_id}/attachments",
                               files={"file": (f"{i}-{j}.bin", os.urandom(3_000_000))})
                    results.append((r.status_code, time.monotonic() - t))

        started = time.monotonic()
        threads = [threading.Thread(target=upload, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        took = time.monotonic() - started
        assert [s for s, _ in results] == [201] * 24, results
        assert took < 20, f"24 uploads took {took:.0f} s (the slowest {max(d for _, d in results):.0f} s): the server froze"
        assert "database is locked" not in log.read_text()


def test_endpoints_that_use_the_database_dont_run_on_the_shared_thread():
    """An `async` endpoint runs on the one thread every request goes through: waiting there for the database freezes
    everyone. Endpoints with a database session are plain functions (FastAPI runs those on worker threads)."""
    import importlib
    import pkgutil

    import app.routers
    from fastapi.routing import APIRoute

    routes = [r for m in pkgutil.iter_modules(app.routers.__path__)
              for r in getattr(importlib.import_module(f"app.routers.{m.name}"), "router", None).routes
              if isinstance(r, APIRoute)]
    assert len(routes) > 100
    bad = [f"{r.path} ({r.endpoint.__name__})" for r in routes
           if inspect.iscoroutinefunction(r.endpoint) and "session" in inspect.signature(r.endpoint).parameters]
    assert not bad, "async endpoints using the database: " + ", ".join(bad)


def test_half_a_file_left_by_a_crash_is_replaced_when_the_file_is_uploaded_again(client, users, notebook):
    from app.config import settings

    h = users["alice"]
    exp = client.post("/api/experiments", headers=h, json={"title": "Cut-off upload", "notebook_id": notebook["id"]}).json()
    data = os.urandom(400_000)
    sha = hashlib.sha256(data).hexdigest()
    half = settings.attachments_dir / sha[:2] / sha  # what Benchtop killed mid-upload left before 0.24.2
    half.parent.mkdir(parents=True, exist_ok=True)
    half.write_bytes(data[:150_000])
    r = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("spectrum.jdx", data)})
    assert r.status_code == 201, r.text
    got = client.get(f"/api/attachments/{r.json()['id']}", headers=h)
    assert got.status_code == 200 and got.content == data, got.status_code
    assert hashlib.sha256(half.read_bytes()).hexdigest() == sha


def test_the_instrument_store_replaces_a_broken_copy_too(tmp_path):
    from app import instruments

    data = os.urandom(200_000)
    sha = hashlib.sha256(data).hexdigest()
    dst = instruments.blob_path(sha)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data[:1000])
    tmp = instruments._tmp_name()
    tmp.write_bytes(data)
    assert instruments._store(tmp) == (sha, len(data))
    assert dst.read_bytes() == data and not tmp.exists()
    tmp = instruments._tmp_name()  # an intact copy is kept as it is
    tmp.write_bytes(data)
    inode = dst.stat().st_ino
    instruments._store(tmp)
    assert dst.stat().st_ino == inode and not tmp.exists()


# ------------------------------------------------------------------ restore

LAB = """
import os, sys
from fastapi.testclient import TestClient
from app.main import app
from app import backup
PW = "crash-test-pw-2026"
with TestClient(app) as c:
    c.post("/api/setup", json={"full_name": "Hannah Weiss", "username": "hweiss", "password": PW, "load_demo": False})
    h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"username": "hweiss", "password": PW}).json()["token"]}
    nb = c.post("/api/notebooks", headers=h, json={"name": "NB"}).json()
    def exp(title, n):
        e = c.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]}).json()
        c.post(f"/api/experiments/{e['id']}/attachments", headers=h, files={"file": ("f.bin", os.urandom(n))})
    exp("In the backup", 300_000)
    print(backup.create_backup("manual")["path"])
    exp("After the backup", 500_000)
"""

CRASH_AT = """
import os, shutil, sys
n, count = int(sys.argv[2]), [0]
def crashing(fn):
    def wrapped(*a, **k):
        count[0] += 1
        if count[0] == n:
            os._exit(9)  # the power goes, right before this step
        return fn(*a, **k)
    return wrapped
os.replace, os.rename, shutil.move = crashing(os.replace), crashing(os.rename), crashing(shutil.move)
from app import backup
sys.exit(backup._main(["backup", "restore", sys.argv[1]]))
"""

AFTER = """
import json
from app.db import init_db
from app import check
init_db()  # Benchtop started again
print(json.dumps(check.check_live()["ok"]))
"""


def _titles(data: Path) -> list[str]:
    db = data / "benchtop.db"
    if not db.exists():
        return ["(no database)"]
    with sqlite3.connect(db) as c:
        try:
            return [r[0] for r in c.execute("SELECT title FROM experiment ORDER BY id")]
        except sqlite3.Error as exc:
            return [f"({exc})"]


def _py(code: str, data: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code, *args], cwd=BACKEND, env=env_for(data), capture_output=True,
                          text=True, timeout=300)


def test_a_restore_cut_off_at_any_step_is_finished_by_the_next_start(tmp_path):
    base = tmp_path / "base"
    r = _py(LAB, base)
    assert r.returncode == 0, r.stderr
    zp = Path(r.stdout.strip().splitlines()[-1])
    before, restored = ["In the backup", "After the backup"], ["In the backup"]
    outcomes = []
    for n in range(1, 60):
        data = tmp_path / f"try-{n}"
        shutil.copytree(base, data)
        cut = _py(CRASH_AT, data, str(zp), str(n))
        r = _py(AFTER, data)
        assert r.returncode == 0, f"cut off at step {n}: Benchtop didn't start again:\n{r.stderr[-3000:]}"
        titles = _titles(data)
        assert titles in (before, restored), f"cut off at step {n}: the lab afterwards holds {titles}"
        assert r.stdout.strip() == "true", f"cut off at step {n}: ./benchtop check fails afterwards"
        assert not (data / "restore-in-progress.json").exists()
        if list(data.glob(".restore-*")):  # cut off while unpacking, before anything changed (cleared after a day)
            assert titles == before
        if titles == restored:  # the data from before is kept, whole
            aside = sorted(data.glob("pre-restore-*"))
            assert len(aside) == 1 and _titles(aside[0]) == before, f"cut off at step {n}: the data from before isn't kept"
        outcomes.append(titles == restored)
        shutil.rmtree(data)
        if cut.returncode == 0:  # the restore ran to the end before reaching step n: every step has been tried
            break
    else:
        raise AssertionError("the restore never finished")
    assert outcomes[0] is False and outcomes[-1] is True and len(outcomes) > 5, outcomes


def test_check_says_to_start_benchtop_when_a_restore_was_cut_off(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "restore-in-progress.json").write_text(json.dumps({"stage": "x", "new_db": "y", "db": "z", "aside": "w"}))
    r = subprocess.run([sys.executable, "-m", "app.check"], cwd=BACKEND, env=env_for(data), capture_output=True, text=True)
    assert r.returncode == 1 and "A restore was cut off part-way" in r.stdout


# ------------------------------------------------------------------ the key that signs sign-ins

def test_an_empty_signing_key_left_by_a_crash_is_replaced(tmp_path, monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("BENCHTOP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BENCHTOP_SECRET_KEY", raising=False)
    (tmp_path / ".secret_key").write_text("")
    key = Settings().secret_key
    assert len(key) == 64 and (tmp_path / ".secret_key").read_text().strip() == key
    assert Settings().secret_key == key  # and then kept
    (tmp_path / ".secret_key").unlink()
    fresh = Settings().secret_key
    assert len(fresh) == 64 and fresh != key and not list(tmp_path.glob(".secret_key.*"))


# ------------------------------------------------------------------ updates

STOP_PART_WAY = """
import os as _os
_placed = [0]
_real_place = _place
def _place(src, dst):
    if _os.environ.get("TEST_STOP_AFTER_FILES") and ".benchtop-previous" not in str(dst):  # not the copy kept for rollback
        _placed[0] += 1
        if _placed[0] > int(_os.environ["TEST_STOP_AFTER_FILES"]):
            _os._exit(9)  # the power goes part-way through swapping the program files
    return _real_place(src, dst)
"""


@needs_tools
def test_an_update_cut_off_part_way_wont_run_and_rollback_puts_the_old_version_back(zip_lab):
    lab, _, _ = zip_lab
    copier = lab.root / "backend/app/install.py"  # this (old) version's copier, which does the swapping
    copier.write_text(copier.read_text().replace("\ndef mirror(", STOP_PART_WAY + "\n\ndef mirror(", 1))
    lab.env["TEST_STOP_AFTER_FILES"] = "40"
    r = lab.run("update", "--yes", ok=False)
    assert r.returncode != 0 and "Updated:" not in r.out and (lab.root / ".benchtop-previous/info").exists(), r.out
    del lab.env["TEST_STOP_AFTER_FILES"]

    for cmd in (["update", "--yes"], ["start"], ["backup"]):
        r = lab.run(*cmd, ok=False)
        assert r.returncode == 1 and "was cut off part-way" in r.out and "./benchtop rollback" in r.out, (cmd, r.out)
    back = lab.run("rollback", "--yes")
    assert "Back on Benchtop 50.0.0" in back.out and version_of(lab.root) == "50.0.0"
    assert not (lab.root / "backend/app/new_in_50_1.py").exists() and (lab.root / "docs/OLD-NOTES.md").exists()
    assert lab.my_work() == ["yield 87%"]
    again = lab.run("update", "--yes")
    assert "Updated: Benchtop 50.0.0 -> 50.1.0" in again.out and version_of(lab.root) == "50.1.0"
    assert (lab.root / "backend/app/new_in_50_1.py").exists() and lab.my_work() == ["yield 87%"]


# ------------------------------------------------------------------ checking a backup from an older version

def test_the_backup_made_before_an_upgrade_passes_its_check(tmp_path):
    """It keeps the old version's database on purpose (so the old Benchtop can restore it). Its check (the weekly
    backup test, Admin → Test backup, ./benchtop check FILE) failed with an error, reading it with today's tables."""
    from tests.test_v24_upgrades import place_backup

    zp = place_backup("v0.9.3", tmp_path / "backups")
    before = zp.read_bytes()
    r = subprocess.run([sys.executable, "-m", "app.check", str(zp)], cwd=BACKEND, env=env_for(tmp_path / "data"),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "All good." in r.stdout, r.stdout + r.stderr[-3000:]
    assert "brought up to date" in r.stdout and "Signed records: all" in r.stdout
    assert zp.read_bytes() == before  # the backup itself is untouched
