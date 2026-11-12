"""0.36: easier for biologists and pharmacologists.

A walk-through of 0.35.1 by a cell biologist and a pharmacologist found:
- a follow-up's description said "Results for EXP-00020 that came after it was signed", even when the follow-up was a
  colleague's assay of the batch: now "Follow-up to EXP-00020: <title>";
- every new record started with chemistry sections: "Without chemistry" starts with Aim, Procedure and Observations &
  results, and the person's last choice is remembered in their account;
- TIFF pictures (microscopes, gel imagers) were broken images in the browser and in report.html: the server makes a PNG
  to look at, display only, never stored with the record or signed;
- typing EXP-00020 in a record made a one-way link: EXP-00020 now lists the records that mention it, only those the
  person may open;
- a required plugin item holding 0 (a vehicle's concentration, passage 0) counted as empty, so the record couldn't be
  signed, and printed as "—": 0 is a value now;
- a .eln file said nothing about how to check it without Benchtop: HOW-TO-CHECK.txt, with a one-liner run here as
  printed;
- two example plugins, cell biology and pharmacology, ship switched off.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from tests.conftest import _login

PW = "lab-bench-pw-2026"
CASES = json.loads((Path(__file__).resolve().parents[2] / "frontend" / "tests" / "data" / "required-cases.json").read_text())["cases"]


def _exp(client, h, nb, title, **body):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"], **body})
    assert r.status_code == 201, r.text
    return client.get(f"/api/experiments/{r.json()['id']}", headers=h).json()


def _put(client, h, entry, content):
    r = client.put(f"/api/entries/{entry['id']}", headers=h, json={"content": content})
    assert r.status_code == 200, r.text
    return r.json()


def _text(client, h, exp, html, title="Aim"):
    e = next(x for x in client.get(f"/api/experiments/{exp['id']}", headers=h).json()["entries"] if x["title"] == title)
    return _put(client, h, e, {"html": html})


def _sign(client, h, exp):
    r = client.post(f"/api/experiments/{exp['id']}/sign", headers=h, json={"password": PW})
    assert r.status_code == 200, r.text
    return r.json()


def _user(client, admin, name):
    client.post("/api/users", headers=admin, json={"username": name, "password": PW, "must_change": False,
                                                   "full_name": name.title(), "role": "scientist"})
    return _login(client, name, PW)


# ====================================================================== 1. the follow-up says what it is, plainly

def test_a_follow_up_is_described_neutrally(client, users, notebook):
    a, b = users["alice"], users["bob"]
    kx = _exp(client, a, notebook, "KX-104: amide with cyclopropylamine")
    _sign(client, a, kx)
    nb_b = client.post("/api/notebooks", headers=b, json={"name": "Assays (v36)"}).json()
    f = client.post(f"/api/experiments/{kx['id']}/follow-up", headers=b, json={"notebook_id": nb_b["id"]}).json()
    got = client.get(f"/api/experiments/{f['id']}", headers=b).json()
    assert got["description"] == f"Follow-up to {kx['eid']}: KX-104: amide with cyclopropylamine"
    assert "came after it was signed" not in got["description"]
    assert got["follows"]["eid"] == kx["eid"]
    # the signed original doesn't change
    assert client.get(f"/api/experiments/{kx['id']}/verify", headers=a).json()["intact"] is True


# ====================================================================== 2. a start without chemistry

STANDARD = ["Aim", "Reaction", "Safety & risk assessment", "Procedure", "Work-up & purification", "Characterisation",
            "Observations & results"]


def test_without_chemistry_starts_with_the_text_sections_only(client, users, notebook):
    a = users["alice"]
    std = _exp(client, a, notebook, "Standard as before")
    assert [e["title"] for e in std["entries"]] == STANDARD, "the default changed"
    assert [e["title"] for e in _exp(client, a, notebook, "Said outright", sections="standard")["entries"]] == STANDARD
    plain = _exp(client, a, notebook, "Viability of HEK293T", sections="no_chemistry")
    assert [(e["kind"], e["title"]) for e in plain["entries"]] == [("text", "Aim"), ("text", "Procedure"), ("text", "Observations & results")]
    assert all(e["content"] == {"html": ""} for e in plain["entries"])
    # a template wins: it is what was chosen
    tpl = client.post("/api/experiments", headers=a, json={"title": "Western template", "is_template": True}).json()
    tpl_entries = client.get(f"/api/experiments/{tpl['id']}", headers=a).json()["entries"]
    from_tpl = _exp(client, a, notebook, "From the template", template_id=tpl["id"], sections="no_chemistry")
    assert [e["title"] for e in from_tpl["entries"]] == [e["title"] for e in tpl_entries]
    assert client.post("/api/experiments", headers=a, json={"title": "x", "notebook_id": notebook["id"], "sections": "biology"}).status_code == 422


def test_the_last_start_is_remembered_in_the_account(client, users):
    a = users["alice"]
    put = lambda v: client.patch("/api/auth/me", headers=a, json={"preferences": {"start_from": v}}).json()["preferences"].get("start_from")  # noqa: E731
    assert put("no_chemistry") == "no_chemistry"
    assert put(42) == 42, "a template's id"
    for bad in ("biology", "42", True, 0, -3, 1.5e300, None, [1]):
        assert put(bad) == 42, f"{bad!r} was taken"
    assert put("standard") == "standard"
    assert client.get("/api/auth/me", headers=a).json()["preferences"]["start_from"] == "standard"


# ====================================================================== 4. TIFF pictures

def _tiff(size=(640, 360), mode="RGB", pages=1, colour=(40, 120, 200)) -> bytes:
    if mode == "I;16":
        im = Image.new("I;16", size)
        im.putdata([(x * 37 + y * 11) % 3000 + 200 for y in range(size[1]) for x in range(size[0])])
        frames = [im]
    else:
        frames = [Image.new(mode, size, tuple((c + 30 * i) % 255 for c in colour)) for i in range(pages)]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=pages > 1, append_images=frames[1:], compression="tiff_lzw")
    return buf.getvalue()


def _upload(client, h, exp, name, data, ctype="image/tiff"):
    r = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": (name, data, ctype)})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_a_tiff_gets_a_png_to_look_at_and_the_file_is_unchanged(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Blot of HEK293T lysates", sections="no_chemistry")
    raw = _tiff((2400, 1200))
    att = _upload(client, a, exp, "blot.tif", raw)
    assert att["sha256"] == hashlib.sha256(raw).hexdigest()
    fe = client.post(f"/api/experiments/{exp['id']}/entries", headers=a, json={"kind": "file", "title": "Blot", "content": {"attachment_ids": [att["id"]]}}).json()
    _text(client, a, exp, f'<p>The blot:</p><p><img data-attachment-id="{att["id"]}" alt="blot.tif"></p>', "Procedure")
    before = client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()["content_hash"]

    r = client.get(f"/api/attachments/{att['id']}/preview", headers=a)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-pages"] == "1" and r.headers["x-scaled"] == "0"
    png = Image.open(io.BytesIO(r.content))
    assert png.format == "PNG" and max(png.size) <= 1600 and png.size == (1600, 800)

    # the record, the file and its fingerprint are as they were
    assert client.get(f"/api/attachments/{att['id']}", headers=a).content == raw
    assert client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()["content_hash"] == before
    signed = _sign(client, a, exp)
    sig_hash = signed["signatures"][-1]["content_hash"]
    assert sig_hash == before
    assert client.get(f"/api/attachments/{att['id']}/preview", headers=a).status_code == 200  # signed: still shown
    v = client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()
    assert v["intact"] is True and v["content_hash"] == sig_hash
    assert [x["sha256"] for x in client.get(f"/api/experiments/{exp['id']}", headers=a).json()["attachments"]] == [att["sha256"]]
    assert fe["content"]["attachment_ids"] == [att["id"]]

    # the report shows the picture (a PNG made for it); the PDF draws the TIFF itself
    report = client.get(f"/api/experiments/{exp['id']}/export", headers=a).text
    assert report.count("data:image/png;base64,") >= 2 and "data:image/tiff" not in report
    assert client.get(f"/api/experiments/{exp['id']}/export?format=pdf", headers=a).status_code == 200
    # the .eln: the TIFF itself in files/, unchanged, and a picture you can see in report.html
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content))
    tif = next(n for n in z.namelist() if n.endswith("/files/blot.tif"))
    assert hashlib.sha256(z.read(tif)).hexdigest() == att["sha256"]
    rep = z.read(next(n for n in z.namelist() if n.endswith("/report.html"))).decode()
    assert "data:image/png;base64," in rep and "data:image/tiff" not in rep


def test_16_bit_and_multi_page_tiffs(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Confocal stack", sections="no_chemistry")
    deep = _upload(client, a, exp, "dapi.tiff", _tiff((300, 200), mode="I;16"))
    r = client.get(f"/api/attachments/{deep['id']}/preview", headers=a)
    assert r.status_code == 200 and r.headers["x-scaled"] == "1"
    lo, hi = Image.open(io.BytesIO(r.content)).convert("L").getextrema()
    assert lo == 0 and hi == 255, "a 16-bit picture shown black or white"
    stack = _upload(client, a, exp, "stack.tif", _tiff((120, 80), pages=3))
    assert client.get(f"/api/attachments/{stack['id']}/preview", headers=a).headers["x-pages"] == "3"
    # a .tif sent as an unknown type is still a TIFF
    odd = _upload(client, a, exp, "scan.TIF", _tiff((90, 60), colour=(1, 2, 3)), "application/octet-stream")
    assert client.get(f"/api/attachments/{odd['id']}/preview", headers=a).status_code == 200


def test_a_broken_or_huge_tiff_gets_no_preview_said_plainly(client, users, notebook, monkeypatch):
    from app import previews
    a = users["alice"]
    exp = _exp(client, a, notebook, "Bad pictures", sections="no_chemistry")
    broken = _upload(client, a, exp, "cut-short.tif", b"II*\x00" + os.urandom(3000))
    r = client.get(f"/api/attachments/{broken['id']}/preview", headers=a)
    assert r.status_code == 422 and r.json()["detail"] == "No preview for this TIFF: it couldn't be read. Download it to view."
    big = _upload(client, a, exp, "slide-scan.tif", _tiff((400, 300), colour=(9, 9, 9)))
    monkeypatch.setattr(previews, "MAX_PIXELS", 100_000)
    r = client.get(f"/api/attachments/{big['id']}/preview", headers=a)
    assert r.status_code == 422 and "too large (400 × 300 pixels)" in r.json()["detail"]
    monkeypatch.setattr(previews, "MAX_FILE", 10)
    huge = _upload(client, a, exp, "huge.tif", _tiff((50, 50), colour=(7, 7, 7)))
    assert "too large" in client.get(f"/api/attachments/{huge['id']}/preview", headers=a).json()["detail"]
    # the report says so instead of breaking, and the file is downloadable as it was
    _text(client, a, exp, f'<p><img data-attachment-id="{broken["id"]}"></p>')
    assert "[image: cut-short.tif: no preview for TIFF]" in client.get(f"/api/experiments/{exp['id']}/export", headers=a).text
    assert client.get(f"/api/attachments/{broken['id']}", headers=a).status_code == 200


def test_previews_are_for_tiffs_and_for_people_who_may_open_the_record(client, users, admin):
    a, b = users["alice"], users["bob"]
    private = client.post("/api/notebooks", headers=a, json={"name": "Private blots (v36)", "visibility": "private"}).json()
    exp = _exp(client, a, private, "Private blot", sections="no_chemistry")
    tif = _upload(client, a, exp, "private.tif", _tiff((64, 64), colour=(5, 6, 7)))
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    png = _upload(client, a, exp, "photo.png", buf.getvalue(), "image/png")
    assert client.get(f"/api/attachments/{png['id']}/preview", headers=a).status_code == 415
    assert client.get(f"/api/attachments/{tif['id']}/preview", headers=b).status_code == 404
    assert client.get(f"/api/attachments/{tif['id']}/preview", headers=a).status_code == 200


# ====================================================================== 5. "Mentioned in"

def _mentions(client, h, exp, **params):
    r = client.get(f"/api/experiments/{exp['id']}/mentioned-in", headers=h, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_mentioned_in_lists_only_what_the_person_may_open(client, users, notebook):
    a, b = users["alice"], users["bob"]
    target = _exp(client, a, notebook, "KX-104 batch 2")
    eid = target["eid"]
    shared = client.post("/api/notebooks", headers=b, json={"name": "Bob's assays (v36)"}).json()
    hidden = client.post("/api/notebooks", headers=b, json={"name": "Bob's private (v36)", "visibility": "private"}).json()
    seen = _exp(client, b, shared, "Viability of KX-104")
    _text(client, b, seen, f"<p>Dose–response of the batch made in <b>{eid}</b>.</p>")
    secret = _exp(client, b, hidden, "Secret title: unpublished target")
    _text(client, b, secret, f"<p>Batch {eid}.</p>")

    m = _mentions(client, a, target)
    assert m["total"] == 1 and [x["eid"] for x in m["items"]] == [seen["eid"]]
    assert "Secret" not in json.dumps(m), "a private record's title leaked"
    assert m["items"][0]["title"] == "Viability of KX-104" and m["items"][0]["owner"]["username"] == "bob"
    # bob can open both of his, so he sees both
    assert {x["eid"] for x in _mentions(client, b, target)["items"]} == {seen["eid"], secret["eid"]}
    # someone who can't open the target can't ask about it
    private_target = _exp(client, b, hidden, "Bob's own private target")
    assert client.get(f"/api/experiments/{private_target['id']}/mentioned-in", headers=a).status_code == 404


def test_mentioned_in_matches_the_whole_number_only(client, users, notebook):
    a = users["alice"]
    target = _exp(client, a, notebook, "Whole-number target")
    eid = target["eid"]
    longer = eid + "7"            # EXP-001237: another record's number
    shorter = eid[:-1]            # EXP-0012: another record's number too
    for title, html in (("Longer number", f"<p>{longer}</p>"), ("Shorter number", f"<p>{shorter}</p>"),
                        ("Glued to a word", f"<p>see{eid}</p>"), ("Followed by letters", f"<p>{eid}b</p>")):
        _text(client, a, _exp(client, a, notebook, title), html)
    _text(client, a, target, f"<p>This is {eid} itself.</p>")
    plain = _exp(client, a, notebook, "Lower case and punctuation")
    _text(client, a, plain, f"<p>(batch {eid.lower()}).</p>")
    in_title = _exp(client, a, notebook, f"Repeat of {eid}")
    in_table = _exp(client, a, notebook, "In a data table")
    t = client.post(f"/api/experiments/{in_table['id']}/entries", headers=a, json={"kind": "table", "title": "Batches"}).json()
    _put(client, a, t, {"columns": [{"name": "Batch"}], "rows": [[eid]]})
    got = {x["eid"] for x in _mentions(client, a, target)["items"]}
    assert got == {plain["eid"], in_title["eid"], in_table["eid"]}


def test_mentioned_in_leaves_out_follow_ups_and_puts_voided_records_last(client, users, notebook):
    a = users["alice"]
    target = _exp(client, a, notebook, "Signed target")
    eid = target["eid"]
    first = _exp(client, a, notebook, "Mentions it first")
    _text(client, a, first, f"<p>{eid}</p>")
    voided = _exp(client, a, notebook, "Mentions it, then voided")
    _text(client, a, voided, f"<p>{eid}</p>")
    assert client.post(f"/api/experiments/{voided['id']}/void", headers=a, json={"password": PW, "comment": "wrong batch"}).status_code == 200
    later = _exp(client, a, notebook, "Mentions it later")
    _text(client, a, later, f"<p>{eid}</p>")
    _sign(client, a, target)
    fu = client.post(f"/api/experiments/{target['id']}/follow-up", headers=a, json={}).json()  # its title and description name it
    m = _mentions(client, a, target)
    assert [x["eid"] for x in m["items"]] == [later["eid"], first["eid"], voided["eid"]]
    assert m["items"][-1]["status"] == "voided" and m["total"] == 3
    assert fu["eid"] not in {x["eid"] for x in m["items"]}
    one = _mentions(client, a, target, limit=1)
    assert one["total"] == 3 and len(one["items"]) == 1


def test_mentioned_in_reads_what_was_typed_in_a_plugin_section_not_its_labels(client, users, notebook):
    a = users["alice"]
    target = _exp(client, a, notebook, "Plugin target")
    eid = target["eid"]
    schema = {"plugin": "test", "id": "assay", "name": "Assay", "title": "Assay", "description": f"about {eid}",
              "fields": [{"id": "batch", "label": f"Batch (e.g. {eid})", "type": "text", "placeholder": eid}], "tables": []}
    only_labels = _exp(client, a, notebook, "Labels only")
    r = client.post(f"/api/experiments/{only_labels['id']}/entries", headers=a,
                    json={"kind": "custom", "title": "Assay", "content": {"schema": schema, "values": {"batch": ""}}})
    assert r.status_code in (200, 201), r.text
    typed = _exp(client, a, notebook, "Typed in")
    r = client.post(f"/api/experiments/{typed['id']}/entries", headers=a,
                    json={"kind": "custom", "title": "Assay", "content": {"schema": schema, "values": {"batch": eid}}})
    assert r.status_code in (200, 201), r.text
    assert [x["eid"] for x in _mentions(client, a, target)["items"]] == [typed["eid"]]


# ====================================================================== 7. required items: the dialog's list is the server's

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_required_items_rule_is_the_one_the_sign_dialog_mirrors(case):
    """frontend/tests/beyond-chemistry.test.mjs checks src/signCheck.ts against the same file."""
    from app import plugins
    assert plugins.missing_required(case["content"]) == case["missing"]


def test_a_zero_is_a_value_when_signing_and_in_print(client, users, notebook, admin):
    a = users["alice"]
    assert client.post("/api/plugins/pharmacology/enabled", headers=admin, json={"enabled": True}).status_code == 200
    try:
        exp = _exp(client, a, notebook, "KX-104 dose–response", sections="no_chemistry")
        r = client.post(f"/api/experiments/{exp['id']}/entries", headers=a, json={"kind": "custom", "title": "Dose–response",
                                                                                 "plugin_section": "pharmacology/dose-response"})
        assert r.status_code in (200, 201), r.text
        sec = r.json()
        refused = client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW})
        assert refused.status_code == 422
        assert refused.json()["detail"] == ("Fill in the required items before signing — Dose–response: Compound, Batch, Assay, Result, "
                                            "Concentrations and responses (at least 3 rows)")
        c = sec["content"]
        c["values"].update({"compound": "KX-104", "batch": exp["eid"], "assay": "CellTiter-Glo, HEK293T, 72 h", "outcome": "IC50 fitted", "ic50": 0.41})
        c["rows"]["points"] = [{"conc": 0, "r1": 100, "r2": 98, "r3": 101, "norm": 100}, {"conc": 1, "r1": 52, "r2": 49, "r3": 50, "norm": 50.3},
                               {"conc": 10, "r1": 0, "r2": 1, "r3": 0, "norm": 0.3}]
        _put(client, a, sec, c)
        _sign(client, a, exp)  # 0.35.1: "Concentrations and responses: Concentration" (the vehicle's 0 counted as empty)
        report = client.get(f"/api/experiments/{exp['id']}/export", headers=a).text
        assert "<td>0.0</td><td>100.0</td><td>98.0</td><td>101.0</td><td>100.0</td>" in report, "a 0 printed as an empty cell"
        assert "<td>10.0</td><td>0.0</td><td>1.0</td><td>0.0</td><td>0.3</td>" in report
        md = client.get(f"/api/experiments/{exp['id']}/export?format=md", headers=a).text
        assert "Concentration 0.0" in md
    finally:
        client.post("/api/plugins/pharmacology/enabled", headers=admin, json={"enabled": False})


# ====================================================================== 8. HOW-TO-CHECK.txt in every .eln

def test_the_eln_says_how_to_check_it_and_its_one_liner_works_as_printed(client, users, notebook, tmp_path):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Ünïcödé check: CDCl₃, 1.50 mmol, 3.0e-5 M", description="µL ± 0.1 °C → 10⁻³ M")
    _text(client, a, exp, "<p>Stirred at −78 °C; [M+H]⁺ 185.0959; 0.1 µM, 1e-9.</p>")
    signed = _sign(client, a, exp)
    sig_hash = signed["signatures"][-1]["content_hash"]

    raw = client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content
    z = zipfile.ZipFile(io.BytesIO(raw))
    root = z.namelist()[0].split("/")[0]
    how = z.read(f"{root}/HOW-TO-CHECK.txt").decode()
    for words in ("record.benchtop.json", "report.html", "ro-crate-metadata.json", "files/"):
        assert words in how, words
    # signed without trusted timestamps: it says so, and explains openssl only where there is a token to check (0.36.1;
    # tests/test_v36_1_demo_fixes.py has a lab with them)
    assert "weren't timestamped" in how and "openssl ts -verify" not in how and "signatures/" not in how
    # listed as a file of the crate, with its fingerprint, after the records
    graph = {n["@id"]: n for n in json.loads(z.read(f"{root}/ro-crate-metadata.json"))["@graph"]}
    node = graph["./HOW-TO-CHECK.txt"]
    assert node["@type"] == "File" and node["encodingFormat"] == "text/plain"
    assert node["sha256"] == hashlib.sha256(how.encode()).hexdigest() and node["contentSize"] == str(len(how.encode()))
    parts = [p["@id"] for p in graph["./"]["hasPart"]]
    assert parts[-1] == "./HOW-TO-CHECK.txt" and graph[parts[0]]["@type"] == "Dataset"

    # run the one-liner exactly as printed, in the record's folder
    lines = how.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith('python3 -c "import json, hashlib, sys'))
    end = next(i for i in range(start, len(lines)) if lines[i].endswith("record.benchtop.json"))
    cmd = "\n".join(lines[start:end + 1])
    z.extractall(tmp_path)
    folder = next(p for p in (tmp_path / root).iterdir() if p.is_dir() and p.name.startswith(exp["eid"]))
    env = {**os.environ, "PATH": f"{os.path.dirname(os.path.realpath(sys.executable))}:{os.environ.get('PATH', '')}"}
    out = subprocess.run(["sh", "-c", cmd], cwd=folder, capture_output=True, text=True, env=env, check=True).stdout.strip()
    record = json.loads((folder / "record.benchtop.json").read_text(encoding="utf-8"))
    assert out == record["content_hash"] == sig_hash
    assert client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()["intact"] is True


def test_importing_a_036_eln_takes_the_records_only(client, users, notebook):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Round trip with the guide")
    raw = client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content
    p = client.post("/api/eln/preview", headers=a, files={"file": ("x.eln", raw, "application/zip")}).json()
    assert [i["title"] for i in p["items"]] == ["Round trip with the guide"]
    client.delete(f"/api/eln/import/{p['token']}", headers=a)


# ====================================================================== 9. the example plugins

def test_the_biology_examples_load_and_ship_switched_off(client, admin):
    from app import plugins
    ex = {p.id: p for p in plugins.discover() if p.bundled}
    for pid, sections in (("cell-biology", {"passage", "transfection", "western", "flow"}), ("pharmacology", {"dose-response", "pk"})):
        assert pid in ex and not ex[pid].errors, ex.get(pid)
        assert {s["id"] for s in ex[pid].sections} == sections
        assert ex[pid].author == "Benchtop examples"
    listed = {p["id"]: p for p in client.get("/api/plugins", headers=admin).json()["plugins"]}
    assert not listed["cell-biology"]["enabled"] and not listed["pharmacology"]["enabled"]
    keys = [s["key"] for s in client.get("/api/plugins/sections", headers=admin).json()]
    assert not any(k.startswith(("cell-biology/", "pharmacology/")) for k in keys)
    # nothing in them pretends to calculate, and an example number in a label would show as a mention
    text = "".join((plugins.BUNDLED_DIR / f"{pid}.toml").read_text() for pid in ("cell-biology", "pharmacology"))
    assert "EXP-0" not in text and "APLAC" not in text
    for us in ("color", "catalog ", "normaliz", "analyz", "BSL-2"):
        assert us not in text, us
