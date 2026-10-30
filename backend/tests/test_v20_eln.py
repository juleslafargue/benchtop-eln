"""0.20: .eln import and export (the ELN Consortium's shared format).

- export: one zip folder, ro-crate-metadata.json following the format's rules, every file listed with a matching SHA-256
  and size, sharing rules respected (you only export what you can open), files sent back with "Not mine" left out
- round trip: a signed Benchtop record exported and imported again comes back with the same sections, tables, reaction
  and files; it arrives read-only ("locked"); its signature is shown as history and checked against the content
- tampering: a file changed after export is imported but flagged; the signature no longer matches
- safety: not a zip, no metadata, paths leaving the folder, encrypted members, zip bombs, script in the write-up
- real exports from eLabFTW and RSpace (from the consortium's examples, MIT licence, tests/data/eln)
- locked records: no edits, no signing, but follow-ups and voiding work
- duplicates, staging (expiry, other people's tokens), viewers, archived notebooks
- ORCID iDs: checked, normalised, used to name people in exports
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import unquote

import pytest

from app import eln
from app.config import settings

PW = "lab-bench-pw-2026"
DATA = Path(__file__).parent / "data" / "eln"


def _exp(client, h, nb, title="ELN round trip", **kw):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"], **kw})
    assert r.status_code == 201, r.text
    return client.get(f"/api/experiments/{r.json()['id']}", headers=h).json()


def _entry(exp, kind, n=0):
    return [e for e in exp["entries"] if e["kind"] == kind][n]


def _put(client, h, entry, content, title=None):
    body = {"content": content, "expected_version": entry["version"]}
    if title:
        body["title"] = title
    r = client.put(f"/api/entries/{entry['id']}", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _rich(client, h, nb, title="Suzuki coupling, 2 mmol"):
    """A realistic record: write-up with a pasted picture, reaction (linked to inventory), table, a spectrum
    attached to characterisation, a comment, tags."""
    exp = _exp(client, h, nb, title, tags=["suzuki", "scale-up"], description="Baseline conditions")
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00"
           b"\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
    pic = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("tlc.png", png, "image/png")}).json()
    jdx = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h,
                      files={"file": ("product_1H.jdx", b"##TITLE=product\n##JCAMP-DX=5.01\n##END=\n", "chemical/x-jcamp-dx")}).json()
    _put(client, h, _entry(exp, "text"), {"html": f'<p>Degas, then heat. <strong>Watch</strong> the colour.</p>'
                                                 f'<img data-attachment-id="{pic["id"]}" alt="tlc.png">'})
    _put(client, h, _entry(exp, "reaction"), {"reaction_smiles": "Brc1ccccc1.OB(O)c1ccccc1>>c1ccc(-c2ccccc2)cc1",
                                              "conditions": {"temperature_c": 80, "time_h": 4, "atmosphere": "N2"},
                                              "rows": [{"role": "reactant", "name": "Bromobenzene", "smiles": "Brc1ccccc1", "equiv": 1, "mass_mg": 314,
                                                        "limiting": True, "basis": "mass", "material_id": 1, "lot_id": 1},
                                                       {"role": "reactant", "name": "Phenylboronic acid", "smiles": "OB(O)c1ccccc1", "equiv": 1.2},
                                                       {"role": "product", "name": "Biphenyl", "smiles": "c1ccc(-c2ccccc2)cc1", "actual_mass_mg": 270}]})
    _put(client, h, _entry(exp, "characterization"), {"items": [{"technique": "1H NMR", "conditions": "CDCl3, 400 MHz",
                                                                  "data": "δ 7.60 (d, 4H)", "attachment_id": jdx["id"]}]})
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=h,
                    json={"kind": "table", "title": "HPLC", "content": {"columns": [{"name": "t (h)"}, {"name": "Conv. (%)"}],
                                                                        "rows": [["1", "40"], ["4", "98"]]}})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=h,  # 0.21: cited papers become ScholarlyArticle nodes
                    json={"kind": "references", "title": "References", "content": {"items": [
                        {"doi": "10.1021/jo971176v", "title": "NMR Chemical Shifts of Common Laboratory Solvents as Trace Impurities",
                         "authors": ["Gottlieb, Hugo E.", "Kotlyar, Vadim", "Nudelman, Abraham"], "journal": "J. Org. Chem.",
                         "year": "1997", "volume": "62", "issue": "21", "pages": "7512-7515"},
                        {"title": "Unpublished results", "authors": ["Patel, P."]}]}})
    assert r.status_code == 201, r.text
    client.post(f"/api/experiments/{exp['id']}/comments", headers=h, json={"body": "Pd black after 3 h"})
    return client.get(f"/api/experiments/{exp['id']}", headers=h).json()


def _download(client, h, url) -> zipfile.ZipFile:
    r = client.get(url, headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.eln+zip")
    return zipfile.ZipFile(io.BytesIO(r.content))


def _graph(z: zipfile.ZipFile) -> tuple[str, dict[str, dict]]:
    roots = {n.split("/")[0] for n in z.namelist()}
    assert len(roots) == 1, roots
    root = roots.pop()
    g = json.loads(z.read(f"{root}/ro-crate-metadata.json"))
    return root, {n["@id"]: n for n in g["@graph"]}


def _preview(client, h, data: bytes, name="export.eln"):
    return client.post("/api/eln/preview", headers=h, files={"file": (name, data, "application/zip")})


def _import(client, h, data: bytes, nb, keys=None, name="export.eln"):
    r = _preview(client, h, data, name)
    assert r.status_code == 200, r.text
    p = r.json()
    r2 = client.post(f"/api/eln/import/{p['token']}", headers=h,
                     json={"notebook_id": nb["id"], "items": keys if keys is not None else [i["key"] for i in p["items"]]})
    assert r2.status_code == 200, r2.text
    return p, r2.json()


@pytest.fixture()
def nb2(client, users):
    return client.post("/api/notebooks", headers=users["bob"], json={"name": "Bob's imports"}).json()


# ---------------------------------------------------------------------------------------------------- export


def test_export_follows_the_format_rules(client, users, notebook):
    h = users["alice"]
    exp = _rich(client, h, notebook)
    z = _download(client, h, f"/api/experiments/{exp['id']}/export.eln")
    root, by = _graph(z)
    desc, top = by["ro-crate-metadata.json"], by["./"]
    assert desc["conformsTo"] == {"@id": eln.CRATE} and desc["sdPublisher"] == {"@id": eln.PUBLISHER}
    assert {"@id": eln.ELN_SPEC} in top["conformsTo"] and by[eln.ELN_SPEC]["@type"] == ["CreativeWork", "Profile"]
    assert by[eln.PUBLISHER]["@type"] == "Organization"
    assert f"{root}/ro-crate-preview.html" in z.namelist()
    ds = by[top["hasPart"][0]["@id"]]
    assert ds["@type"] == "Dataset" and ds["identifier"] == exp["eid"] and ds["name"] == exp["title"]
    assert ds["keywords"] == "suzuki, scale-up" and isinstance(ds["keywords"], str)
    assert "Degas, then heat" in ds["text"] and "base64" not in ds["text"] and "[picture: tlc.png]" in ds["text"]
    assert by[ds["author"]["@id"]]["@type"] == "Person"
    assert all(by[c["@id"]]["@type"] == "Comment" for c in ds["comment"]) and by[ds["comment"][0]["@id"]]["text"] == "Pd black after 3 h"
    pvs = {by[v["@id"]]["propertyID"]: by[v["@id"]] for v in ds["variableMeasured"]}
    assert pvs["temperature"]["value"] == 80 and pvs["temperature"]["unitText"] == "°C" and pvs["status"]["value"] == "open"
    mols = [by[m["@id"]] for m in ds["mentions"] if by[m["@id"]]["@type"] == "MolecularEntity"]
    assert {m["smiles"] for m in mols} == {"Brc1ccccc1", "OB(O)c1ccccc1", "c1ccc(-c2ccccc2)cc1"}
    files = [by[p["@id"]] for p in ds["hasPart"]]
    names = {f["name"] for f in files}
    assert {"report.html", "record.benchtop.json", "tlc.png", "product_1H.jdx", "reaction.rxn"} <= names
    assert sum(1 for f in files if f["encodingFormat"] == "chemical/x-mdl-molfile") == 3
    for f in files:  # every file is in the zip, at its @id, with the size and SHA-256 the metadata gives
        data = z.read(f"{root}/{unquote(f['@id'][2:])}")
        assert f["@type"] == "File" and f["contentSize"] == str(len(data)) and f["sha256"] == hashlib.sha256(data).hexdigest()
    for node in by.values():  # no empty values (the format's checker warns), references only as {"@id": …}
        assert all(v not in (None, "", [], {}) for v in node.values()), node["@id"]
        for k in ("author", "comment", "hasPart", "mentions", "variableMeasured", "citation"):
            for ref in (node.get(k) if isinstance(node.get(k), list) else [node[k]] if k in node else []):
                assert set(ref) == {"@id"} and ref["@id"] in by, (node["@id"], k, ref)
    if os.environ.get("BENCHTOP_ELN_SAMPLE"):  # CI runs the consortium's own checker on this file (deploy/github-tests.yml)
        out = Path(os.environ["BENCHTOP_ELN_SAMPLE"]) / f"{root}.eln"  # named like its folder, as the format asks
        out.write_bytes(client.get(f"/api/experiments/{exp['id']}/export.eln", headers=h).content)


def test_every_export_states_its_licence_all_rights_reserved(client, users, notebook):
    """RO-Crate 1.2 requires the root to have a licence (the consortium's checker enforces it since roc-validator 0.13.1).
    An ordinary export grants none; only one made for Zenodo carries an open licence (test_v28_zenodo)."""
    h = users["alice"]
    exp = _exp(client, h, notebook, "Licence check")
    for url in (f"/api/experiments/{exp['id']}/export.eln", f"/api/notebooks/{notebook['id']}/export.eln", "/api/export.eln"):
        _, by = _graph(_download(client, h, url))
        assert by["./"]["license"] == {"@id": "#licence"}, url
        assert by["#licence"] == {"@id": "#licence", "@type": "CreativeWork", "name": "No licence granted: all rights reserved",
                                  "description": "No licence is granted. All rights are reserved by the authors and their "
                                                 "institution; ask the authors before reusing."}
        assert not any("license" in n for i, n in by.items() if i != "./")  # the experiments inherit it from the root


def test_imports_with_or_without_a_licence(client, users, notebook, nb2):
    """Import never depends on the licence: Benchtop's own exports have one, older ones and other ELNs' may not."""
    a, b = users["alice"], users["bob"]
    exp = _exp(client, a, notebook, "Licensed round trip")
    data = client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content
    _, res = _import(client, b, data, nb2, name="licensed.eln")
    assert not res["failed"] and [c["title"] for c in res["created"]] == ["Licensed round trip"]
    nodes = json.loads(zipfile.ZipFile(io.BytesIO(_simple())).read("export/ro-crate-metadata.json"))["@graph"][2:]
    assert nodes[0]["@id"] == "./" and "license" not in nodes[0]
    # none at all, a plain URL, a reference to an entity that isn't there
    for licence in (None, "https://creativecommons.org/licenses/by/4.0/", {"@id": "#nowhere"}):
        root = nodes[0] if licence is None else {**nodes[0], "license": licence}
        _, res = _import(client, b, _crate([root, *nodes[1:]], {"exp/trace.csv": b"time,signal\n0,1\n"}), nb2)
        assert not res["failed"] and [c["title"] for c in res["created"]] == ["Grignard"], licence


def test_export_respects_sharing_and_leaves_out_files_sent_back(client, users, notebook):
    a, b = users["alice"], users["bob"]
    private = client.post("/api/notebooks", headers=a, json={"name": "Alice private", "visibility": "private"}).json()
    secret = _exp(client, a, private, "Secret route")
    open_exp = _exp(client, a, notebook, "Open to the lab")
    assert client.get(f"/api/experiments/{secret['id']}/export.eln", headers=b).status_code == 404
    assert client.get(f"/api/notebooks/{private['id']}/export.eln", headers=b).status_code == 404
    _, by = _graph(_download(client, b, "/api/export.eln"))
    ids = {n.get("identifier") for n in by.values()}
    assert open_exp["eid"] in ids and secret["eid"] not in ids
    _, by = _graph(_download(client, a, "/api/export.eln"))
    assert secret["eid"] in {n.get("identifier") for n in by.values()}
    _, by = _graph(_download(client, a, f"/api/notebooks/{notebook['id']}/export.eln"))
    assert open_exp["eid"] in {n.get("identifier") for n in by.values()} and secret["eid"] not in {n.get("identifier") for n in by.values()}
    # a file sent back with "Not mine" isn't part of the record, so it isn't exported
    att = client.post(f"/api/experiments/{open_exp['id']}/attachments", headers=a, files={"file": ("not-ours.csv", b"x,y\n1,2\n")}).json()
    from sqlmodel import Session

    from app.db import engine
    from app.models import Attachment, utcnow
    with Session(engine) as s:
        row = s.get(Attachment, att["id"])
        row.withdrawn_at = utcnow()
        s.add(row)
        s.commit()
    z = _download(client, a, f"/api/experiments/{open_exp['id']}/export.eln")
    assert not any(n.endswith("not-ours.csv") for n in z.namelist())


def test_export_is_in_the_audit_trail(client, users, notebook):
    h = users["alice"]
    exp = _exp(client, h, notebook, "Audited export")
    _download(client, h, f"/api/experiments/{exp['id']}/export.eln")
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import AuditEvent
    with Session(engine) as s:
        ev = s.exec(select(AuditEvent).where(AuditEvent.action == "export_eln").order_by(AuditEvent.id.desc())).first()
    assert ev is not None and exp["eid"] in json.dumps(ev.details)


# ---------------------------------------------------------------------------------------------------- round trip


def test_signed_record_comes_back_identical_read_only_with_its_signature_checked(client, users, notebook, nb2):
    a, b = users["alice"], users["bob"]
    exp = _rich(client, a, notebook)
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW, "comment": "done"}).status_code == 200
    data = client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content

    p, res = _import(client, b, data, nb2, name="alice.eln")
    item = p["items"][0]
    assert p["source"] == "Benchtop ELN" and item["from_benchtop"] and item["locked"] and "signed" in item["lock_reason"]
    alice = client.get("/api/auth/me", headers=a).json()["full_name"]  # other tests may have renamed her
    assert item["files"] == 2 and item["authors"] == [alice]
    new = client.get(f"/api/experiments/{res['created'][0]['id']}", headers=b).json()
    assert new["status"] == "locked" and new["owner"]["username"] == "bob" and new["eid"] != exp["eid"]
    assert new["title"] == exp["title"] and new["tags"] == exp["tags"] and new["description"] == exp["description"]
    o = new["origin"]
    assert o["original_id"] == exp["eid"] and o["source"] == "Benchtop ELN" and o["files"] == {"imported": 2, "matched": 2, "mismatched": 0}
    assert len(o["signatures"]) == 1 and o["signatures"][0]["matches"] and o["signatures"][0]["signer"] == alice
    assert new["signatures"] == []  # history, never a Benchtop signature

    src = client.get(f"/api/experiments/{exp['id']}", headers=a).json()
    sha_new = {x["id"]: x["sha256"] for x in new["attachments"]}
    sha_old = {x["id"]: x["sha256"] for x in src["attachments"]}
    assert sorted(sha_new.values()) == sorted(sha_old.values())
    old_sections = [(e["kind"], e["title"]) for e in src["entries"]]
    assert [(e["kind"], e["title"]) for e in new["entries"]] == old_sections + [("text", "Comments from Benchtop ELN")]
    for o_e, n_e in zip(src["entries"], new["entries"]):
        oc, nc = json.loads(json.dumps(o_e["content"])), json.loads(json.dumps(n_e["content"]))
        if o_e["kind"] == "reaction":  # the other lab's inventory isn't ours: those links are dropped, nothing else
            assert oc["rows"][0]["material_id"] == 1 and nc["rows"][0]["material_id"] is None and nc["rows"][0]["lot_id"] is None
            for r in oc["rows"] + nc["rows"]:
                r.pop("material_id", None), r.pop("lot_id", None)
        if o_e["kind"] == "characterization":  # re-pointed at the same spectrum in the new record
            assert sha_new[nc["items"][0]["attachment_id"]] == sha_old[oc["items"][0]["attachment_id"]]
            oc["items"][0]["attachment_id"] = nc["items"][0]["attachment_id"] = None
        if o_e["kind"] == "text" and "data-attachment-id" in oc["html"]:
            import re
            old_id = int(re.search(r'data-attachment-id="(\d+)"', oc["html"]).group(1))
            new_id = int(re.search(r'data-attachment-id="(\d+)"', nc["html"]).group(1))
            assert sha_new[new_id] == sha_old[old_id]
            oc["html"] = nc["html"] = ""
        assert oc == nc, o_e["title"]
    assert "Pd black after 3 h" in new["entries"][-1]["content"]["html"]


def test_draft_comes_back_editable_and_a_follow_up_links_again(client, users, notebook, nb2):
    a, b = users["alice"], users["bob"]
    first = _exp(client, a, notebook, "Step 1")
    client.post(f"/api/experiments/{first['id']}/sign", headers=a, json={"password": PW})
    r = client.post(f"/api/experiments/{first['id']}/follow-up", headers=a, json={})
    assert r.status_code == 201, r.text
    fu = client.get(f"/api/experiments/{r.json()['id']}", headers=a).json()
    data = client.get(f"/api/notebooks/{notebook['id']}/export.eln", headers=a).content
    p = _preview(client, b, data).json()
    keys = [i["key"] for i in p["items"] if i["identifier"] in (first["eid"], fu["eid"])]
    res = client.post(f"/api/eln/import/{p['token']}", headers=b, json={"notebook_id": nb2["id"], "items": keys}).json()
    by_title = {c["title"]: c for c in res["created"]}
    new_first = client.get(f"/api/experiments/{by_title['Step 1']['id']}", headers=b).json()
    new_fu = client.get(f"/api/experiments/{by_title[fu['title']]['id']}", headers=b).json()
    assert new_first["status"] == "locked" and new_fu["status"] == "open" and new_fu["can_edit"]
    assert new_fu["follows"]["id"] == new_first["id"]
    text = _entry(new_fu, "text")
    _put(client, b, text, {"html": "<p>carrying on</p>"})


def test_a_file_changed_after_export_is_flagged_and_the_signature_no_longer_matches(client, users, notebook, nb2):
    a, b = users["alice"], users["bob"]
    exp = _rich(client, a, notebook, "Tamper target")
    client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW})
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as w:
        for i in z.infolist():
            data = z.read(i)
            if i.filename.endswith("product_1H.jdx"):
                data = data.replace(b"product", b"PRODUCT")  # same size, different content
            w.writestr(i, data)
    _, res = _import(client, b, out.getvalue(), nb2)
    new = client.get(f"/api/experiments/{res['created'][0]['id']}", headers=b).json()
    o = new["origin"]
    assert o["files"]["mismatched"] == 1 and any("didn't match" in w for w in o["warnings"])
    assert o["signatures"][0]["matches"] is False and res["created"][0]["warnings"]


# ---------------------------------------------------------------------------------------------------- locked records


def test_locked_records_are_read_only_but_can_be_followed_up_and_voided(client, users, notebook, nb2):
    a, b = users["alice"], users["bob"]
    exp = _exp(client, a, notebook, "Signed elsewhere")
    client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW})
    _, res = _import(client, b, client.get(f"/api/experiments/{exp['id']}/export.eln", headers=a).content, nb2)
    new = client.get(f"/api/experiments/{res['created'][0]['id']}", headers=b).json()
    e = new["entries"][0]
    assert client.put(f"/api/entries/{e['id']}", headers=b, json={"content": {"html": "<p>x</p>"}, "expected_version": e["version"]}).status_code == 409
    assert client.post(f"/api/experiments/{new['id']}/entries", headers=b, json={"kind": "text"}).status_code == 409
    assert client.post(f"/api/experiments/{new['id']}/attachments", headers=b, files={"file": ("x.txt", b"x")}).status_code == 409
    assert client.post(f"/api/experiments/{new['id']}/sign", headers=b, json={"password": PW}).status_code == 409
    fu = client.post(f"/api/experiments/{new['id']}/follow-up", headers=b, json={})
    assert fu.status_code == 201 and client.get(f"/api/experiments/{fu.json()['id']}", headers=b).json()["status"] == "open"
    assert client.post(f"/api/experiments/{new['id']}/void", headers=b, json={"password": PW, "comment": "duplicate import"}).status_code == 200
    # and it exports as locked again, so the next system knows too
    _, by = _graph(_download(client, b, f"/api/experiments/{fu.json()['id']}/export.eln"))
    assert any(n.get("creativeWorkStatus") == "open" for n in by.values())


def test_the_database_step_that_brings_in_locked(client):
    from app import migrations
    from app.db import engine
    assert migrations.SCHEMA_VERSION >= 4 and migrations.current_version(engine) == migrations.SCHEMA_VERSION


# ---------------------------------------------------------------------------------------------------- other ELNs


def test_elabftw_export_imports_with_write_up_fields_steps_comments_and_its_picture(client, users, nb2):
    b = users["bob"]
    p, res = _import(client, b, (DATA / "elabftw-export.eln").read_bytes(), nb2, name="export.eln")
    assert p["source"] == "eLabFTW" and len(p["items"]) == 12 and not res["failed"] and len(res["created"]) == 12
    gold = next(c for c in res["created"] if c["title"] == "Gold master experiment")
    e = client.get(f"/api/experiments/{gold['id']}", headers=b).json()
    assert e["status"] == "open" and "generated from yml" in e["tags"] and e["origin"]["authors"] == ["Nicola Mohr"]
    assert not any(i["warnings"] for i in p["items"]) and e["origin"]["files"]["matched"] == 1  # found where it's listed, and checked
    titles = [x["title"] for x in e["entries"]]
    assert titles[:4] == ["Notes from eLabFTW", "Steps", "Details from eLabFTW", "Comments from eLabFTW"]
    notes = e["entries"][0]["content"]["html"]
    pic = next(x for x in e["attachments"] if x["filename"] == "example.jpg")  # eLabFTW writes "folder//example.jpg"
    assert f'data-attachment-id="{pic["id"]}"' in notes and "<table>" in notes
    rows = {r[0]: r for r in e["entries"][2]["content"]["rows"]}
    assert rows["Number with units"][1:] == ["12", "mM"] and rows["Original link"][1].startswith("https://elab.local")
    assert "elabftw_metadata" not in json.dumps(rows)  # a whole JSON blob stays in original-record.json
    assert "✓ a step" in e["entries"][1]["content"]["html"]
    assert "DNA extraction" in e["entries"][3]["content"]["html"]
    assert any(x["filename"] == "original-record.json" for x in e["attachments"])


def test_rspace_export_without_names_or_trailing_slashes_still_imports(client, users, nb2):
    b = users["bob"]
    p, res = _import(client, b, (DATA / "rspace-export.eln").read_bytes(), nb2, name="rspace.eln")
    titles = {i["title"] for i in p["items"]}
    assert {"doc_Editable2-32", "doc_Experiment-1-25"} <= titles and not res["failed"]
    ed = next(c for c in res["created"] if c["title"] == "doc_Editable2-32")
    e = client.get(f"/api/experiments/{ed['id']}", headers=b).json()
    assert {"lemmings_1701965473304.gif", "doc_Editable2-32.xml"} <= {x["filename"] for x in e["attachments"]}
    assert {"red", "mydocument", "category1"} <= set(e["tags"])  # RSpace sends keywords as a list


def _crate(nodes: list[dict], files: dict[str, bytes], root="export", extra: dict[str, bytes] | None = None) -> bytes:
    graph = [{"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"},
              "conformsTo": {"@id": eln.CRATE}, "sdPublisher": {"@id": "#pub"}},
             {"@id": "#pub", "@type": "Organization", "name": "TestELN"}, *nodes]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"{root}/ro-crate-metadata.json", json.dumps({"@context": eln.CONTEXT, "@graph": graph}))
        for name, data in files.items():
            z.writestr(f"{root}/{name}", data)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out.getvalue()


def _simple(text="<p>hello</p>", status=None, **ds):
    data = b"time,signal\n0,1\n"
    return _crate([{"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "./exp/"}]},
                   {"@id": "./exp/", "@type": "Dataset", "name": "Grignard", "text": text, "author": {"@id": "#p1"},
                    "hasPart": [{"@id": "./exp/trace.csv"}], **({"creativeWorkStatus": status} if status else {}), **ds},
                   {"@id": "#p1", "@type": "Person", "givenName": "Ada", "familyName": "Lovelace"},
                   {"@id": "./exp/trace.csv", "@type": "File", "name": "trace.csv", "encodingFormat": "text/csv",
                    "sha256": hashlib.sha256(data).hexdigest(), "contentSize": str(len(data))}],
                  {"exp/trace.csv": data})


def test_signed_status_elsewhere_arrives_locked_and_script_never_survives(client, users, nb2):
    b = users["bob"]
    p, res = _import(client, b, _simple('<p onclick="x()">ok</p><script>alert(1)</script><img src="javascript:alert(1)">', "Signed"), nb2)
    assert p["source"] == "TestELN" and p["items"][0]["locked"] and "Signed" in p["items"][0]["lock_reason"]
    e = client.get(f"/api/experiments/{res['created'][0]['id']}", headers=b).json()
    h = e["entries"][0]["content"]["html"]
    assert e["status"] == "locked" and "<script" not in h and "onclick" not in h and "javascript:" not in h and "ok" in h
    assert e["origin"]["authors"] == ["Ada Lovelace"] and e["origin"]["files"]["matched"] == 1


@pytest.mark.parametrize("make, message", [
    (lambda: b"this is not a zip", "isn't a .eln file"),
    (lambda: _crate([], {}, extra={"elsewhere/readme.txt": b"x"})[:0] + _zip({"a/readme.txt": b"x"}), "no ro-crate-metadata.json"),
    (lambda: _zip({"export/ro-crate-metadata.json": b"{not json"}), "isn't valid JSON"),
    (lambda: _zip({"export/ro-crate-metadata.json": b"{}", "export/../../evil.sh": b"rm -rf /"}), "points outside"),
    (lambda: _zip({"/etc/passwd": b"x", "export/ro-crate-metadata.json": b"{}"}), "points outside"),
    (lambda: _encrypted(), "encrypted"),
    (lambda: _bomb(), "zip bomb"),
])
def test_unsafe_or_broken_files_are_refused_before_anything_is_stored(client, users, make, message):
    r = _preview(client, users["bob"], make())
    assert r.status_code == 422 and message in r.json()["detail"], r.text
    assert not list((eln.scratch_dir() / "imports").glob("*.eln"))  # nothing kept


def _zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(zipfile.ZipInfo(n), d)
    return out.getvalue()


def _encrypted() -> bytes:
    raw = bytearray(_zip({"export/ro-crate-metadata.json": b"{}"}))
    # set the "encrypted" flag in the local and central headers, as a password-protected zip has
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = raw.find(sig)
        raw[i + off] |= 0x1
    return bytes(raw)


def _bomb() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("export/ro-crate-metadata.json", "{}")
        with z.open("export/zeros.bin", "w") as f:
            block = bytes(1 << 20)
            for _ in range(160):  # 160 MB of zeros squeeze to ~160 kB
                f.write(block)
    return out.getvalue()


def test_files_too_big_for_one_attachment_are_left_out_with_a_warning(client, users, nb2, monkeypatch):
    monkeypatch.setattr(settings, "instrument_max_mb", 0)
    p, res = _import(client, users["bob"], _simple(), nb2)
    assert p["items"][0]["files"] == 0 and "over the" in p["items"][0]["warnings"][0]
    assert res["created"][0]["warnings"]


# ---------------------------------------------------------------------------------------------------- staging and rules


def test_duplicates_are_spotted_and_staged_files_cleaned_up(client, users, nb2):
    b = users["bob"]
    data = _simple(url="https://other-eln.example/experiments/77")
    _, res = _import(client, b, data, nb2)
    assert not list((eln.scratch_dir() / "imports").glob("*.eln"))  # imported: the upload is gone
    p = _preview(client, b, data).json()
    assert p["items"][0]["already"]["id"] == res["created"][0]["id"]
    assert client.delete(f"/api/eln/import/{p['token']}", headers=b).status_code == 204
    assert not list((eln.scratch_dir() / "imports").glob(f"{p['token']}*"))


def test_tokens_are_personal_viewers_cant_import_and_notebooks_must_be_open_to_you(client, users, nb2, notebook):
    a, b, v = users["alice"], users["bob"], users["vic"]
    p = _preview(client, b, _simple()).json()
    r = client.post(f"/api/eln/import/{p['token']}", headers=a, json={"notebook_id": notebook["id"], "items": ["0"]})
    assert r.status_code == 422 and "expired" in r.json()["detail"]
    assert _preview(client, v, _simple()).status_code == 403
    private = client.post("/api/notebooks", headers=a, json={"name": "Alice only", "visibility": "private"}).json()
    r = client.post(f"/api/eln/import/{p['token']}", headers=b, json={"notebook_id": private["id"], "items": ["0"]})
    assert r.status_code == 404
    client.delete(f"/api/eln/import/{p['token']}", headers=b)


# ---------------------------------------------------------------------------------------------------- ORCID


@pytest.mark.parametrize("given, stored", [
    ("0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("0000 0001 5109 3700", "0000-0001-5109-3700"),
    ("0000-0002-1694-233x", "0000-0002-1694-233X"),
    ("", None),
])
def test_orcid_is_checked_and_tidied(client, users, given, stored):
    r = client.patch("/api/auth/me", headers=users["bob"], json={"orcid": given})
    assert r.status_code == 200 and r.json()["orcid"] == stored


@pytest.mark.parametrize("bad", ["0000-0002-1825-0098", "1234", "0000-0002-1825-00977"])
def test_orcid_typos_are_caught(client, users, bad):
    r = client.patch("/api/auth/me", headers=users["bob"], json={"orcid": bad})
    assert r.status_code == 422


def test_export_names_people_by_their_orcid(client, users, notebook):
    a = users["alice"]
    client.patch("/api/auth/me", headers=a, json={"orcid": "0000-0002-1825-0097"})
    try:
        exp = _exp(client, a, notebook, "Credited")
        _, by = _graph(_download(client, a, f"/api/experiments/{exp['id']}/export.eln"))
        ds = next(n for n in by.values() if n.get("identifier") == exp["eid"])
        assert ds["author"] == {"@id": "https://orcid.org/0000-0002-1825-0097"}
        assert by["https://orcid.org/0000-0002-1825-0097"]["@type"] == "Person"
    finally:
        client.patch("/api/auth/me", headers=a, json={"orcid": ""})
