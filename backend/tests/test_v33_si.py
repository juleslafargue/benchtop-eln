"""0.33: Supporting Information documents, one per paper, kept by the person writing it.

A document lists experiments, each with the compound number and name used in the paper; reading it back gives each
compound's write-up, product and spectra, and what a journal will ask for that the record doesn't have. Only its owner
sees it, only experiments the owner may open are ever read into it, and it never changes an experiment."""
from __future__ import annotations

from sqlmodel import Session, select

from app import si
from app.db import engine
from app.models import Entry, EntryKind, ExperimentStatus

PW = "lab-bench-pw-2026"
JDX = b"##TITLE=t\n##JCAMP-DX=4.24\n##DATA TYPE=NMR SPECTRUM\n##.OBSERVE NUCLEUS=^1H\n##XYDATA=(XY..XY)\n1.0, 2.0\n##END=\n"


def make_compound(client, h, nb, title="Suzuki 3a", *, c13=True, hrms=True, appearance="White solid", mp=None, file=True):
    exp = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]}).json()
    exp = client.get(f"/api/experiments/{exp['id']}", headers=h).json()
    ents = {e["kind"]: e for e in exp["entries"]}
    client.put(f"/api/entries/{ents['reaction']['id']}", headers=h, json={"content": {
        "conditions": {"temperature_c": 80, "time_h": 12, "atmosphere": "N2"},
        "rows": [{"role": "reactant", "name": "4-Bromoanisole", "smiles": "COc1ccc(Br)cc1", "mw": 187.04, "basis": "mmol", "mmol": 1, "limiting": True},
                 {"role": "solvent", "name": "Toluene", "basis": "volume", "volume_ml": 5},
                 {"role": "product", "name": "4-Methoxybiphenyl", "smiles": "COc1ccc(-c2ccccc2)cc1", "mw": 184.24, "formula": "C13H12O"}]}})
    client.put(f"/api/entries/{ents['purification']['id']}", headers=h, json={"content": {
        "method": "column", "eluent": "5% EtOAc/hexanes", "isolated_mg": 160, "appearance": appearance, "rf": "0.4"}})
    items = []
    a1 = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("3a_1H.jdx", JDX)}).json() if file else None
    items.append({"technique": "1H NMR", "conditions": "400 MHz, CDCl3", "data": "δ 7.54 (d, J = 8.8 Hz, 2H), 3.86 (s, 3H)",
                  "attachment_id": a1["id"] if a1 else None})
    if c13:
        a2 = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("3a_13C.jdx", JDX)}).json()
        items.append({"technique": "13C NMR", "conditions": "101 MHz, CDCl3", "data": "δ 159.2, 140.9", "attachment_id": a2["id"]})
    if hrms:
        items.append({"technique": "HRMS", "conditions": "ESI", "data": "m/z [M+H]+ calcd for C13H13O 185.0961; found 185.0959"})
    if mp:
        items.append({"technique": "mp", "conditions": "", "data": mp})
    client.put(f"/api/entries/{ents['characterization']['id']}", headers=h, json={"content": {"items": items}})
    return client.get(f"/api/experiments/{exp['id']}", headers=h).json()


def new_doc(client, h, title="Suzuki couplings"):
    r = client.post("/api/si-documents", headers=h, json={"title": title})
    assert r.status_code == 201, r.text
    return r.json()


def put(client, h, doc, **kw):
    return client.put(f"/api/si-documents/{doc['id']}", headers=h, json={"expected_version": doc["version"], **kw})


def test_create_list_update_and_delete(client, users, notebook):
    a = users["alice"]
    exp = make_compound(client, a, notebook)
    doc = new_doc(client, a)
    assert doc["title"] == "Suzuki couplings" and doc["items"] == [] and doc["compounds"] == [] and doc["page_size"] == "letter"
    r = put(client, a, doc, general="All reactions under N2.\n\nNMR spectra at 298 K.", page_size="a4",
            items=[{"experiment_id": exp["id"], "number": " 3a ", "name": ""}])
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["version"] == 2 and doc["page_size"] == "a4" and doc["general"].startswith("All reactions")
    assert doc["items"] == [{"experiment_id": exp["id"], "number": "3a", "name": "", "spectra": None}]
    c = doc["compounds"][0]
    assert c["visible"] and c["eid"] == exp["eid"] and c["number"] == "3a"
    assert c["name"] == "4-Methoxybiphenyl" == c["name_default"]  # the product row's name unless one is typed
    assert c["product"]["smiles"] == "COc1ccc(-c2ccccc2)cc1"
    assert "4-bromoanisole" in c["experimental_html"].lower() and "R<sub>f</sub>" in c["characterisation_html"]
    assert "<i>J</i> = 8.8 Hz" in c["characterisation_html"]
    # every NMR file is a figure by default
    assert [(s["filename"], s["included"]) for s in c["spectra"]] == [("3a_1H.jdx", True), ("3a_13C.jdx", True)]

    # a name typed for the paper, and only the 13C spectrum
    c13 = c["spectra"][1]["attachment_id"]
    doc = put(client, a, doc, items=[{"experiment_id": exp["id"], "number": "3a", "name": "Anisyl benzene", "spectra": [c13]}]).json()
    c = doc["compounds"][0]
    assert c["name"] == "Anisyl benzene" and [s["included"] for s in c["spectra"]] == [False, True]

    listed = client.get("/api/si-documents", headers=a).json()
    assert {"id": doc["id"], "title": "Suzuki couplings", "compounds": 1} .items() <= next(d for d in listed if d["id"] == doc["id"]).items()
    assert client.delete(f"/api/si-documents/{doc['id']}", headers=a).json()["ok"]
    assert client.get(f"/api/si-documents/{doc['id']}", headers=a).status_code == 404
    assert all(d["id"] != doc["id"] for d in client.get("/api/si-documents", headers=a).json())


def test_only_the_owner_can_list_open_change_or_delete_it(client, users, admin, notebook):
    a, b = users["alice"], users["bob"]
    doc = new_doc(client, a, "Alice's paper")
    for h in (b, admin, users["vic"]):
        assert all(d["id"] != doc["id"] for d in client.get("/api/si-documents", headers=h).json())
        assert client.get(f"/api/si-documents/{doc['id']}", headers=h).status_code == 404
        assert put(client, h, doc, title="Mine now").status_code in (403, 404)
        assert client.delete(f"/api/si-documents/{doc['id']}", headers=h).status_code in (403, 404)
    assert client.get(f"/api/si-documents/{doc['id']}", headers=a).json()["title"] == "Alice's paper"
    # a read-only account can't make one
    assert client.post("/api/si-documents", headers=users["vic"], json={"title": "x"}).status_code == 403


def test_a_save_from_an_older_version_is_refused(client, users):
    a = users["alice"]
    doc = new_doc(client, a)
    first = put(client, a, doc, title="From tab one")
    assert first.status_code == 200
    stale = put(client, a, doc, title="From tab two")  # still at version 1
    assert stale.status_code == 409 and "changed elsewhere" in stale.json()["detail"]
    assert client.get(f"/api/si-documents/{doc['id']}", headers=a).json()["title"] == "From tab one"
    assert put(client, a, first.json(), title="Tab two, reloaded").status_code == 200


def test_an_experiment_the_person_cant_open_is_never_read_into_their_si(client, users, admin):
    a, b = users["alice"], users["bob"]
    shared = client.post("/api/notebooks", headers=b, json={"name": "Bob's open notebook"}).json()
    private = client.post("/api/notebooks", headers=b, json={"name": "Bob's private notebook", "visibility": "private"}).json()
    secret = make_compound(client, b, private, "Bob's secret compound")
    later = make_compound(client, b, shared, "Bob's compound, hidden later")
    doc = new_doc(client, a)

    # adding one she can't open is refused, and says nothing about it
    r = put(client, a, doc, items=[{"experiment_id": secret["id"], "number": "1"}])
    assert r.status_code == 404 and "secret" not in r.text.lower()

    doc = put(client, a, doc, items=[{"experiment_id": later["id"], "number": "2"}]).json()
    assert doc["compounds"][0]["visible"] and doc["compounds"][0]["title"] == "Bob's compound, hidden later"

    # Bob makes the notebook private: the id stays in Alice's list, but nothing of the record is read
    assert client.patch(f"/api/notebooks/{shared['id']}", headers=b, json={"visibility": "private"}).status_code == 200
    calls = []
    real = si.live_entries
    si.live_entries = lambda s, i: calls.append(i) or real(s, i)
    try:
        doc = client.get(f"/api/si-documents/{doc['id']}", headers=a).json()
    finally:
        si.live_entries = real
    c = doc["compounds"][0]
    assert later["id"] not in calls
    assert c["visible"] is False and c["problems"] == [f"{later['eid']} is no longer one you can open, so it's left out"]
    for k in ("title", "experimental_html", "characterisation_html", "spectra", "product", "name_default"):
        assert k not in c
    assert "Bob's" not in str(c) and "Methoxybiphenyl" not in str(c)
    # and saving the document again (a new title) keeps it in the list, still unread
    r = put(client, a, doc, title="Renamed", items=doc["items"])
    assert r.status_code == 200 and r.json()["compounds"][0]["visible"] is False
    # an id that never existed reads the same way
    with Session(engine) as s:
        from app.models import SiDocument
        d = s.get(SiDocument, doc["id"])
        d.items = [*d.items, {"experiment_id": 999_999, "number": "9", "name": "", "spectra": None}]
        s.add(d)
        s.commit()
    gone = client.get(f"/api/si-documents/{doc['id']}", headers=a).json()["compounds"][1]
    assert gone["visible"] is False and gone["eid"] == "EXP-999999"
    client.patch(f"/api/notebooks/{shared['id']}", headers=b, json={"visibility": "everyone"})


def test_what_a_journal_will_ask_for_is_listed(client, users, notebook):
    a = users["alice"]
    full = make_compound(client, a, notebook, "Complete", mp="86–88 °C")
    bare = make_compound(client, a, notebook, "Bare", c13=False, hrms=False, file=False)
    oil = make_compound(client, a, notebook, "Oil", appearance="Colourless oil")
    doc = new_doc(client, a)
    doc = put(client, a, doc, items=[{"experiment_id": e["id"], "number": n} for e, n in ((full, "1"), (bare, "2a"), (oil, "3"))]).json()
    p = {c["number"]: c["problems"] for c in doc["compounds"]}
    no_integrals = "the ¹H spectrum has no saved integrals (the figure uses automatic ones; check them in the spectrum viewer)"
    assert p["1"] == ["not signed yet", no_integrals]
    assert p["2a"] == ["not signed yet", "no ¹³C NMR data", "no HRMS (or elemental analysis)",
                       "no melting point (it's a white solid)", "no ¹H NMR spectrum file", "no ¹³C NMR spectrum file"]
    assert p["3"] == ["not signed yet", no_integrals]  # an oil needs no melting point

    # an empty record: what the write-up couldn't find is listed too
    empty = client.post("/api/experiments", headers=a, json={"title": "Empty", "notebook_id": notebook["id"]}).json()
    doc = put(client, a, doc, items=[{"experiment_id": empty["id"], "number": "4"}]).json()
    p = doc["compounds"][0]["problems"]
    assert "no reaction table with amounts" in p and "no isolated mass" in p and "no ¹H NMR data" in p
    assert "no characterisation data" not in p  # said more precisely by the lines above


def test_saved_integrals_and_signing_clear_their_lines(client, users, notebook):
    a = users["alice"]
    exp = make_compound(client, a, notebook, "Signed and analysed", mp="86–88 °C")
    ch = next(e for e in exp["entries"] if e["kind"] == "characterization")
    items = ch["content"]["items"]
    items[0]["analysis"] = {"integrals": [[7.6, 7.5], [3.9, 3.8]], "ref_idx": 1, "ref_value": 3, "threshold": 0.05}
    assert client.put(f"/api/entries/{ch['id']}", headers=a, json={"content": {"items": items}}).status_code == 200
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW}).status_code == 200
    before = client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()
    with Session(engine) as s:
        versions = {e.id: (e.version, e.updated_at) for e in s.exec(select(Entry).where(Entry.experiment_id == exp["id"]))}
    doc = new_doc(client, a)
    doc = put(client, a, doc, items=[{"experiment_id": exp["id"], "number": "5"}]).json()
    assert doc["compounds"][0]["status"] == "signed" and doc["compounds"][0]["problems"] == []
    # reading a signed record into an SI changes nothing a signature covers
    after = client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()
    assert after["content_hash"] == before["content_hash"] and after["intact"]
    with Session(engine) as s:
        assert versions == {e.id: (e.version, e.updated_at) for e in s.exec(select(Entry).where(Entry.experiment_id == exp["id"]))}


def test_checks_on_their_own():
    def entry(kind, content):
        return Entry(experiment_id=1, kind=kind, content=content)
    char = entry(EntryKind.characterization, {"items": [
        {"technique": "¹H NMR", "data": "δ 7.2", "attachment_id": 4, "analysis": {"integrals": [[1, 2]]}},
        {"technique": "13C{1H} NMR", "data": "δ 128.1", "attachment_id": 5},
        {"technique": "Elemental analysis", "data": "Anal. calcd for C7H8O: C, 77.75; found C, 77.60"},
        {"technique": "m.p.", "data": "101 °C"}]})
    pur = entry(EntryKind.purification, {"appearance": "Pale yellow crystals"})
    assert si.checks([char, pur], {"missing": []}, ExperimentStatus.witnessed) == []
    assert si.checks([char, pur], {"missing": []}, ExperimentStatus.voided) == ["the experiment is voided"]
    no_mp = entry(EntryKind.characterization, {"items": [it for it in char.content["items"] if it["technique"] != "m.p."]})
    assert si.checks([no_mp, pur], {"missing": []}, ExperimentStatus.signed) == ["no melting point (it's pale yellow crystals)"]


def test_odd_requests_get_plain_refusals(client, users):
    a = users["alice"]
    doc = new_doc(client, a)
    for body in ({"items": [{"experiment_id": 2**63}]}, {"items": [{"experiment_id": "x"}]}, {"page_size": "legal"},
                 {"items": [{"experiment_id": 1, "number": "x" * 50}]}, {"title": "t" * 400},
                 {"items": [{"experiment_id": 1, "spectra": list(range(1, 100))}]}):
        assert client.put(f"/api/si-documents/{doc['id']}", headers=a, json=body).status_code == 422, body
    for bad in (0, -1, 2**40):
        assert client.get(f"/api/si-documents/{bad}", headers=a).status_code in (404, 422)
