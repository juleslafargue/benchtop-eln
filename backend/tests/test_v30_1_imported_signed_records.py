"""0.30.1: a signed record brought in from another Benchtop holds exactly what was signed, and says so only when it does.

Found by the 0.30 stress test: an imported copy of a signed record went through the same tidying as anything typed
(reaction tables recalculated, an atom economy over 100 % dropped, empty conditions filled in), so it no longer held
what was signed, while its banner said "The content matches what was signed (checked on import)".

Now, when the file's content matches its signature, the sections are stored exactly as signed: only re-pointed at the
imported files (and away from the other lab's inventory, whose numbers mean nothing here). If something had to be
changed (rich text that isn't as Benchtop's cleaner leaves it, so it might not be safe to show; a section not in the
shape Benchtop writes), the copy says it isn't exactly what was signed and keeps the record as signed beside it, in
original-record.json. "Verify integrity" on the copy checks it against its fingerprint from the import, and says so."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from sqlmodel import Session

from tests.test_v20_eln import _entry, _import, _rich

PW = "lab-bench-pw-2026"


@pytest.fixture()
def nb2(client, users):
    return client.post("/api/notebooks", headers=users["bob"], json={"name": "Bob's signed imports"}).json()


def _as_older_benchtop_signed_it(exp):
    """The reaction section as 0.28 stored it: no empty conditions filled in, and the atom economy it showed then
    (0.29 drops values over 100 %). Signing then covers exactly this."""
    from app.db import engine
    from app.models import Entry
    with Session(engine) as s:
        e = s.get(Entry, _entry(exp, "reaction")["id"])
        c = json.loads(json.dumps(e.content))
        c.pop("conditions", None)
        c.setdefault("totals", {}).setdefault("metrics", {})["atom_economy_pct"] = 111.973
        c["totals"]["metrics"].pop("impossible", None)
        e.content = c
        s.add(e)
        s.commit()


def _record_file(z: zipfile.ZipFile) -> str:
    return next(n for n in z.namelist() if n.endswith("/record.benchtop.json"))


def _signed_export(client, a, notebook, title):
    exp = _rich(client, a, notebook, title)
    _as_older_benchtop_signed_it(exp)
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW}).status_code == 200
    data = client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content
    z = zipfile.ZipFile(io.BytesIO(data))
    return exp, data, json.loads(z.read(_record_file(z)))


def _remapped(rec, new):
    """What the signed sections should be in the copy: the snapshot, re-pointed at the copy's files (same SHA-256)."""
    from app.eln_import import _remap
    by_sha = {a["sha256"]: a["id"] for a in new["attachments"]}
    ids = {a["id"]: by_sha[a["sha256"]] for a in rec["attachments"]}
    return [(e["kind"], e["title"], _remap(e["content"], ids)) for e in rec["snapshot"]["entries"]]


def test_an_imported_signed_record_holds_exactly_what_was_signed(client, users, notebook, nb2):
    a, b = users["alice"], users["bob"]
    exp, data, rec = _signed_export(client, a, notebook, "Signed by 0.28, imported by 0.30.1")
    assert any(e["kind"] == "reaction" and "conditions" not in e["content"] for e in rec["snapshot"]["entries"])
    _, res = _import(client, b, data, nb2)
    new = client.get(f"/api/experiments/{res['created'][0]['id']}", headers=b).json()
    want = _remapped(rec, new)
    got = [(e["kind"], e["title"], e["content"]) for e in new["entries"]][:len(want)]
    for (wk, wt, wc), (gk, gt, gc) in zip(want, got):
        assert (gk, gt) == (wk, wt)
        assert json.dumps(gc) == json.dumps(wc), f"“{wt}” isn't what was signed"
    rxn = next(c for k, _, c in got if k == "reaction")
    assert "conditions" not in rxn and rxn["totals"]["metrics"]["atom_economy_pct"] == 111.973
    assert all(r.get("material_id") is None for r in rxn["rows"])  # the other lab's inventory: numbers that mean nothing here
    # and it says so, honestly
    o = new["origin"]
    assert o["signatures"][0]["matches"] is True and o["as_signed"] is True
    assert not any("original-record.json" in w for w in o["warnings"])
    v = client.get(f"/api/experiments/{new['id']}/verify", headers=b).json()
    assert v["against"] == "import" and v["intact"] is True
    # changed behind the app's back: the check says so
    from app.db import engine
    from app.models import Entry
    with Session(engine) as s:
        e = s.get(Entry, new["entries"][0]["id"])
        keep = e.content
        e.content = {**keep, "html": keep.get("html", "") + "<p>added later</p>"}
        s.add(e)
        s.commit()
        v = client.get(f"/api/experiments/{new['id']}/verify", headers=b).json()
        assert v["against"] == "import" and v["intact"] is False
        e.content = keep
        s.add(e)
        s.commit()
    assert client.get(f"/api/experiments/{new['id']}/verify", headers=b).json()["intact"] is True


def test_the_integrity_check_never_claims_a_signature_it_didnt_check(client, users, notebook):
    a = users["alice"]
    draft = client.post("/api/experiments", headers=a, json={"title": "Never signed", "notebook_id": notebook["id"]}).json()
    v = client.get(f"/api/experiments/{draft['id']}/verify", headers=a).json()
    assert v["against"] == "nothing" and v["intact"] is True
    assert client.post(f"/api/experiments/{draft['id']}/sign", headers=a, json={"password": PW}).status_code == 200
    v = client.get(f"/api/experiments/{draft['id']}/verify", headers=a).json()
    assert v["against"] == "signature" and v["intact"] is True
    assert client.post(f"/api/experiments/{draft['id']}/withdraw", headers=a, json={"password": PW}).status_code == 200
    assert client.get(f"/api/experiments/{draft['id']}/verify", headers=a).json()["against"] == "nothing"


def _crafted(data: bytes, change) -> bytes:
    """The export with its record changed, and the signature's fingerprint made to match again (anyone can: it's a
    SHA-256 of the content, a check, not a seal)."""
    from app.audit import canonical
    z = zipfile.ZipFile(io.BytesIO(data))
    name = _record_file(z)
    rec = json.loads(z.read(name))
    for part in (rec["entries"], rec["snapshot"]["entries"]):
        change(part)
    h = hashlib.sha256(canonical(rec["snapshot"]).encode()).hexdigest()
    for s in rec["signatures"]:
        s["content_hash"] = h
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as w:
        for i in z.infolist():
            w.writestr(i, json.dumps(rec).encode() if i.filename == name else z.read(i))
    return out.getvalue()


@pytest.mark.parametrize("what", ["unsafe rich text", "odd shape"])
def test_a_signed_section_that_has_to_be_changed_is_said_and_the_original_kept(client, users, notebook, nb2, what):
    a, b = users["alice"], users["bob"]
    _, data, _ = _signed_export(client, a, notebook, f"Signed, then {what}")

    def change(entries):
        for e in entries:
            if what == "unsafe rich text" and e["kind"] == "text" and e["title"] == "Aim":
                e["content"]["html"] = '<p onclick="steal()">Aim</p><script>steal()</script>'
            if what == "odd shape" and e["kind"] == "reaction":
                e["content"]["rows"] = "not a list of rows"
    crafted = _crafted(data, change)
    _, res = _import(client, b, crafted, nb2)
    new = client.get(f"/api/experiments/{res['created'][0]['id']}", headers=b).json()
    o = new["origin"]
    assert o["signatures"][0]["matches"] is True  # the file is consistent with itself…
    assert o["as_signed"] is False  # …but this copy isn't exactly it, and says so
    assert any("isn't exactly what was signed" in w and "original-record.json" in w for w in o["warnings"]), o["warnings"]
    if what == "unsafe rich text":
        h = next(e for e in new["entries"] if e["title"] == "Aim")["content"]["html"]
        assert "onclick" not in h and "<script" not in h and "Aim" in h
    else:
        assert next(e for e in new["entries"] if e["kind"] == "reaction")["content"]["rows"] == []
    kept = next(x for x in new["attachments"] if x["filename"] == "original-record.json")
    r = client.get(f"/api/attachments/{kept['id']}", headers=b)
    z = zipfile.ZipFile(io.BytesIO(crafted))
    assert r.status_code == 200 and r.content == z.read(_record_file(z))
    assert any(e["kind"] == "file" and kept["id"] in e["content"]["attachment_ids"] for e in new["entries"])
