"""0.34: archival PDFs (PDF/A-3b) and the archive PDF of the records signed over a span of days.

Every PDF Benchtop makes is now PDF/A-3b: fonts embedded (DejaVu), an sRGB output intent, XMP metadata that says so,
and the record's own data and timestamp proofs attached inside it. The archive is one such PDF of the records
signed from one day to another (in the person's own calendar days), by notebook and author, only records the person may open, voided ones
marked, drafts left out; spectra drawn by the browser and uploaded to a job only its owner can see. Making either never
changes a record. veraPDF checks the files on GitHub (see .github/workflows/tests.yml); tests/pdfa_checks.py checks what
can be checked here."""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
import zipfile
from datetime import date, datetime

import pytest
from PIL import Image
from sqlmodel import Session, select

from app import archive
from app.db import engine
from app.models import Experiment, Notebook, Signature
from tests import pdfa_checks
from tests.fake_tsa import FakeTSA

PW = "lab-bench-pw-2026"
JDX = b"##TITLE=t\n##JCAMP-DX=4.24\n##DATA TYPE=NMR SPECTRUM\n##.OBSERVE NUCLEUS=^1H\n##XYDATA=(XY..XY)\n1.0, 2.0\n##END=\n"
GLYPHS = "CDCl₃ δ 7.26 (s, 1H), µL, ± 0.1 °C, A → B, C≡N, 1.5 Å, ¹H, ¹³C, 10⁻³ M, [M+H]⁺, H₂O"


@pytest.fixture()
def tsa(monkeypatch):
    from app import sitesettings, timestamps
    fake = FakeTSA()
    monkeypatch.delenv("BENCHTOP_TIMESTAMPS", raising=False)
    monkeypatch.setattr(timestamps, "synchronous", True)
    monkeypatch.setattr(timestamps, "transport", fake)
    monkeypatch.setattr(timestamps, "_skip_until", 0.0)
    sitesettings.update("timestamps", {"enabled": True, "tsa_url": "http://tsa.example.org"})
    yield fake
    sitesettings.update("timestamps", {"enabled": False})


def Y(year: int) -> str:
    """The query for a calendar year (UTC days)."""
    return f"from_date={year}-01-01&to_date={year}-12-31"


def J(year: int) -> dict:
    return {"from_date": f"{year}-01-01", "to_date": f"{year}-12-31"}


def record(client, h, nb, title="Suzuki 3a", *, spectrum=True, description=""):
    """A record with a characterisation section: a 1H NMR file (drawable) and a PDF (not a spectrum)."""
    exp = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"], "description": description}).json()
    exp = client.get(f"/api/experiments/{exp['id']}", headers=h).json()
    ents = {e["kind"]: e for e in exp["entries"]}
    items = [{"technique": "HRMS", "conditions": "ESI", "data": "found 185.0959"}]
    if spectrum:
        a1 = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("3a_1H.jdx", JDX)}).json()
        a2 = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("3a_report.pdf", b"%PDF-1.4 x")}).json()
        items = [{"technique": "1H NMR", "conditions": "400 MHz, CDCl3", "data": "δ 7.54 (d, 2H)", "attachment_id": a1["id"]},
                 {"technique": "HRMS", "conditions": "ESI", "data": "found 185.0959", "attachment_id": a2["id"]}]
    client.put(f"/api/entries/{ents['characterization']['id']}", headers=h, json={"content": {"items": items}})
    return client.get(f"/api/experiments/{exp['id']}", headers=h).json()


def sign(client, h, exp, comment=""):
    r = client.post(f"/api/experiments/{exp['id']}/sign", headers=h, json={"password": PW, "comment": comment})
    assert r.status_code == 200, r.text


def witness(client, h, exp):
    r = client.post(f"/api/experiments/{exp['id']}/witness", headers=h, json={"password": PW, "comment": ""})
    assert r.status_code == 200, r.text


def void(client, h, exp, reason="Contaminated starting material"):
    r = client.post(f"/api/experiments/{exp['id']}/void", headers=h, json={"password": PW, "comment": reason})
    assert r.status_code == 200, r.text


def dated(exp_id: int, when: str, meaning: str | None = None) -> None:
    """Move a record's signatures (all, or one meaning) to another date: test data for year boundaries."""
    with Session(engine) as s:
        for sig in s.exec(select(Signature).where(Signature.experiment_id == exp_id)):
            if meaning is None or sig.meaning == meaning:
                sig.signed_at = datetime.fromisoformat(when)
                s.add(sig)
        s.commit()


def fingerprint(client, h, exp) -> str:
    return client.get(f"/api/experiments/{exp['id']}/verify", headers=h).json()["content_hash"]


def png(w=1200, h=500, colour=(255, 255, 255, 0)) -> bytes:
    im = Image.new("RGBA", (w, h), colour)
    for x in range(min(100, w - 1), min(1100, w)):
        im.putpixel((x, (400 - (x % 300)) % h), (0, 0, 0, 255))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def wait(client, h, job_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/archive/jobs/{job_id}", headers=h).json()
        if j["state"] not in ("queued", "building"):
            return j
        time.sleep(0.2)
    raise AssertionError("the archive took too long")


# ====================================================================== every PDF is PDF/A-3b

def test_download_pdf_is_pdfa_with_the_record_inside(client, users, notebook, tsa):
    a, b = users["alice"], users["bob"]
    exp = record(client, a, notebook, "Glyphs " + GLYPHS[:20], description=GLYPHS + " and a smile 😀 and 中文")
    sign(client, a, exp, "done")
    witness(client, b, exp)
    before = fingerprint(client, a, exp)
    versions = {e["id"]: e["version"] for e in client.get(f"/api/experiments/{exp['id']}", headers=a).json()["entries"]}
    r = client.get(f"/api/experiments/{exp['id']}/export?format=pdf", headers=a)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    pdf = r.content
    assert pdfa_checks.problems(pdf) == []
    # the record's data, as format=json gives it, and each timestamp's proof
    files = pdfa_checks.attachments(pdf)
    assert set(files) == {f"{exp['eid']}.json"} | {f"{exp['eid']}-signature-{s['id']}-timestamp.zip"
                                                     for s in client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()["signatures"]}
    assert len(files) == 3
    data = json.loads(files[f"{exp['eid']}.json"])
    served = client.get(f"/api/experiments/{exp['id']}/export?format=json", headers=a).json()
    assert data["content_hash"] == served["content_hash"] == before
    assert data["snapshot"] == served["snapshot"] and [s["id"] for s in data["signatures"]] == [s["id"] for s in served["signatures"]]
    rel = pdfa_checks.relationships(pdf)
    assert rel[f"{exp['eid']}.json"] == "/Source" and all(v == "/Supplement" for k, v in rel.items() if k.endswith(".zip"))
    z = zipfile.ZipFile(io.BytesIO(next(v for k, v in files.items() if k.endswith(".zip"))))
    assert {"signature.json", "token.tst", "tsa-certs.pem", "README.txt"} <= set(z.namelist())
    assert json.loads(z.read("signature.json"))["content_sha256"] == before
    # every character chemists type is drawn from the embedded font (and can be copied back out of it)
    text = pdfa_checks.text(pdf)
    for want in ("CDCl₃", "δ 7.26", "µL", "± 0.1 °C", "A → B", "C≡N", "1.5 Å", "¹H", "¹³C", "10⁻³", "[M+H]⁺", "H₂O"):
        assert want in text, want
    assert "😀" not in text and "□" in text  # what the font hasn't got is shown as a box, never drawn with nothing
    # nothing about the record changed
    assert fingerprint(client, a, exp) == before
    assert {e["id"]: e["version"] for e in client.get(f"/api/experiments/{exp['id']}", headers=a).json()["entries"]} == versions


def test_the_printed_fingerprint_command_gives_the_records_fingerprint(client, users, notebook, tmp_path):
    a = users["alice"]
    exp = record(client, a, notebook, "Recompute me", description="Ünïcödé, CDCl₃, 1.50 mmol, 3.0e-5 M")
    sign(client, a, exp)
    files = pdfa_checks.attachments(client.get(f"/api/experiments/{exp['id']}/export?format=pdf", headers=a).content)
    (tmp_path / f"{exp['eid']}.json").write_bytes(files[f"{exp['eid']}.json"])
    cmd = archive.HOW_TO_RECOMPUTE.replace("EXP-00012.json", f"{exp['eid']}.json").replace("python3", "python3", 1)
    env = {**os.environ, "PATH": f"{os.path.dirname(os.path.realpath(__import__('sys').executable))}:{os.environ.get('PATH', '')}"}
    out = subprocess.run(["sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True, env=env, check=True).stdout.strip()
    assert out == fingerprint(client, a, exp)


def test_a_draft_pdf_is_pdfa_too(client, users, notebook):
    a = users["alice"]
    exp = record(client, a, notebook, "Draft only", spectrum=False)
    pdf = client.get(f"/api/experiments/{exp['id']}/export?format=pdf", headers=a).content
    assert pdfa_checks.problems(pdf) == []
    assert list(pdfa_checks.attachments(pdf)) == [f"{exp['eid']}.json"]


# ====================================================================== which records go in

def test_year_boundaries_order_and_what_goes_in(client, users, notebook, admin):
    a, b = users["alice"], users["bob"]
    nb = notebook["id"]
    early = record(client, a, notebook, "Signed on the last second of 2019", spectrum=False)
    first = record(client, a, notebook, "Signed on the first second of 2020", spectrum=False)
    last = record(client, a, notebook, "Signed on the last second of 2020", spectrum=False)
    later = record(client, a, notebook, "Signed on the first second of 2021", spectrum=False)
    mid = record(client, a, notebook, "Witnessed in June", spectrum=False)
    voided = record(client, a, notebook, "Voided after signing", spectrum=False)
    voided_draft = record(client, a, notebook, "Voided as a draft", spectrum=False)
    draft = record(client, a, notebook, "Never signed", spectrum=False)
    withdrawn = record(client, a, notebook, "Signed then withdrawn", spectrum=False)
    for e in (early, first, last, later, mid, voided, withdrawn):
        sign(client, a, e)
    witness(client, b, mid)
    void(client, a, voided, "Wrong lot of boronic acid")
    void(client, a, voided_draft, "Started in the wrong notebook")
    assert client.post(f"/api/experiments/{withdrawn['id']}/withdraw", headers=a, json={"password": PW, "comment": ""}).status_code == 200
    dated(early["id"], "2019-12-31T23:59:59")
    dated(first["id"], "2020-01-01T00:00:00")
    dated(last["id"], "2020-12-31T23:59:59")
    dated(later["id"], "2021-01-01T00:00:00")
    dated(mid["id"], "2020-06-01T10:00:00", "author")
    dated(mid["id"], "2020-06-03T10:00:00", "witness")
    dated(voided["id"], "2020-03-01T09:00:00", "author")
    dated(voided["id"], "2021-02-01T09:00:00", "void")  # placed by its signing, not its voiding
    dated(voided_draft["id"], "2020-04-01T09:00:00")
    dated(withdrawn["id"], "2020-05-01T09:00:00")

    p = client.get(f"/api/archive/preview?{Y(2020)}&notebook_id={nb}", headers=a).json()
    assert {k: p[k] for k in ("records", "witnessed", "awaiting_witness", "voided", "files", "scope", "title")} == {
        "records": 5, "witnessed": 1, "awaiting_witness": 2, "voided": 2, "files": 0,
        "scope": f"Notebook “{notebook['name']}”", "title": "Benchtop records 2020"}
    with Session(engine) as s:
        from app.models import User
        alice = s.exec(select(User).where(User.username == "alice")).one()
        rows = archive.select_records(s, alice, archive.Span.year(2020), nb)
        assert [e.id for e, _ in rows] == [first["id"], voided["id"], voided_draft["id"], mid["id"], last["id"]]
        d = dict((e.id, d) for e, d in rows)
        assert d[mid["id"]]["witness"] is not None and d[first["id"]]["witness"] is None
        assert d[voided_draft["id"]]["author"] is None and d[voided_draft["id"]]["void"].comment == "Started in the wrong notebook"
    assert client.get(f"/api/archive/preview?{Y(2019)}&notebook_id={nb}", headers=a).json()["records"] == 1
    assert client.get(f"/api/archive/preview?{Y(2021)}&notebook_id={nb}", headers=a).json()["records"] == 1
    opts = client.get("/api/archive/options", headers=a).json()
    assert {2019, 2020, 2021} <= {y["year"] for y in opts["years"]}
    assert nb in [n["id"] for n in opts["notebooks"]]

    # the PDF: the cover says what's left out, the contents list them in order, the voided ones say so
    with Session(engine) as s:
        alice = s.exec(select(User).where(User.username == "alice")).one()
        pdf, info = archive.build_pdf(s, alice, archive.Span.year(2020), nb, page_size="a4")
    assert pdfa_checks.problems(pdf) == []
    assert info["records"] == [first["eid"], voided["eid"], voided_draft["eid"], mid["eid"], last["eid"]]
    how = [p for t, p in pdfa_checks.outline(pdf) if t == "How to check this archive"][0]
    text = pdfa_checks.text(pdf, pages=range(how - 1))  # the last page's examples use a made-up record number
    assert "Drafts (records not signed)" in text and "Benchtop records 2020" in text
    assert draft["eid"] not in text and withdrawn["eid"] not in text and early["eid"] not in text and later["eid"] not in text
    assert text.count("VOIDED") >= 4 and "Wrong lot of boronic acid" in text and "Started in the wrong notebook" in text
    assert "awaiting witness" in text
    assert "Never signed by its author" in text
    contents = pdfa_checks.text(pdf, pages=[1])
    for e in (first, voided, voided_draft, mid, last):
        assert e["eid"] in contents and fingerprint(client, a, e)[:32] in contents
    assert contents.index(first["eid"]) < contents.index(voided["eid"]) < contents.index(mid["eid"]) < contents.index(last["eid"])
    # each record starts on the page the contents give, with a bookmark there
    marks = pdfa_checks.outline(pdf)
    assert [t for t, _ in marks][:2] == ["Cover", "Contents"] and marks[-1][0] == "How to check this archive"
    rec = {t.split(" ")[0]: p for t, p in marks[2:-1]}
    assert list(rec) == info["records"]
    for eid, page in rec.items():
        assert eid in pdfa_checks.text(pdf, pages=[page - 1])
        assert f"{eid} · page {page} of {info['pages']}" in pdfa_checks.text(pdf, pages=[page - 1]) or \
               f"{eid} (VOIDED) · page {page} of {info['pages']}" in pdfa_checks.text(pdf, pages=[page - 1])
    assert pdfa_checks.page_count(pdf) == info["pages"]
    assert "(VOIDED)" in [t for t, _ in marks if t.startswith(voided["eid"])][0]
    # every record's data is inside, with the fingerprint Benchtop reports
    files = pdfa_checks.attachments(pdf)
    for e in (first, voided, voided_draft, mid, last):
        assert json.loads(files[f"{e['eid']}.json"])["content_hash"] == fingerprint(client, a, e)
    assert len(files) == 5  # no timestamps here: one file each


def test_a_record_the_person_cant_open_is_never_in_it(client, users, admin):
    a, b = users["alice"], users["bob"]
    private = client.post("/api/notebooks", headers=b, json={"name": "Bob's private", "visibility": "private"}).json()
    hidden = record(client, b, private, "Bob's secret result", spectrum=False)
    sign(client, b, hidden)
    dated(hidden["id"], "2018-05-05T10:00:00")
    shared = record(client, b, private, "Shared with Alice", spectrum=False)
    sign(client, b, shared)
    dated(shared["id"], "2018-06-06T10:00:00")
    alice_id = client.get("/api/auth/me", headers=a).json()["id"]
    assert client.put(f"/api/experiments/{shared['id']}/shares/{alice_id}", headers=b, json={"permission": "view"}).status_code == 200
    bob_id = client.get("/api/auth/me", headers=b).json()["id"]
    p = client.get(f"/api/archive/preview?{Y(2018)}&author_id={bob_id}", headers=a).json()
    assert p["records"] == 1
    # bob and the admin see both
    assert client.get(f"/api/archive/preview?{Y(2018)}&author_id={bob_id}", headers=b).json()["records"] == 2
    assert client.get(f"/api/archive/preview?{Y(2018)}&author_id={bob_id}", headers=admin).json()["records"] == 2
    # choosing that notebook still gives alice only the record shared with her
    assert client.get(f"/api/archive/preview?{Y(2018)}&notebook_id={private['id']}", headers=a).json()["records"] == 1
    with Session(engine) as s:
        from app.models import User
        alice = s.exec(select(User).where(User.username == "alice")).one()
        pdf, info = archive.build_pdf(s, alice, archive.Span.year(2018), author_id=bob_id)
    assert info["records"] == [shared["eid"]]
    how = [p for t, p in pdfa_checks.outline(pdf) if t == "How to check this archive"][0]
    text = pdfa_checks.text(pdf, pages=range(how - 1))
    assert hidden["eid"] not in text and "Bob's secret result" not in text
    assert f"{hidden['eid']}.json" not in pdfa_checks.attachments(pdf)
    # nor can she start an archive that would hold it
    j = client.post("/api/archive/jobs", headers=a, json={**J(2018), "author_id": bob_id, "page_size": "letter"}).json()
    assert [s["eid"] for s in j["spectra"]] == [] and j["counts"]["records"] == 1
    client.delete(f"/api/archive/jobs/{j['id']}", headers=a)


# ====================================================================== the job: drawing, uploads, building

def test_job_end_to_end_with_uploaded_spectra(client, users, notebook, tsa):
    a, b, v = users["alice"], users["bob"], users["vic"]
    e1 = record(client, a, notebook, "Archive with spectra")
    e2 = record(client, a, notebook, "Archive, second record", spectrum=False)
    for e in (e1, e2):
        sign(client, a, e)
    witness(client, b, e1)
    dated(e1["id"], "2017-03-01T10:00:00")
    dated(e2["id"], "2017-04-01T10:00:00")
    before = {e["id"]: (fingerprint(client, a, e), len(client.get(f"/api/experiments/{e['id']}", headers=a).json()["attachments"]))
              for e in (e1, e2)}
    r = client.post("/api/archive/jobs", headers=a, json={**J(2017), "notebook_id": notebook["id"], "page_size": "a4"})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["state"] == "drawing" and job["counts"]["records"] == 2 and job["spectra_total"] == 2
    nmr, other = job["spectra"]
    assert nmr["filename"] == "3a_1H.jdx" and nmr["technique"] == "1H NMR" and other["filename"] == "3a_report.pdf"
    assert len(nmr["sha256"]) == 64 and not nmr["done"]
    url = f"/api/archive/jobs/{job['id']}"
    # only its owner sees, feeds or downloads it; to anyone else it doesn't exist
    for h in (b, v):
        assert client.get(url, headers=h).status_code == 404
        assert client.put(f"{url}/spectra/{nmr['attachment_id']}", headers=h, files={"file": ("x.png", png(), "image/png")}).status_code == 404
        assert client.post(f"{url}/spectra/{nmr['attachment_id']}/skip", headers=h, json={"reason": "x"}).status_code == 404
        assert client.post(f"{url}/build", headers=h).status_code == 404
        assert client.get(f"{url}/download", headers=h).status_code == 404
        assert client.delete(url, headers=h).status_code == 404
    assert client.get("/api/archive/jobs/0123456789abcdef", headers=a).status_code == 404
    assert client.get("/api/archive/jobs/..%2F..%2Fetc", headers=a).status_code in (404, 422)
    # uploads are checked: a file of this job, a real PNG, not too big
    assert client.put(f"{url}/spectra/999999", headers=a, files={"file": ("x.png", png(), "image/png")}).status_code == 404
    assert client.put(f"{url}/spectra/{nmr['attachment_id']}", headers=a, files={"file": ("x.png", b"GIF89a....", "image/png")}).status_code == 415
    assert client.put(f"{url}/spectra/{nmr['attachment_id']}", headers=a,
                      files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 100, "image/png")}).status_code == 415
    assert client.put(f"{url}/spectra/{nmr['attachment_id']}", headers=a,
                      files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"\0" * (archive.MAX_PNG_BYTES + 10), "image/png")}).status_code == 413
    assert client.put(f"{url}/spectra/{nmr['attachment_id']}", headers=a, files={"file": ("x.png", png(7000, 50), "image/png")}).status_code == 415
    r = client.put(f"{url}/spectra/{nmr['attachment_id']}", headers=a, files={"file": ("x.png", png(), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["spectra_done"] == 1 and r.json()["spectra_drawn"] == 1
    assert client.post(f"{url}/spectra/{other['attachment_id']}/skip", headers=a, json={"reason": "not a kind of file the spectrum viewer draws"}).json()["spectra_done"] == 2
    # reopening the page: the job, with what's drawn
    again = client.get("/api/archive/options", headers=a).json()["job"]
    assert again["id"] == job["id"] and all(s["done"] for s in again["spectra"])
    assert client.post(f"{url}/build", headers=a).status_code == 202
    assert client.put(f"{url}/spectra/{nmr['attachment_id']}", headers=a, files={"file": ("x.png", png(), "image/png")}).status_code == 409
    done = wait(client, a, job["id"])
    assert done["state"] == "done", done
    r = client.get(f"{url}/download", headers=a)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "Benchtop%20records%202017%20%E2%80%93%20Notebook%20NB.pdf" in r.headers["content-disposition"]
    pdf = r.content
    assert pdfa_checks.problems(pdf) == []
    assert pdfa_checks.page_count(pdf) == done["pages"]
    assert pdfa_checks.images(pdf, min_width=1000) == 1  # the uploaded spectrum, as it was drawn
    text = pdfa_checks.text(pdf)
    assert e1["eid"] in text and e2["eid"] in text
    assert "1H NMR (400 MHz, CDCl3) · 3a_1H.jdx" in text and nmr["sha256"] in text
    assert "Not drawn (not a kind of file the spectrum viewer draws)" in text and other["sha256"] in text
    files = pdfa_checks.attachments(pdf)
    assert json.loads(files[f"{e1['eid']}.json"])["content_hash"] == before[e1["id"]][0]
    assert sum(1 for k in files if k.startswith(e1["eid"]) and k.endswith("-timestamp.zip")) == 2  # author and witness
    assert sum(1 for k in files if k.startswith(e2["eid"]) and k.endswith("-timestamp.zip")) == 1
    # the pictures were only for the archive: no record gained a file, no fingerprint moved
    for e in (e1, e2):
        assert (fingerprint(client, a, e), len(client.get(f"/api/experiments/{e['id']}", headers=a).json()["attachments"])) == before[e["id"]]
    assert client.delete(url, headers=a).status_code == 204
    assert client.get(url, headers=a).status_code == 404


def test_a_job_open_when_benchtop_stopped_says_so_and_can_start_again(client, users, notebook):
    a = users["alice"]
    e = record(client, a, notebook, "Interrupted archive", spectrum=False)
    sign(client, a, e)
    dated(e["id"], "2016-02-02T10:00:00")
    for state in ("drawing", "building", "queued"):
        job = client.post("/api/archive/jobs", headers=a, json={**J(2016), "notebook_id": notebook["id"], "page_size": "letter"}).json()
        p = archive.scratch_dir() / job["id"] / "job.json"
        j = json.loads(p.read_text())
        j["state"], j["run"] = state, "an-earlier-run"  # as left by a Benchtop that has since stopped
        p.write_text(json.dumps(j))
        got = client.get(f"/api/archive/jobs/{job['id']}", headers=a).json()
        assert got["state"] == "stopped" and "Start it again" in got["error"]
        assert client.post(f"/api/archive/jobs/{job['id']}/build", headers=a).status_code == 409
    # starting again replaces it
    new = client.post("/api/archive/jobs", headers=a, json={**J(2016), "notebook_id": notebook["id"], "page_size": "letter"}).json()
    assert client.get(f"/api/archive/jobs/{job['id']}", headers=a).status_code == 404
    assert new["state"] == "drawing"
    assert client.post(f"/api/archive/jobs/{new['id']}/build", headers=a).status_code == 202
    assert wait(client, a, new["id"])["state"] == "done"


def test_jobs_are_cleaned_up_after_a_day(client, users, notebook):
    a = users["alice"]
    e = record(client, a, notebook, "Old archive", spectrum=False)
    sign(client, a, e)
    dated(e["id"], "2015-02-02T10:00:00")
    job = client.post("/api/archive/jobs", headers=a, json={**J(2015), "notebook_id": notebook["id"], "page_size": "letter"}).json()
    d = archive.scratch_dir() / job["id"]
    old = time.time() - archive.KEEP_SECONDS - 60
    os.utime(d / "job.json", (old, old))
    archive.sweep()
    assert not d.exists()
    assert client.get(f"/api/archive/jobs/{job['id']}", headers=a).status_code == 404


def test_odd_requests_get_plain_answers(client, users):
    a = users["alice"]
    assert client.get(f"/api/archive/preview?{Y(1800)}", headers=a).status_code == 422
    assert client.post("/api/archive/jobs", headers=a, json={**J(2016), "page_size": "a3"}).status_code == 422
    assert client.post("/api/archive/jobs", headers=a, json={**J(1999), "page_size": "a4"}).status_code == 422  # nothing signed then
    assert client.post("/api/archive/jobs", headers=a, json={**J(2016), "author_id": 999999, "page_size": "a4"}).status_code == 404
    assert client.get("/api/archive/options").status_code == 401


# ====================================================================== any span of days, in the person's own days

def test_a_week_in_vancouver_includes_its_first_and_last_seconds(client, users, notebook):
    """3–9 November 2014 in Vancouver (UTC−8): from 08:00 UTC on the 3rd to 08:00 UTC on the 10th."""
    a = users["alice"]
    before = record(client, a, notebook, "Sunday 2 November, 23:59:59 in Vancouver", spectrum=False)
    first = record(client, a, notebook, "Monday 3 November, 00:00:00 in Vancouver", spectrum=False)
    last = record(client, a, notebook, "Sunday 9 November, 23:59:59 in Vancouver", spectrum=False)
    after = record(client, a, notebook, "Monday 10 November, 00:00:00 in Vancouver", spectrum=False)
    for e, when in ((before, "2014-11-03T07:59:59"), (first, "2014-11-03T08:00:00"), (last, "2014-11-10T07:59:59"),
                    (after, "2014-11-10T08:00:00")):
        sign(client, a, e)
        dated(e["id"], when)
    week = {"from_date": "2014-11-03", "to_date": "2014-11-09", "start": "2014-11-03T08:00:00Z", "end": "2014-11-10T08:00:00Z",
            "tz": "America/Vancouver"}
    q = "&".join(f"{k}={v}" for k, v in week.items()) + f"&notebook_id={notebook['id']}"
    p = client.get(f"/api/archive/preview?{q}", headers=a).json()
    assert p["records"] == 2 and p["title"] == "Benchtop records, 3–9 November 2014"
    assert p["phrase"] == "from 3 November 2014 to 9 November 2014"
    # the same days in UTC hold a different pair
    utc = client.get(f"/api/archive/preview?from_date=2014-11-03&to_date=2014-11-09&notebook_id={notebook['id']}", headers=a).json()
    assert utc["records"] == 2 and utc["tz"] == "UTC"
    with Session(engine) as s:
        from app.models import User
        alice = s.exec(select(User).where(User.username == "alice")).one()
        span = archive.Span.make(date(2014, 11, 3), date(2014, 11, 9), datetime(2014, 11, 3, 8), datetime(2014, 11, 10, 8),
                                 "America/Vancouver")
        assert [e.id for e, _ in archive.select_records(s, alice, span, notebook["id"])] == [first["id"], last["id"]]
        utc_days = archive.Span.make(date(2014, 11, 3), date(2014, 11, 9))
        assert [e.id for e, _ in archive.select_records(s, alice, utc_days, notebook["id"])] == [before["id"], first["id"]]
        pdf, info = archive.build_pdf(s, alice, span, notebook["id"])
    assert info["records"] == [first["eid"], last["eid"]]
    cover = pdfa_checks.text(pdf, pages=[0])
    assert "Benchtop records, 3–9 November 2014" in cover and "from 3 November 2014 to 9 November 2014" in cover
    assert "America/Vancouver" in cover
    assert pdfa_checks.problems(pdf) == []
    job = client.post("/api/archive/jobs", headers=a, json={**week, "notebook_id": notebook["id"], "page_size": "letter"}).json()
    assert job["title"] == "Benchtop records, 3–9 November 2014" and job["counts"]["records"] == 2
    assert job["file_name"] == "Benchtop records, 3–9 November 2014 – Notebook NB.pdf"
    client.delete(f"/api/archive/jobs/{job['id']}", headers=a)


def test_from_after_to_and_an_empty_span_say_so_plainly(client, users, notebook):
    a = users["alice"]
    r = client.get("/api/archive/preview?from_date=2014-11-09&to_date=2014-11-03", headers=a)
    assert r.status_code == 422 and "“From” is later than “To”" in r.json()["detail"]
    r = client.post("/api/archive/jobs", headers=a, json={"from_date": "2014-11-09", "to_date": "2014-11-03", "page_size": "a4"})
    assert r.status_code == 422 and "“From” is later than “To”" in r.json()["detail"]
    empty = client.get(f"/api/archive/preview?from_date=1990-03-01&to_date=1990-03-07&notebook_id={notebook['id']}", headers=a).json()
    assert empty["records"] == 0
    r = client.post("/api/archive/jobs", headers=a, json={"from_date": "1990-03-01", "to_date": "1990-03-07", "page_size": "a4"})
    assert r.status_code == 422
    assert r.json()["detail"] == "No records you can open were signed from 1 March 1990 to 7 March 1990; nothing to archive"
    # start and end times that don't belong to the days chosen are refused, never used
    r = client.get("/api/archive/preview?from_date=2014-11-03&to_date=2014-11-09&start=2014-10-01T00:00:00Z&end=2014-11-10T08:00:00Z",
                   headers=a)
    assert r.status_code == 422
    r = client.get("/api/archive/preview?from_date=2014-11-03&to_date=2014-11-09&start=0001-01-01T00:00:00%2B14:00", headers=a)
    assert r.status_code == 422


def test_the_span_is_named_for_what_it_is():
    S, d = archive.Span.make, date
    assert S(d(2026, 1, 1), d(2026, 12, 31)).title() == "Benchtop records 2026"
    assert S(d(2026, 1, 1), d(2026, 11, 7)).title() == "Benchtop records, 1 January – 7 November 2026"
    assert S(d(2026, 11, 1), d(2026, 11, 30)).title() == "Benchtop records, November 2026"
    assert S(d(2024, 2, 1), d(2024, 2, 29)).label() == "February 2024"
    assert S(d(2026, 11, 3), d(2026, 11, 9)).label() == "3–9 November 2026"
    assert S(d(2026, 11, 3), d(2026, 11, 3)).label() == "3 November 2026"
    assert S(d(2026, 10, 27), d(2026, 11, 2)).label() == "27 October – 2 November 2026"
    assert S(d(2025, 12, 29), d(2026, 1, 4)).label() == "29 December 2025 – 4 January 2026"
    assert S(d(2026, 11, 3), d(2026, 11, 3)).phrase() == "on 3 November 2026"
    assert S(d(2026, 1, 1), d(2026, 12, 31)).phrase() == "in 2026"
    # a span across the change of clocks (Vancouver, 3 November 2024): its two ends are at different offsets from UTC.
    # The browser works the offsets out; past dates here, since time-zone rules change with the law.
    sp = S(d(2024, 10, 28), d(2024, 11, 10), datetime(2024, 10, 28, 7), datetime(2024, 11, 11, 8), "America/Vancouver")
    assert sp.holds(datetime(2024, 10, 28, 7)) and not sp.holds(datetime(2024, 10, 28, 6, 59, 59))
    assert sp.holds(datetime(2024, 11, 11, 7, 59, 59)) and not sp.holds(datetime(2024, 11, 11, 8))
    with pytest.raises(archive.ArchiveError):
        S(d(2026, 11, 9), d(2026, 11, 3))
