"""v0.36.6: backups after a failure, and restoring a damaged, wrong or partial backup file.

- A backup folder that isn't there (a drive not connected) gave "Internal Server Error" on Back up now; now a plain
  message, recorded, and the next backup that works clears the warning. The Admin page shows the last backup that
  worked and the last failure.
- A copy to the second place cut short (the drive full or pulled out) left a partial zip there, taking the room.
- Restoring a file that isn't a whole Benchtop backup printed a Python traceback, and one whose database wasn't a
  database replaced the data with it. Now a plain message, and nothing is changed."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app import backup, check
from app.config import settings

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def own_backups(tmp_path, monkeypatch):
    """Backups in a folder of this test's own, with the state file (last success, last failure) its own too."""
    monkeypatch.setattr(settings, "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(settings, "backup_interval_hours", 24.0)
    state = settings.data_dir / "backup-settings.json"
    saved = state.read_bytes() if state.exists() else None
    state.unlink(missing_ok=True)
    yield tmp_path
    backup.set_copy_dir(None)
    if saved is None:
        state.unlink(missing_ok=True)
    else:
        state.write_bytes(saved)


def test_a_missing_backup_folder_gets_a_plain_message_and_the_next_backup_clears_it(client, admin, own_backups):
    assert client.post("/api/admin/backups", headers=admin).status_code == 201
    first = client.get("/api/admin/backups", headers=admin).json()
    assert first["last_success"] and first["last_failure"] is None and not first["failing"]
    bd = settings.backup_dir
    shutil.move(bd, own_backups / "moved")
    bd.write_text("not a folder")  # as an unplugged drive's empty mount point can be
    r = client.post("/api/admin/backups", headers=admin)
    assert r.status_code == 400, r.text
    assert "couldn't write to" in r.json()["detail"] and "tries again" in r.json()["detail"], r.text
    h = client.get("/api/admin/backups", headers=admin).json()
    assert h["failing"] and h["last_failure"]["error"] == r.json()["detail"]
    assert any(w.startswith("The last backup failed") for w in h["warnings"])
    assert h["last_success"]["at"] == first["last_success"]["at"]  # still says when the last good one was
    bd.unlink()
    shutil.move(own_backups / "moved", bd)
    assert client.post("/api/admin/backups", headers=admin).status_code == 201  # tries afresh, and works
    h = client.get("/api/admin/backups", headers=admin).json()
    assert not h["failing"] and not any("failed" in w for w in h["warnings"])
    assert h["last_failure"] and h["last_success"]["at"] > h["last_failure"]["at"]  # both shown, the failure as past


def test_a_copy_cut_short_leaves_nothing_behind_and_the_next_one_works(client, admin, own_backups, monkeypatch):
    copy = own_backups / "copy"
    copy.mkdir()
    backup.set_copy_dir(str(copy))
    real = shutil.copyfile

    def full(src, dst, *a, **k):
        with open(dst, "wb") as f:
            f.write(b"x" * 1000)
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(shutil, "copyfile", full)
    b = client.post("/api/admin/backups", headers=admin).json()
    assert b["copy"] and not b["copy"]["ok"] and "full" in b["copy"]["error"]
    assert [p.name for p in copy.iterdir() if p.name.endswith(".part")] == []
    monkeypatch.setattr(shutil, "copyfile", real)
    b2 = client.post("/api/admin/backups", headers=admin).json()
    assert b2["copy"]["ok"] and (copy / b2["name"]).exists()
    h = client.get("/api/admin/backups", headers=admin).json()
    assert h["last_copy"]["ok"] and h["last_copy_ok"]["name"] == b2["name"]


# ---------------------------------------------------------------- restoring a bad file (item 7)

@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    """A Benchtop data folder of its own, with one good backup, restored into from its own process (./benchtop restore)."""
    data = tmp_path_factory.mktemp("restore-lab")
    env = {**os.environ, "BENCHTOP_DATA_DIR": str(data), "BENCHTOP_BACKUP_INTERVAL_HOURS": "0"}
    env.pop("BENCHTOP_BACKUP_DIR", None)
    subprocess.run([sys.executable, "-c", "from app.db import init_db; init_db()"], cwd=BACKEND, env=env, check=True, timeout=120)
    made = subprocess.run([sys.executable, "-m", "app.backup", "create"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    good = Path(made.stdout.split("Backup written: ")[1].split(" (")[0])
    return data, env, good


def _rezip(good: Path, out: Path, drop=(), change=None) -> None:
    with zipfile.ZipFile(good) as zi, zipfile.ZipFile(out, "w") as zo:
        for n in zi.namelist():
            if n not in drop:
                data = zi.read(n)
                zo.writestr(n, change[n](data) if change and n in change else data)


NOT_A_DB = b"hello, not a database" * 100


def _manifest(f):
    return lambda b: json.dumps(f(json.loads(b))).encode()


BAD = {
    "cut-short.zip": ("cut short", lambda g, p: p.write_bytes(g.read_bytes()[: g.stat().st_size // 2])),
    "random.zip": ("isn't a zip file", lambda g, p: p.write_bytes(os.urandom(5000))),
    "empty.zip": ("the file is empty", lambda g, p: p.write_bytes(b"")),
    "notes.txt": ("isn't a zip file", lambda g, p: p.write_text("just some notes")),
    "no-list.zip": ("Not a Benchtop backup", lambda g, p: _rezip(g, p, drop=("manifest.json",))),
    "list-not-json.zip": ("damaged list of contents", lambda g, p: _rezip(g, p, change={"manifest.json": lambda b: b"{nope"})),
    "list-a-list.zip": ("damaged list of contents", lambda g, p: _rezip(g, p, change={"manifest.json": lambda b: b"[1, 2]"})),
    "no-fingerprint.zip": ("no fingerprint", lambda g, p: _rezip(g, p, change={"manifest.json": _manifest(
        lambda m: {k: v for k, v in m.items() if k != "database_sha256"})})),
    "version-in-words.zip": ("version isn't a number", lambda g, p: _rezip(g, p, change={"manifest.json": _manifest(
        lambda m: {**m, "schema_version": "new"})})),
    "files-a-list.zip": ("attached files is damaged", lambda g, p: _rezip(g, p, change={"manifest.json": _manifest(
        lambda m: {**m, "files": [1, 2]})})),
    "not-a-database.zip": ("doesn't hold a Benchtop database", lambda g, p: _rezip(g, p, change={
        "benchtop.db": lambda b: NOT_A_DB,
        "manifest.json": _manifest(lambda m: {**m, "database_sha256": hashlib.sha256(NOT_A_DB).hexdigest()})})),
    "damaged-inside.zip": ("damaged", lambda g, p: p.write_bytes(_zero_inside(g))),
    "a-folder": ("is a folder", lambda g, p: p.mkdir()),
}


def _zero_inside(good: Path) -> bytes:
    raw = bytearray(good.read_bytes())
    i = raw.find(b"benchtop.db") + 200
    raw[i:i + 64] = b"\0" * 64
    return bytes(raw)


@pytest.mark.parametrize("name", list(BAD))
def test_restoring_a_bad_file_says_whats_wrong_and_changes_nothing(lab, tmp_path, name):
    data, env, good = lab
    says, make = BAD[name]
    p = tmp_path / name
    make(good, p)
    db = data / "benchtop.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    r = subprocess.run([sys.executable, "-m", "app.backup", "restore", str(p)], cwd=BACKEND, env=env, capture_output=True,
                       text=True, timeout=120)
    out = r.stdout + r.stderr
    assert "Traceback" not in out and r.returncode == 2, out
    assert says in out and "Nothing was changed" in out, out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not list(data.glob("pre-restore-*")) and not list(data.glob(".restore-*"))


def test_checking_a_bad_file_says_so_too(lab, tmp_path):
    _, _, good = lab
    for name in ("list-a-list.zip", "files-a-list.zip", "damaged-inside.zip", "cut-short.zip"):
        p = tmp_path / name
        BAD[name][1](good, p)
        res = check.check_backup(p)
        assert res["ok"] is False and res["checks"][0]["name"] == "Backup file", (name, res)
