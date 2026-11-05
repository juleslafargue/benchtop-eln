import os
import sys
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="benchtop-test-")
os.environ["BENCHTOP_DATA_DIR"] = _tmp
os.environ["BENCHTOP_ADMIN_PASSWORD"] = "adminpass"
os.environ["BENCHTOP_BACKUP_INTERVAL_HOURS"] = "0"  # tests trigger backups explicitly
os.environ["BENCHTOP_CHECK_INTERVAL_HOURS"] = "0"  # tests run the daily data check explicitly
os.environ["BENCHTOP_PUBCHEM"] = "off"  # tests inject a mock PubChem
os.environ["BENCHTOP_DOI"] = "off"  # no calls to doi.org; tests inject a mock
os.environ["BENCHTOP_TIMESTAMPS"] = "off"  # no calls to real timestamp services; tests switch on a fake one
os.environ["BENCHTOP_NMR_AUTO_UPDATE"] = "off"  # no NMRShiftDB2 download at start-up (tests build their own table)
os.environ["BENCHTOP_INSTRUMENT_SCAN_SECONDS"] = "0"  # tests look at instrument folders explicitly (instruments.scan_all)
os.environ["BENCHTOP_UPDATE_CHECK"] = "off"  # no calls to GitHub; tests feed releases in themselves
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def _login(client, u, p):
    r = client.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="session")
def admin(client):
    return _login(client, "admin", "adminpass")


@pytest.fixture(scope="session")
def users(client, admin):
    for u, role in (("alice", "scientist"), ("bob", "scientist"), ("vic", "viewer")):
        client.post("/api/users", headers=admin, json={"username": u, "password": "lab-bench-pw-2026", "must_change": False,
                                                       "full_name": u.title(), "role": role})
    return {u: _login(client, u, "lab-bench-pw-2026") for u in ("alice", "bob", "vic")}


@pytest.fixture()
def notebook(client, users):
    return client.post("/api/notebooks", headers=users["alice"], json={"name": "NB"}).json()


@pytest.fixture(scope="module")
def safety_settled(client):
    """For a test file that registers many materials (0.31): when it's done, their safety data counts as looked up,
    so the hourly refresh in later tests (40 records a run, those never looked up first) still reaches theirs."""
    from sqlalchemy import func
    from sqlmodel import Session, select

    from app import safety
    from app.db import engine
    from app.models import ChemSafety, Material, utcnow
    with Session(engine) as s:
        start = s.exec(select(func.max(Material.id))).one() or 0
    yield
    with Session(engine) as s:
        now = utcnow()
        for m in s.exec(select(Material).where(Material.id > start)).all():
            key = safety.material_key(m)
            cs = s.exec(select(ChemSafety).where(ChemSafety.key == key)).first() if key else None
            if key and (cs is None or cs.fetched_at is None):
                cs = cs or ChemSafety(key=key, title=m.name)
                cs.status, cs.fetched_at, cs.checked_at = "not_found", now, now
                s.add(cs)
        s.commit()
