"""0.34: archival PDFs are optional at install time.

The PDF/A package (pikepdf) doesn't exist ready-made for every computer Benchtop runs on (an Intel Mac on an older
macOS, a new Python). The launcher installs it only as a ready-made package, and where there is none it says so in one
line and carries on; it tries again only when a requirements file changes. Without it, Benchtop still makes every PDF
(Download PDF, the Zenodo PDF and the archive PDF) as an ordinary PDF in the same embedded fonts, and nothing it makes
or shows claims PDF/A or attachments."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app import archive, pdfa
from app.db import engine
from tests import pdfa_checks
from tests.test_v34_archive import J, dated, record, sign, wait

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def no_pikepdf(monkeypatch):
    """As on a computer where the PDF/A package couldn't be installed: importing it fails."""
    monkeypatch.setitem(sys.modules, "pikepdf", None)
    assert not pdfa.available()
    yield


def test_without_the_pdfa_package_a_record_pdf_is_an_ordinary_pdf(client, users, notebook, no_pikepdf):
    a = users["alice"]
    exp = record(client, a, notebook, "Ordinary PDF: CDCl₃ δ 7.26", description="¹H, ¹³C, 10⁻³ M, A → B")
    sign(client, a, exp)
    r = client.get(f"/api/experiments/{exp['id']}/export?format=pdf", headers=a)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF-")
    pdf = r.content
    assert pdfa_checks.attachments(pdf) == {}
    found = pdfa_checks.problems(pdf)
    assert any("pdfaid" in p or "XMP" in p for p in found), "it mustn't claim to be PDF/A"
    assert not [p for p in found if "not embedded" in p or ".notdef" in p], "the fonts are embedded all the same"
    assert pdfa_checks.width_mismatches(pdf) == [], "and their widths match the embedded fonts, PDF/A or not"
    text = pdfa_checks.text(pdf)
    assert exp["eid"] in text and "CDCl₃" in text and "A → B" in text and "PDF/A" not in text


def test_without_the_pdfa_package_the_archive_is_made_and_says_where_the_proofs_are(client, users, notebook, no_pikepdf):
    a = users["alice"]
    e1 = record(client, a, notebook, "Archive without PDF/A, one")
    e2 = record(client, a, notebook, "Archive without PDF/A, two", spectrum=False)
    for e in (e1, e2):
        sign(client, a, e)
    dated(e1["id"], "2013-03-01T10:00:00")
    dated(e2["id"], "2013-04-01T10:00:00")
    opts = client.get("/api/archive/options", headers=a).json()
    assert opts["pdfa"] is False
    assert client.get("/api/archive/pdfa", headers=a).json() == {"available": False}
    job = client.post("/api/archive/jobs", headers=a, json={**J(2013), "notebook_id": notebook["id"], "page_size": "a4"}).json()
    nmr = job["spectra"][0]
    from tests.test_v34_archive import png
    assert client.put(f"/api/archive/jobs/{job['id']}/spectra/{nmr['attachment_id']}", headers=a,
                      files={"file": ("x.png", png(), "image/png")}).status_code == 200
    assert client.post(f"/api/archive/jobs/{job['id']}/build", headers=a).status_code == 202
    done = wait(client, a, job["id"])
    assert done["state"] == "done", done
    pdf = client.get(f"/api/archive/jobs/{job['id']}/download", headers=a).content
    assert pdf.startswith(b"%PDF-") and pdfa_checks.page_count(pdf) == done["pages"]
    assert pdfa_checks.attachments(pdf) == {}
    assert not [p for p in pdfa_checks.problems(pdf) if "not embedded" in p or ".notdef" in p]
    assert pdfa_checks.width_mismatches(pdf) == []
    assert pdfa_checks.images(pdf, min_width=1000) == 1
    marks = pdfa_checks.outline(pdf)
    assert [t for t, _ in marks][:2] == ["Cover", "Contents"] and marks[-1][0] == "How to check this archive"
    assert [t.split(" ")[0] for t, _ in marks[2:-1]] == [e1["eid"], e2["eid"]]
    text = pdfa_checks.text(pdf)
    assert "PDF/A-3" not in text and "attached to this file" not in text and "qpdf" not in text
    assert "the PDF/A package isn't installed there" in text
    assert "record.benchtop.json" in text
    # no trusted timestamps here: it says so, and doesn't explain how to check proofs that aren't there (0.36.1)
    assert "weren't timestamped" in " ".join(text.replace("’", "'").split()) and "openssl ts -verify" not in text
    assert f"{e1['eid']} · page" in text  # the footers
    client.delete(f"/api/archive/jobs/{job['id']}", headers=a)


def test_with_the_package_the_state_says_on(client, users):
    assert client.get("/api/archive/pdfa", headers=users["alice"]).json() == {"available": True}
    assert client.get("/api/archive/options", headers=users["alice"]).json()["pdfa"] is True


# ---------------------------------------------------------------- the launcher

STUB = """#!/bin/bash
# a stand-in for the environment's Python: notes what it's asked; pip can't install the PDF/A package if told so
echo "$*" >> "$STUB_LOG"
case "$*" in
  *requirements-pdfa.txt*) [ -f "$STUB_PDFA_FAILS" ] && exit 1; exit 0 ;;
esac
exit 0
"""


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_the_launcher_carries_on_when_the_pdfa_package_cant_be_installed(tmp_path):
    root = tmp_path / "benchtop"
    (root / "backend/.venv/bin").mkdir(parents=True)
    shutil.copy2(REPO / "benchtop", root / "benchtop")
    for f in ("requirements.txt", "requirements-pdfa.txt"):
        shutil.copy2(REPO / "backend" / f, root / "backend" / f)
    py = root / "backend/.venv/bin/python"
    py.write_text(STUB)
    py.chmod(0o755)
    log, fails = tmp_path / "python.log", tmp_path / "pdfa-fails"
    fails.write_text("")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "BENCHTOP_DATA_DIR": str(tmp_path / "data"),
           "STUB_LOG": str(log), "STUB_PDFA_FAILS": str(fails), "LANG": "C.UTF-8"}

    def run():
        log.write_text("")
        r = subprocess.run(["bash", str(root / "benchtop"), "check"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        return r, log.read_text()

    line = "PDFs will be ordinary PDFs on this computer: the PDF/A package isn't available for it."
    r, calls = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert line in r.stdout
    assert "-m app.check" in calls, "it went on to do what it was asked"
    pdfa_calls = [c for c in calls.splitlines() if "requirements-pdfa.txt" in c]
    assert len(pdfa_calls) == 1 and "--only-binary=:all:" in pdfa_calls[0], "only ready-made packages, never compiled"
    core = [c for c in calls.splitlines() if "requirements.txt" in c and "pdfa" not in c]
    assert len(core) == 1 and pdfa_calls[0] != core[0]
    # the next start doesn't try again
    r, calls = run()
    assert r.returncode == 0 and line not in r.stdout and "requirements-pdfa.txt" not in calls and "-m app.check" in calls
    # an update that changes the requirements tries again, and this time it works: nothing to say
    fails.unlink()
    (root / "backend/requirements-pdfa.txt").write_text((root / "backend/requirements-pdfa.txt").read_text() + "\n# changed\n")
    r, calls = run()
    assert r.returncode == 0 and line not in r.stdout and "requirements-pdfa.txt" in calls
    r, calls = run()
    assert "requirements-pdfa.txt" not in calls
