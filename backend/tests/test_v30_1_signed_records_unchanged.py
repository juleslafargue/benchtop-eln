"""0.30.1: upgrading never changes what a signature covers, nor how its fingerprint is worked out.

0.30.1 rebuilds the notebook table (database step 6) and changes how signed records from another Benchtop are imported.
Neither may touch a signed record that's already in a lab's data. For the data of past versions (tests/upgrades/),
upgraded by today's code:

- every row of a signed or witnessed record (the record, its sections and their history, its files, its signatures)
  is byte for byte what it was before the upgrade, and
- today's services.content_hash gives, for each of them, exactly the fingerprint the old version stored when it was
  signed (so the way the fingerprint is worked out hasn't changed either).

A normal run tries the key versions (as test_v24_upgrades.py does); BENCHTOP_UPGRADE_TESTS=all tries every one."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_v24_upgrades import chosen, restore_and_upgrade, run

pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parent / "upgrades").is_dir(),
                                reason="the release zip leaves the upgrade test's data out; it's in the Git repository")

HASHES = """
import json
from sqlmodel import Session, select
from app.db import engine
from app.models import Experiment, ExperimentStatus, Signature
from app.services import content_hash
out = {}
with Session(engine) as s:
    for e in s.exec(select(Experiment).where(Experiment.status.in_([ExperimentStatus.signed, ExperimentStatus.witnessed]))):
        sig = s.exec(select(Signature).where(Signature.experiment_id == e.id, Signature.meaning.in_(["author", "witness"]))
                     .order_by(Signature.id.desc())).first()
        out[e.id] = [content_hash(s, e), sig.content_hash if sig else None]
print(json.dumps(out))
"""


def _signed_rows(db: Path) -> dict[str, list[tuple]]:
    """Every row a signed or witnessed record is made of, as stored (text as bytes)."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        c.text_factory = bytes
        have = {r[0].decode() for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ids = [r[0] for r in c.execute("SELECT id FROM experiment WHERE status IN ('signed', 'witnessed', 'SIGNED', 'WITNESSED')")]
        q = ",".join("?" * len(ids)) or "NULL"
        out = {
            "experiment": c.execute(f"SELECT id, title, description, owner_id, status FROM experiment WHERE id IN ({q}) ORDER BY id", ids).fetchall(),
            "entry": c.execute(f"SELECT * FROM entry WHERE experiment_id IN ({q}) ORDER BY id", ids).fetchall(),
            "attachment": c.execute(f"SELECT * FROM attachment WHERE experiment_id IN ({q}) ORDER BY id", ids).fetchall(),
            "signature": c.execute(f"SELECT * FROM signature WHERE experiment_id IN ({q}) ORDER BY id", ids).fetchall(),
        }
        if "entryversion" in have:
            out["entryversion"] = c.execute(f"SELECT * FROM entryversion WHERE entry_id IN (SELECT id FROM entry WHERE experiment_id IN ({q})) "
                                            "ORDER BY id", ids).fetchall()
        return out


@pytest.mark.parametrize("name", chosen())
def test_signed_records_and_their_fingerprints_are_unchanged_by_the_upgrade(name, tmp_path):
    up = restore_and_upgrade(name, tmp_path)
    before, after = _signed_rows(up["old_db"]), _signed_rows(up["data"] / "benchtop.db")
    assert before["signature"], f"{name} has no signed records to check"
    for table, rows in before.items():
        # columns added by later versions come at the end: compare what was there
        got = [r[:len(rows[0])] for r in after[table]] if rows else after[table]
        assert got == rows, f"{table}: rows of signed records changed in the upgrade"
    r = run(["-c", HASHES], up["data"])
    assert r.returncode == 0, r.stderr
    hashes = json.loads(r.stdout.strip().splitlines()[-1])
    assert hashes, f"{name}: no signed records after the upgrade"
    wrong = {i: h for i, h in hashes.items() if h[0] != h[1]}
    assert not wrong, f"{name}: today's fingerprint differs from the one stored when signed: {wrong}"
