"""Make the upgrade test's data for one version of Benchtop: a lab made by that version's own code, backed up by that
version's own backup command. tests/test_v24_upgrades.py restores each one with the current code, upgrades it, and
checks that nothing was lost or changed.

Part of the release routine, after bumping the version (run from backend/):

    python3 tests/make_upgrade_fixture.py                 # this version, from the files as they are now
    python3 tests/make_upgrade_fixture.py --commit 2148bb8  # an older version, from git history

What it does:
  1. a fresh data folder, filled by that version's demo lab (`app.demo_lab`; before 0.9.3, `app.seed`)
  2. extras, through that version's own API, for the features it has: a record signed and witnessed (with trusted
     timestamps from a test timestamp service, from 0.14.1), an instrument folder with a file attached and one waiting
     in the Inbox (0.16), sample codes (0.17), a signed record exported and imported back as a locked record, plus an
     eLabFTW export (0.20), a References section (0.21), an API token and a webhook (0.10), the signed record published
     on a fake Zenodo sandbox (tests/fake_zenodo.py), with its author's Zenodo token (0.28)
  3. a backup with that version's own `python -m app.backup create`
  4. into tests/upgrades/: the backup zip as vX.Y.Z.zip, without the attached files; those go once into
     tests/upgrades/files/ (most are the same from one version to the next), and fixtures.json notes where it came
     from, what's in it, and (for backups from before 0.15, which held their files inside the zip) which files the
     zip held, so the test can put them back.

The release zip leaves tests/upgrades/ out (deploy/release.py), so this doesn't make downloads bigger.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent
REPO = BACKEND.parent
OUT = TESTS / "upgrades"
FILES = OUT / "files"
INDEX = OUT / "fixtures.json"
DEMO_PW = "demo-lab-password"  # the demo lab's accounts (0.9.3 on)
SEED_PW = "benchtop123"  # app.seed's alice and bob (before 0.9.3)
ADMIN_PW = "admin-fixture-pw"
OFFLINE = {"BENCHTOP_PUBCHEM": "off", "BENCHTOP_DOI": "off", "BENCHTOP_NMR_AUTO_UPDATE": "off",
           "BENCHTOP_UPDATE_CHECK": "off", "BENCHTOP_INSTRUMENT_SCAN_SECONDS": "0", "BENCHTOP_BACKUP_INTERVAL_HOURS": "0",
           "BENCHTOP_ADMIN_PASSWORD": ADMIN_PW, "BENCHTOP_TIMESTAMPS": "off"}


def _version_of(backend: Path) -> str:
    m = re.search(r'^__version__ = "([^"]+)"', (backend / "app/__init__.py").read_text(), re.M)
    assert m, "no __version__"
    return m.group(1)


@contextlib.contextmanager
def _locked():
    """Several versions can be made at once (one per process): git worktrees and fixtures.json take turns."""
    OUT.mkdir(parents=True, exist_ok=True)
    with open(Path(tempfile.gettempdir()) / "benchtop-fixture.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _run(args: list[str], cwd: Path, env: dict[str, str], timeout: int = 900) -> str:
    r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed in {cwd}:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return r.stdout


def make(commit: str | None) -> str:
    with tempfile.TemporaryDirectory(prefix="benchtop-fixture-") as tmp:
        tmp = Path(tmp)
        if commit:
            src = tmp / "src"
            with _locked():
                subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(src), commit], check=True, capture_output=True)
        else:
            src = REPO
        try:
            backend = src / "backend"
            version = _version_of(backend)
            data = tmp / "data"
            data.mkdir()
            env = {**os.environ, **OFFLINE, "BENCHTOP_DATA_DIR": str(data), "PYTHONDONTWRITEBYTECODE": "1"}
            for k in ("BENCHTOP_BACKUP_DIR", "BENCHTOP_DATABASE_URL", "BENCHTOP_DEMO", "BENCHTOP_STATIC_DIR"):
                env.pop(k, None)
            maker = "app.demo_lab" if (backend / "app/demo_lab.py").exists() else "app.seed"
            _run([sys.executable, "-m", maker], backend, env)
            extras = json.loads(_run([sys.executable, str(Path(__file__).resolve()), "--extras", str(tmp / "instrument")],
                                     backend, {k: v for k, v in env.items() if k != "BENCHTOP_TIMESTAMPS"}).strip().splitlines()[-1])
            _run([sys.executable, "-m", "app.backup", "create"], backend, env)
            zips = sorted((data / "backups").glob("benchtop-backup-*.zip"))
            assert len(zips) == 1, zips
            files_in_zip = _store(zips[0], data / "backups" / "benchtop-files", OUT / f"v{version}.zip")
        finally:
            if commit:
                with _locked():
                    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(src)], capture_output=True)
    with _locked():
        index = json.loads(INDEX.read_text()) if INDEX.exists() else {}
        index[f"v{version}"] = {
            "commit": commit or _head(), "made": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "lab": maker.removeprefix("app."),
            "extras": extras, **({"files_in_zip": files_in_zip} if files_in_zip else {})}
        INDEX.write_text(json.dumps(dict(sorted(index.items(), key=lambda kv: _key(kv[0]))), indent=1) + "\n")
    return version


def _head() -> str:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() + \
        (" (with changes not yet committed)" if subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "backend/app"],
                                                               capture_output=True, text=True).stdout.strip() else "")


def _key(name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in name.lstrip("v").split("."))


def _store(backup: Path, shared: Path, target: Path) -> list[str]:
    """Copy the backup without its attached files; put those (once) in FILES. Returns the files the zip itself held."""
    held = []
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup) as z, zipfile.ZipFile(target, "w") as out:
        for info in z.infolist():
            if info.filename.startswith("attachments/") and re.fullmatch(r"[0-9a-f]{64}", Path(info.filename).name):
                sha = Path(info.filename).name
                _keep(z.read(info), sha)
                held.append(sha)
            else:
                out.writestr(info, z.read(info), compress_type=info.compress_type)
    for f in sorted(shared.rglob("*")) if shared.is_dir() else []:
        if f.is_file() and re.fullmatch(r"[0-9a-f]{64}", f.name):
            _keep(f.read_bytes(), f.name)
    return sorted(held)


def _keep(data: bytes, sha: str) -> None:
    p = FILES / sha[:2] / sha
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


# ------------------------------------------------------------------ extras, run inside the old version's code

def extras(instrument_dir: Path) -> list[str]:
    """Runs with the old version's backend as the current folder: only what that version has, through its own API."""
    sys.path.insert(0, os.getcwd())
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from app import models
    from app.db import engine
    from app.main import app

    done: list[str] = []
    with Session(engine) as s:
        names = {u.username for u in s.exec(select(models.User))}
    demo = "hweiss" in names
    author, witness = ("mruiz", "hweiss") if demo else ("alice", "bob")
    pw = DEMO_PW if demo else SEED_PW
    ts = None
    try:
        from app import sitesettings, timestamps
        sys.path.insert(0, str(TESTS))
        from fake_tsa import FakeTSA
        timestamps.synchronous = True
        timestamps.transport = FakeTSA()
        if hasattr(timestamps, "_skip_until"):
            timestamps._skip_until = 0.0
        sitesettings.update("timestamps", {"enabled": True, "tsa_url": "http://tsa.example.org"})
        ts = timestamps
    except ImportError:
        pass

    with TestClient(app) as c:
        def login(u: str, p: str) -> dict[str, str]:
            r = c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, (u, r.text)
            return {"Authorization": f"Bearer {r.json()['token']}"}

        def ok(r, what: str):
            assert r.status_code in (200, 201), f"{what}: {r.status_code} {r.text[:500]}"
            return r.json()

        a, w = login(author, pw), login(witness, pw)
        admin = w if demo else login("admin", ADMIN_PW)  # the demo lab's lab manager is its admin
        nb = ok(c.post("/api/notebooks", headers=a, json={"name": "Upgrade checks"}), "notebook")

        def record(title: str) -> dict:
            exp = ok(c.post("/api/experiments", headers=a, json={"title": title, "notebook_id": nb["id"]}), "experiment")
            ok(c.post(f"/api/experiments/{exp['id']}/entries", headers=a,
                      json={"kind": "text", "title": "Procedure", "content": {"html": "<p>Stirred 2 h at 25 °C; yield 87%.</p>"}}), "entry")
            return exp

        # signed and witnessed (with trusted timestamps when this version has them)
        exp = record("Signed and witnessed before the upgrade")
        ok(c.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": pw}), "sign")
        ok(c.post(f"/api/experiments/{exp['id']}/witness", headers=w, json={"password": pw}), "witness")
        done.append("signed and witnessed record")
        if ts is not None:
            ts.retry_pending()
            with Session(engine) as s:
                got = [t.status for t in s.exec(select(models.SignatureTimestamp))]
            assert got and all(x == "ok" for x in got), got
            done.append(f"{len(got)} trusted timestamps")

        if hasattr(models, "ApiToken"):
            ok(c.post("/api/auth/tokens", headers=a, json={"name": "Plate reader script", "scope": "read"}), "token")
            done.append("API token")
        if hasattr(models, "Webhook"):
            ok(c.post("/api/webhooks", headers=admin, json={"name": "Lab chat", "url": "http://127.0.0.1:9/hook",
                                                            "events": ["experiment.signed"], "active": False}), "webhook")
            done.append("webhook (inactive)")

        if hasattr(models, "SampleCode"):
            ok(c.put("/api/auth/me/sample-prefix", headers=a, json={"prefix": "MR"}), "sample prefix")
            sx = record("Sample codes")
            ok(c.post(f"/api/experiments/{sx['id']}/samples", headers=a, json={}), "sample code")
            ok(c.post(f"/api/experiments/{sx['id']}/samples", headers=a, json={"number": 42}), "sample code 42")
            done.append("sample codes")

        if hasattr(models, "Instrument"):
            from app import instruments
            instrument_dir.mkdir(parents=True, exist_ok=True)
            ok(c.post("/api/instruments", headers=admin, json={"name": "HPLC 1", "kind": "files", "folder": str(instrument_dir)}), "instrument")
            ix = record("Instrument data")
            t0 = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5)  # naive UTC, as Benchtop stores times
            instruments.scan_all(now=t0)
            (instrument_dir / f"{ix['eid']} hplc.csv").write_text("time,mAU\n0.0,1.2\n0.1,1.4\n")
            (instrument_dir / "no number here.csv").write_text("time,mAU\n0.0,3.1\n")
            for dt in (10, 300, 600):
                instruments.scan_all(now=t0 + timedelta(seconds=dt))
            got = ok(c.get(f"/api/experiments/{ix['id']}", headers=a), "instrument record")
            assert got["attachments"], "the instrument file wasn't attached"
            done.append("instrument folder: a file attached, one in the Inbox")

        if (Path("app") / "eln_import.py").exists():
            r = c.get(f"/api/experiments/{exp['id']}/export.eln", headers=a)
            assert r.status_code == 200, r.text[:300]
            for name, data in (("signed.eln", r.content), ("elabftw-export.eln", (TESTS / "data/eln/elabftw-export.eln").read_bytes())):
                p = ok(c.post("/api/eln/preview", headers=a, files={"file": (name, data, "application/zip")}), f"preview {name}")
                ok(c.post(f"/api/eln/import/{p['token']}", headers=a,
                          json={"notebook_id": nb["id"], "items": [i["key"] for i in p["items"]]}), f"import {name}")
            done.append("imported .eln: a signed record (locked) and an eLabFTW export")

        if (Path("app") / "doi.py").exists():
            rx = record("References")
            ok(c.post(f"/api/experiments/{rx['id']}/entries", headers=a, json={"kind": "references", "title": "References", "content": {"items": [
                {"doi": "10.1021/om100106e", "title": "NMR Chemical Shifts of Trace Impurities", "authors": ["Fulmer, Gregory R."],
                 "journal": "Organometallics", "year": "2010", "volume": "29", "issue": "9", "pages": "2176-2179"},
                {"citation": "Smith, J. Lab notebook p. 12 (typed in by hand)."}]}}), "references")
            done.append("References section")

        if (Path("app") / "publishing.py").exists():  # 0.28: the signed record published on (a fake) Zenodo's sandbox
            from app import zenodo
            from fake_zenodo import GOOD, FakeZenodo
            zenodo.set_transport(FakeZenodo(zenodo.SANDBOX).transport())
            ok(c.put("/api/admin/zenodo", headers=admin, json={"site": "sandbox"}), "Zenodo site")
            ok(c.put("/api/auth/zenodo", headers=a, json={"token": GOOD}), "Zenodo token")
            d = ok(c.get(f"/api/experiments/{exp['id']}/zenodo/preview", headers=a), "publish preview")["details"]
            ok(c.post(f"/api/experiments/{exp['id']}/zenodo/prepare", headers=a, json=d), "prepare on Zenodo")
            ok(c.post(f"/api/experiments/{exp['id']}/zenodo/publish", headers=a, json={"confirm": True}), "publish on Zenodo")
            done.append("the signed record published on Zenodo (a fake sandbox), and its author's Zenodo token")
    return done


def main(argv: list[str]) -> int:
    if len(argv) > 2 and argv[1] == "--extras":
        print(json.dumps(extras(Path(argv[2]))))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--commit", help="an older version's commit (default: this version, from the files as they are now)")
    args = ap.parse_args(argv[1:])
    v = make(args.commit)
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"Made tests/upgrades/v{v}.zip. The upgrade test data is now {size / 1e6:.1f} MB in all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
