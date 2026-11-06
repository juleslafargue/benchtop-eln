"""Upgrading from every past version: nothing lost, nothing a signature covers changed.

An upgrade is when a lab's data is most at risk, and until now the upgrade steps were only tried on databases made by
the current code. tests/upgrades/ holds a backup made by each released version's own code (a demo lab plus extras;
tests/make_upgrade_fixture.py, which the release routine runs for each new version). For each one, with today's code:

- `./benchtop restore` takes the old backup (before 0.15 the files were inside the zip; since, beside it)
- starting Benchtop upgrades it, after backing it up first (0.21.1), and that backup keeps the old database version
- every row of every table is still there and unchanged (counts and content), so nothing a signature covers moved
- `./benchtop check` passes: the database, its version, the audit trail, signed records, trusted timestamps, files
- the API opens every record, section history, report and audit trail, downloads every attached file (checking its
  SHA-256), and opens every page's data
- a new record can still be written, signed and witnessed, and the check still passes afterwards
- a Benchtop older than the data still refuses it (TooNew)

A normal run tries five of them (KEY_VERSIONS and the newest); BENCHTOP_UPGRADE_TESTS=all tries every one. The browser
side: `python tests/smoke_browser.py --upgraded v0.9.3` opens every page on the oldest demo lab, upgraded.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parent / "upgrades").is_dir(),
                                reason="the release zip leaves the upgrade test's data out; it's in the Git repository")

BACKEND = Path(__file__).resolve().parents[1]
UPGRADES = Path(__file__).resolve().parent / "upgrades"
DEMO_PW, SEED_PW, ADMIN_PW = "demo-lab-password", "benchtop123", "admin-fixture-pw"


def _key(name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in name.removeprefix("v").removesuffix(".zip").split("."))


def fixtures() -> list[str]:
    return sorted((p.stem for p in UPGRADES.glob("v*.zip")), key=_key)


# Each version takes 15-20 s, so a normal run tries the ones that stand for the rest: the oldest, the first demo lab,
# the first with numbered upgrade steps, the first that kept files beside the zip, and the newest. GitHub's
# "Upgrades from every past version" job runs them all (BENCHTOP_UPGRADE_TESTS=all), four at a time.
KEY_VERSIONS = ("v0.7.1", "v0.9.3", "v0.13.0", "v0.15.0")


def chosen() -> list[str]:
    have = fixtures()
    if os.environ.get("BENCHTOP_UPGRADE_TESTS", "").lower() == "all":
        return have
    return [n for n in have if n in KEY_VERSIONS] + have[-1:]


def index() -> dict:
    return json.loads((UPGRADES / "fixtures.json").read_text())


def env_for(data: Path, **extra: str) -> dict[str, str]:
    e = {**os.environ, "BENCHTOP_DATA_DIR": str(data), "BENCHTOP_PUBCHEM": "off", "BENCHTOP_DOI": "off", "BENCHTOP_TIMESTAMPS": "off",
         "BENCHTOP_NMR_AUTO_UPDATE": "off", "BENCHTOP_UPDATE_CHECK": "off", "BENCHTOP_INSTRUMENT_SCAN_SECONDS": "0",
         "BENCHTOP_BACKUP_INTERVAL_HOURS": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    for k in ("BENCHTOP_BACKUP_DIR", "BENCHTOP_DATABASE_URL", "BENCHTOP_DEMO", "BENCHTOP_SECRET_KEY", "BENCHTOP_ADMIN_PASSWORD"):
        e.pop(k, None)
    return {**e, **extra}


def run(args: list[str], data: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=BACKEND, env=env_for(data, **extra), capture_output=True, text=True, timeout=600)


def place_backup(name: str, folder: Path) -> Path:
    """The version's backup as it made it: its zip (files back inside it, before 0.15), and the files folder beside."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"benchtop-backup-{name}.zip"
    shutil.copyfile(UPGRADES / f"{name}.zip", target)
    held = index()[name].get("files_in_zip", [])
    if held:
        with zipfile.ZipFile(target, "a") as z:
            for sha in held:
                z.write(UPGRADES / "files" / sha[:2] / sha, f"attachments/{sha[:2]}/{sha}", compress_type=zipfile.ZIP_STORED)
    with zipfile.ZipFile(target) as z:
        manifest = json.loads(z.read("manifest.json"))
    for sha in manifest.get("files", {}):
        dst = folder / "benchtop-files" / sha[:2] / sha
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(UPGRADES / "files" / sha[:2] / sha, dst)
    return target


def restore_and_upgrade(name: str, root: Path) -> dict:
    """Restore the old backup with today's `./benchtop restore`, then start today's Benchtop on it."""
    zp = place_backup(name, root / "from-old-version")
    data = root / "data"
    with zipfile.ZipFile(zp) as z:
        old_db = z.read("benchtop.db")
    (root / "old.db").write_bytes(old_db)
    r = run(["-m", "app.backup", "restore", str(zp)], data)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (data / "benchtop.db").read_bytes() == old_db
    r = run(["-c", "from app.db import init_db; init_db()"], data, BENCHTOP_BACKUP_INTERVAL_HOURS="24")
    assert r.returncode == 0, r.stdout + r.stderr
    return {"data": data, "old_db": root / "old.db", "log": r.stderr}


# Bytes 92-99 of an SQLite file's header: "version-valid-for" (92-95) and SQLITE_VERSION_NUMBER (96-99)
SQLITE_VERSION_STAMP = range(92, 100)


def same_sqlite_data(a: bytes, b: bytes) -> bool:
    """Two SQLite database files are the same byte for byte, except for the version stamp in the header (bytes 92-99).

    SQLite writes its own version number there whenever it writes a file. A backup made with SQLite's backup API (as
    Benchtop's are) is a new file, so it carries the version of the SQLite that made it, not of the one that made the
    original: a test machine with a newer SQLite (GitHub's, in November 2026) gives a backup whose data is unchanged
    but whose stamp differs. Everything else must match: the length, the page size and every other byte."""
    if len(a) != len(b):
        return False
    skip = SQLITE_VERSION_STAMP
    return a[:skip.start] == b[:skip.start] and a[skip.stop:] == b[skip.stop:]


def tables(db: Path) -> dict[str, tuple[list[str], list[tuple]]]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        out = {}
        for t in names:
            cols = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
            out[t] = (cols, c.execute(f'SELECT {", ".join(chr(34) + x + chr(34) for x in cols)} FROM "{t}" ORDER BY rowid').fetchall())
    return out


def schema_version(db: Path) -> int:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='benchtop_schema'").fetchone():
            return 0
        return c.execute("SELECT MAX(number) FROM benchtop_schema").fetchone()[0] or 0


@pytest.mark.parametrize("name", chosen())
def test_upgrade_from(name, tmp_path):
    from app import migrations

    up = restore_and_upgrade(name, tmp_path)
    data, old_db = up["data"], up["old_db"]

    # 1. the automatic backup before the upgrade (0.21.1): made when a step was due, keeping the old version
    old_v = schema_version(old_db)
    made = sorted((data / "backups").glob("benchtop-backup-*.zip"))
    if old_v < migrations.SCHEMA_VERSION:
        assert len(made) == 1, up["log"]
        with zipfile.ZipFile(made[0]) as z:
            m = json.loads(z.read("manifest.json"))
            assert same_sqlite_data(z.read("benchtop.db"), old_db.read_bytes()), "the backup before the upgrade isn't the old data"
        assert m["reason"] == "before-upgrade" and (m.get("schema_version") or 0) == old_v
    else:
        assert made == [], "a backup before an upgrade when nothing was due"
    assert schema_version(data / "benchtop.db") == migrations.SCHEMA_VERSION

    # 2. every row of every table is still there, and what was in it is unchanged (new columns aside)
    before, after = tables(old_db), tables(data / "benchtop.db")
    for t, (cols, rows) in before.items():
        assert t in after, f"table {t} is gone"
        acols, arows = after[t]
        if t == "benchtop_schema":  # the upgrade steps that ran are added to it
            arows = arows[:len(rows)]
        assert len(arows) == len(rows), f"{t}: {len(rows)} rows before, {len(arows)} after"
        keep = [acols.index(c) for c in cols]
        changed = [i for i, (b, a) in enumerate(zip(rows, arows)) if tuple(a[k] for k in keep) != b]
        assert not changed, f"{t}: {len(changed)} rows changed, e.g. {rows[changed[0]]} -> {arows[changed[0]]}"

    # 3. ./benchtop check: database, version, audit trail, signed records, trusted timestamps, attached files
    out = health_check(data)
    if "trusted timestamps" in " ".join(index()[name]["extras"]):
        assert "Trusted timestamps: all 2 verify" in out, out

    # 4-5. a real server on the upgraded data opens everything, and a new record can be signed and witnessed
    with server(data) as (base, log):
        problems = walk(base, data / "benchtop.db")
    assert not problems, "\n".join(problems[:40])
    assert "Traceback" not in log.read_text(), log.read_text()[-6000:]
    health_check(data)


def health_check(data: Path) -> str:
    r = run(["-m", "app.check"], data)
    assert r.returncode == 0 and "All good." in r.stdout, r.stdout + r.stderr
    for part in ("Database file", "Database version", "Audit trail", "Signed records", "Attached files"):
        assert f"✓ {part}" in r.stdout, r.stdout
    return r.stdout


def test_same_sqlite_data_skips_only_the_version_stamp(tmp_path):
    """0.31.0: the check that a backup before an upgrade holds the old data skips the 8 bytes where SQLite stamps its
    own version, and nothing else."""
    db = tmp_path / "small.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (x TEXT)")
        c.executemany("INSERT INTO t VALUES (?)", [(f"yield {i}%",) for i in range(500)])
    c.close()
    a = db.read_bytes()
    assert len(a) > 4096 and int.from_bytes(a[16:18], "big") > 0  # a header and more than one page

    def changed(offset: int) -> bytes:
        b = bytearray(a)
        b[offset] ^= 0xFF
        return bytes(b)
    assert same_sqlite_data(a, a)
    stamped = bytearray(a)
    stamped[92:100] = (12345).to_bytes(4, "big") + (3099001).to_bytes(4, "big")  # another SQLite's stamp
    assert same_sqlite_data(a, bytes(stamped)) and same_sqlite_data(bytes(stamped), a)
    for offset in (0, 16, 24, 91, 100, len(a) // 2, len(a) - 1):  # the header outside 92-99, and data pages
        assert not same_sqlite_data(a, changed(offset)), offset
    assert not same_sqlite_data(a, a + bytes(4096))  # a page more
    assert not same_sqlite_data(a, a[:-4096])  # a page fewer
    # as in the upgrade test: the old data is itself a backup, and the backup before the upgrade is a backup of it.
    # (SQLite's backup API also restarts the change counter, bytes 24-27, so only a backup of a backup matches.)
    def backup(frm: Path, to: Path) -> bytes:
        src, dst = sqlite3.connect(frm), sqlite3.connect(to)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return to.read_bytes()
    first = backup(db, tmp_path / "first.db")
    assert same_sqlite_data(first, backup(tmp_path / "first.db", tmp_path / "second.db"))


def test_an_older_benchtop_still_refuses_the_upgraded_data(tmp_path):
    """After the upgrade, data marked as opened by a newer Benchtop is refused, and left as it was."""
    name = fixtures()[-1]
    data = restore_and_upgrade(name, tmp_path)["data"]
    with sqlite3.connect(data / "benchtop.db") as c:
        c.execute("INSERT INTO benchtop_schema VALUES (999, 'From the future', '9.9.9', '2030-01-01T00:00:00+00:00')")
    snap = tables(data / "benchtop.db")
    # the upgrade just now may have backed up first (when the newest fixture is older than today's database steps)
    had = sorted((data / "backups").glob("*.zip"))
    r = run(["-c", "from app.db import init_db; init_db()"], data, BENCHTOP_BACKUP_INTERVAL_HOURS="24")
    assert r.returncode != 0 and "TooNew" in r.stderr and "newer Benchtop (9.9.9" in r.stderr
    assert tables(data / "benchtop.db") == snap
    assert sorted((data / "backups").glob("*.zip")) == had, "the refused start made a backup"


def test_every_released_version_has_upgrade_data():
    """The release routine adds each new version's backup (tests/make_upgrade_fixture.py)."""
    from app import __version__
    have = fixtures()
    assert have[0] == "v0.7.1", "the oldest released version's data is missing"
    assert f"v{__version__}" in have, (f"No upgrade test data for {__version__} yet. Run, from backend/: "
                                       "python3 tests/make_upgrade_fixture.py (the release routine)")
    assert set(index()) == set(have)
    for name in have:
        with zipfile.ZipFile(UPGRADES / f"{name}.zip") as z:
            assert json.loads(z.read("manifest.json"))["app_version"] == name.removeprefix("v"), name
            assert not [n for n in z.namelist() if n.startswith("attachments/")], f"{name}: its files belong in upgrades/files"
    for f in (UPGRADES / "files").rglob("*"):
        if f.is_file():
            assert hashlib.sha256(f.read_bytes()).hexdigest() == f.name, f"upgrades/files/{f.name} is damaged"


# ------------------------------------------------------------------ a real server, and what it's asked

@contextlib.contextmanager
def server(data: Path, **extra: str):
    """Benchtop's own web server on the data, as `./benchtop start` runs it. Yields its address and its log."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = data.parent / f"server-{port}.log"
    with open(log, "w") as f:  # a file, not a pipe: an unread pipe fills up and stalls the server
        p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=BACKEND,
                             env=env_for(data, **extra), stdout=f, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(240):
                try:
                    if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if p.poll() is not None:
                    raise AssertionError("The server stopped:\n" + log.read_text()[-4000:])
                time.sleep(0.25)
            else:
                raise AssertionError("The server didn't start:\n" + log.read_text()[-4000:])
            yield base, log
        finally:
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()


def walk(base: str, db: Path) -> list[str]:
    """Sign in as everyone; open every page's data, every record (report, history, audit trail, signatures) and every
    attached file; then write, sign and witness a new record. Returns what went wrong."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        users = {r[0]: {"username": r[1], "role": r[2], "active": r[3]} for r in c.execute("SELECT id, username, role, active FROM user")}
        exps = [{"id": r[0], "owner": r[1], "status": r[2]} for r in c.execute("SELECT id, owner_id, status FROM experiment")]
        atts = [{"id": r[0], "exp": r[1], "sha": r[2], "name": r[3]} for r in c.execute("SELECT id, experiment_id, sha256, filename FROM attachment")]
        entries = [{"id": r[0], "exp": r[1]} for r in c.execute("SELECT id, experiment_id FROM entry")]
    demo = any(u["username"] == "hweiss" for u in users.values())
    owner = {e["id"]: e["owner"] for e in exps}
    problems: list[str] = []
    tokens: dict[int, dict[str, str]] = {}
    c = httpx.Client(base_url=base, timeout=120)

    def pw_of(uid: int) -> str:
        return ADMIN_PW if users[uid]["username"] == "admin" else DEMO_PW if demo else SEED_PW

    def as_user(uid: int) -> dict[str, str]:
        if uid not in tokens:
            r = c.post("/api/auth/login", json={"username": users[uid]["username"], "password": pw_of(uid)})
            assert r.status_code == 200, (users[uid]["username"], r.text)
            tokens[uid] = {"Authorization": f"Bearer {r.json()['token']}"}
        return tokens[uid]

    def get(path: str, uid: int, **kw) -> httpx.Response:
        r = c.get(path, headers=as_user(uid), **kw)
        if r.status_code != 200:
            problems.append(f"GET {path} as {users[uid]['username']}: {r.status_code} {r.text[:300]}")
        return r

    active = [i for i, u in users.items() if u["active"] and not (demo and u["username"] == "admin")]
    admin = next(i for i in active if users[i]["role"] == "admin")
    for uid in active:  # everyone's own pages
        for p in ("/api/auth/me", "/api/dashboard", "/api/notebooks", "/api/experiments?limit=1000", "/api/notifications",
                  "/api/materials", "/api/lots", "/api/search?q=yield", "/api/reactions", "/api/compounds", "/api/groups",
                  "/api/inbox", "/api/inbox/count", "/api/spectra", "/api/auth/tokens", "/api/auth/sessions"):
            get(p, uid)
    for p in ("/api/users", "/api/audit", "/api/admin/backups", "/api/admin/timestamps", "/api/admin/email",
              "/api/admin/witness-settings", "/api/instruments", "/api/webhooks", "/api/plugins", "/api/safety/status"):
        get(p, admin)
    v = get("/api/audit/verify", admin).json()
    if not v.get("ok"):
        problems.append(f"audit trail: {v}")
    for e in exps:
        uid = e["owner"]
        get(f"/api/experiments/{e['id']}", uid)
        for fmt in ("html", "md", "json"):
            get(f"/api/experiments/{e['id']}/export", uid, params={"format": fmt})
        get(f"/api/experiments/{e['id']}/audit", uid)
        get(f"/api/experiments/{e['id']}/writeup", uid)
        if e["status"] in ("signed", "witnessed", "locked"):
            v = get(f"/api/experiments/{e['id']}/verify", uid).json()
            if not v.get("intact"):
                problems.append(f"experiment {e['id']} ({e['status']}) no longer matches its signature: {v}")
    for x in entries:
        get(f"/api/entries/{x['id']}/versions", owner[x["exp"]])
    for a in atts:
        r = get(f"/api/attachments/{a['id']}", owner[a["exp"]])
        if r.status_code == 200 and hashlib.sha256(r.content).hexdigest() != a["sha"]:
            problems.append(f"{a['name']} downloads with different content")
    done = [e for e in exps if e["status"] == "witnessed"]
    if done:  # one report as PDF (the slowest export), and everything as .eln
        get(f"/api/experiments/{done[0]['id']}/export", done[0]["owner"], params={"format": "pdf"})
    get("/api/export.eln", exps[0]["owner"])

    # a new record, signed and witnessed, on the upgraded data
    writer = next(i for i in active if users[i]["role"] == "scientist")
    witness = next(i for i in active if i != writer and users[i]["role"] in ("scientist", "admin"))
    h = as_user(writer)
    nb = c.post("/api/notebooks", headers=h, json={"name": "After the upgrade"}).json()
    exp = c.post("/api/experiments", headers=h, json={"title": "Written after the upgrade", "notebook_id": nb["id"]}).json()
    steps = [c.post(f"/api/experiments/{exp['id']}/entries", headers=h,
                    json={"kind": "text", "title": "Procedure", "content": {"html": "<p>Still works.</p>"}}),
             c.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("after.csv", b"t,mAU\n0,1\n", "text/csv")}),
             c.post(f"/api/experiments/{exp['id']}/sign", headers=h, json={"password": pw_of(writer)}),
             c.post(f"/api/experiments/{exp['id']}/witness", headers=as_user(witness), json={"password": pw_of(witness)})]
    for r in steps:
        if r.status_code not in (200, 201):
            problems.append(f"new record: {r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
    if steps[-1].status_code == 200 and steps[-1].json()["status"] != "witnessed":
        problems.append(f"new record: {steps[-1].json()['status']} after witnessing")
    c.close()
    return problems
