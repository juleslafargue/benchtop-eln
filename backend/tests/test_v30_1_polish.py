"""0.30.1: the smaller things the 0.30 stress test found.

- @mentions: a username ending in "." or "-" ("jo.") is told when mentioned (the server cut the last character off).
- Sample codes: the ⌘K box asks the server for a code it doesn't have (it loads only the latest 500 records); the
  server answers only for records the person can open, and a hidden record's code answers as a code never handed out.
  Codes written with other dashes (JL–013) or with nothing between (JL013) are found, in search too.
- A witness request to someone made read-only, or whose account is disabled, is cancelled (and audited): it no longer
  sits on their dashboard, nor keeps the record from others' "could witness". Signed records are untouched.
- Notebook names of only spaces or invisible characters are refused; spaces around a name are dropped.
- A follow-up refused because the record's notebook is archived says so.
- .eln exports: non-Latin titles stay as letters in the file's @ids, so names stay within 255 bytes (the ELN
  Consortium's checker failed on ~40 Cyrillic or ~27 Chinese characters); Benchtop reads old and new files alike.
  The .eln put on Zenodo is named after the folder inside it. Odd titles ("..", direction marks, a long name with
  brackets) give tidy file names.
- spectrum_expanded is stored as true or false, never 1."""
from __future__ import annotations

import io
import json
import zipfile
from urllib.parse import quote

import pytest
from sqlmodel import Session, select

PW = "lab-bench-pw-2026"


@pytest.fixture(autouse=True)
def _leave_no_uploads():
    from app.eln import scratch_dir
    d = scratch_dir() / "imports"
    before = set(d.iterdir()) if d.is_dir() else set()
    yield
    for f in (set(d.iterdir()) if d.is_dir() else set()) - before:
        f.unlink(missing_ok=True)


def _account(client, admin, username, role="scientist"):
    r = client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                       "full_name": username.title().rstrip(".-"), "role": role})
    assert r.status_code == 201, r.text
    tok = client.post("/api/auth/login", json={"username": username, "password": PW}).json()["token"]
    return {"Authorization": "Bearer " + tok}, r.json()["id"]


def _exp(client, h, nb_id, title="Polish"):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb_id})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ @mentions

def test_a_username_ending_in_a_dot_is_told_when_mentioned(client, users, admin, notebook):
    jo, _ = _account(client, admin, "jo.")
    dash, _ = _account(client, admin, "kim-")
    e = _exp(client, users["alice"], notebook["id"])
    r = client.post(f"/api/experiments/{e['id']}/comments", headers=users["alice"], json={"body": "@jo. and @kim- please look. Thanks @bob."})
    assert r.status_code == 201, r.text

    def told(h):
        return any(n["kind"] == "mention" and (n["experiment"] or {}).get("id") == e["id"]
                   for n in client.get("/api/notifications", headers=h).json()["items"])
    assert told(jo) and told(dash)  # 0.30: "jo." was looked up as "jo", and never told
    assert told(users["bob"])  # "@bob." at the end of a sentence is still bob


# ------------------------------------------------------------------ sample codes

def _code(client, h, exp_id):
    r = client.post(f"/api/experiments/{exp_id}/samples", headers=h, json={})
    assert r.status_code == 201, r.text
    return r.json()


def test_sample_code_lookup_finds_any_code_the_person_can_open(client, users, admin):
    owner, _ = _account(client, admin, "pcodes")
    other, _ = _account(client, admin, "qcodes")
    assert client.put("/api/auth/me/sample-prefix", headers=owner, json={"prefix": "PQ"}).status_code == 200
    shared_nb = client.post("/api/notebooks", headers=owner, json={"name": "Codes everyone sees"}).json()
    hidden_nb = client.post("/api/notebooks", headers=owner, json={"name": "Codes only I see", "visibility": "private"}).json()
    seen = _exp(client, owner, shared_nb["id"], "Seen by all")
    hidden = _exp(client, owner, hidden_nb["id"], "TOPSECRET route")
    c1 = _code(client, owner, seen["id"])["code"]          # PQ-001
    c2 = _code(client, owner, hidden["id"])["code"]        # PQ-002
    retired = _code(client, owner, seen["id"])             # PQ-003, then taken off
    assert client.delete(f"/api/samples/{retired['id']}", headers=owner).status_code == 200
    n1 = int(c1.split("-")[1])

    def look(h, typed):
        return client.get("/api/samples/lookup", headers=h, params={"code": typed})

    for typed in (c1, c1.lower(), f"PQ-{n1}", f"pq_{n1:03d}", f"PQ {n1}", f"PQ{n1:03d}", f"pq{n1}",
                  f"PQ‐{n1:03d}", f"PQ–{n1:03d}", f"PQ−{n1:03d}", f"PQ—{n1:03d}", f" {c1} "):
        r = look(other, typed)
        assert r.status_code == 200, (typed, r.text)
        assert r.json()["experiment"]["id"] == seen["id"] and r.json()["code"] == c1
    assert look(owner, c2).json()["experiment"]["id"] == hidden["id"]  # the owner finds their own
    # someone who can't open the record: answered exactly as a code never handed out, or a retired one
    unknown, gone, hid = look(other, "PQ-777"), look(other, retired["code"]), look(other, c2)
    assert unknown.status_code == gone.status_code == hid.status_code == 404
    assert unknown.json() == gone.json() == hid.json()
    assert "TOPSECRET" not in hid.text and hidden["eid"] not in hid.text
    for nonsense in ("", "suzuki", "%", "PQ-", "EXP-00001", "x" * 500):
        assert look(other, nonsense).status_code == 404


def test_search_reads_codes_with_other_dashes_or_none(client, users, admin):
    owner, _ = _account(client, admin, "rcodes")
    other, _ = _account(client, admin, "scodes")
    assert client.put("/api/auth/me/sample-prefix", headers=owner, json={"prefix": "RQ"}).status_code == 200
    nb = client.post("/api/notebooks", headers=owner, json={"name": "Search codes"}).json()
    priv = client.post("/api/notebooks", headers=owner, json={"name": "Search codes hidden", "visibility": "private"}).json()
    e = _exp(client, owner, nb["id"], "Found by its code")
    h = _exp(client, owner, priv["id"], "Hidden by its notebook")
    code = _code(client, owner, e["id"])["code"]
    hidden_code = _code(client, owner, h["id"])["code"]
    n = int(code.split("-")[1])
    for typed in (f"RQ–{n:03d}", f"RQ−{n}", f"RQ{n:03d}", f"rq{n}"):
        hits = [x["id"] for x in client.get("/api/search", headers=other, params={"q": typed}).json()["experiments"]]
        assert hits and hits[0] == e["id"], typed
    # what search makes of a hidden code is what it makes of one never handed out
    hn = int(hidden_code.split("-")[1])
    a = client.get("/api/search", headers=other, params={"q": f"RQ{hn:03d}"}).json()
    b = client.get("/api/search", headers=other, params={"q": "RQ990"}).json()
    assert not a["experiments"] and not b["experiments"]
    assert a["terms"] == [f"rq-{hn:03d}"] and b["terms"] == ["rq-990"]


def test_instrument_files_with_an_en_dash_find_their_experiment(client, users, admin):
    from app import samples
    from app.db import engine
    owner, uid = _account(client, admin, "tcodes")
    assert client.put("/api/auth/me/sample-prefix", headers=owner, json={"prefix": "TQ"}).status_code == 200
    nb = client.post("/api/notebooks", headers=owner, json={"name": "Instrument codes"}).json()
    e = _exp(client, owner, nb["id"])
    code = _code(client, owner, e["id"])["code"]
    with Session(engine) as s:
        found = samples.find(s, f"2026-11-03 {code.replace('-', chr(0x2013))} crude.jdx")
        assert [(f.text, f.code.experiment_id) for f in found] == [(code, e["id"])]
        # a file name with a date straight after the letters isn't taken for a code: no separator means no code there
        assert samples.find(s, "TQ2026-11-03.jdx") == []


# ------------------------------------------------------------------ witness requests

def test_a_witness_made_read_only_or_disabled_is_no_longer_waited_on(client, users, admin):
    from app.db import engine
    from app.models import AuditEvent, Signature, WitnessRequest
    author, _ = _account(client, admin, "wauthor")
    w1, w1_id = _account(client, admin, "wviewer")
    w2, w2_id = _account(client, admin, "wdisabled")
    third, _ = _account(client, admin, "wthird")
    nb = client.post("/api/notebooks", headers=author, json={"name": "Witnessing"}).json()
    e1, e2 = _exp(client, author, nb["id"], "Waits on w1"), _exp(client, author, nb["id"], "Waits on w2")
    for e, wid in ((e1, w1_id), (e2, w2_id)):
        r = client.post(f"/api/experiments/{e['id']}/sign", headers=author, json={"password": PW, "witness_id": wid})
        assert r.status_code == 200, r.text
    with Session(engine) as s:
        sigs = {e["id"]: [(g.id, g.content_hash, g.signed_at) for g in s.exec(select(Signature).where(Signature.experiment_id == e["id"]))]
                for e in (e1, e2)}
    before = {e["id"]: client.get(f"/api/experiments/{e['id']}/verify", headers=author).json() for e in (e1, e2)}
    assert client.get("/api/dashboard", headers=w1).json()["totals"]["awaiting"] == 1
    could = lambda: {x["id"] for x in client.get("/api/dashboard", headers=third).json()["lists"]["could_witness"]}  # noqa: E731
    assert e1["id"] not in could() and e2["id"] not in could()  # someone was asked

    assert client.patch(f"/api/users/{w1_id}", headers=admin, json={"role": "viewer"}).status_code == 200
    assert client.patch(f"/api/users/{w2_id}", headers=admin, json={"active": False}).status_code == 200
    with Session(engine) as s:
        for e in (e1, e2):
            r = s.exec(select(WitnessRequest).where(WitnessRequest.experiment_id == e["id"])).one()
            assert (r.status, r.outcome) == ("cancelled", "witness_unavailable")
        ev = {x.object_id: x.details for x in s.exec(select(AuditEvent).where(AuditEvent.action == "witness_requests_cancelled"))}
        assert ev[w1_id]["eids"] == [e1["eid"]] and "read-only" in ev[w1_id]["reason"]
        assert ev[w2_id]["eids"] == [e2["eid"]] and "disabled" in ev[w2_id]["reason"]
        # the signed records and their signatures are exactly as they were
        assert {e["id"]: [(g.id, g.content_hash, g.signed_at) for g in s.exec(select(Signature).where(Signature.experiment_id == e["id"]))]
                for e in (e1, e2)} == sigs
    for e in (e1, e2):
        after = client.get(f"/api/experiments/{e['id']}", headers=author).json()
        assert after["status"] == "signed" and after["witness_request"] is None
        assert client.get(f"/api/experiments/{e['id']}/verify", headers=author).json() == before[e["id"]]
    assert client.get("/api/dashboard", headers=w1).json()["totals"]["awaiting"] == 0
    assert {e1["id"], e2["id"]} <= could()  # nobody is asked now: others can see they could witness them
    told = [n["text"] for n in client.get("/api/notifications", headers=author).json()["items"] if n["kind"] == "witness_cancelled"]
    assert any(e1["eid"] in t and "read-only" in t for t in told) and any(e2["eid"] in t and "disabled" in t for t in told)
    assert client.get("/api/audit/verify", headers=admin).json()["ok"]


def test_requests_left_waiting_on_someone_who_cant_witness_are_cancelled_by_housekeeping(client, users, admin):
    """Made read-only before 0.30.1 (or by hand in the database): the hourly housekeeping clears it."""
    from app import witness
    from app.db import engine
    from app.models import Role, User, WitnessRequest
    author, _ = _account(client, admin, "hauthor")
    w, wid = _account(client, admin, "hwitness")
    nb = client.post("/api/notebooks", headers=author, json={"name": "Housekeeping"}).json()
    e = _exp(client, author, nb["id"])
    assert client.post(f"/api/experiments/{e['id']}/sign", headers=author, json={"password": PW, "witness_id": wid}).status_code == 200
    with Session(engine) as s:
        u = s.get(User, wid)
        u.role = Role.viewer
        s.add(u)
        s.commit()
    witness.send_due_reminders()
    with Session(engine) as s:
        r = s.exec(select(WitnessRequest).where(WitnessRequest.experiment_id == e["id"])).one()
        assert (r.status, r.outcome) == ("cancelled", "witness_unavailable")


# ------------------------------------------------------------------ notebook names, follow-ups

@pytest.mark.parametrize("name", ["   ", "​", "​ ‎⁠", "\t\n", "﻿"])
def test_a_notebook_name_of_nothing_visible_is_refused(client, users, name):
    r = client.post("/api/notebooks", headers=users["alice"], json={"name": name})
    assert r.status_code == 422 and "name" in r.text.lower(), r.text
    nb = client.post("/api/notebooks", headers=users["alice"], json={"name": "Has a name"}).json()
    r = client.patch(f"/api/notebooks/{nb['id']}", headers=users["alice"], json={"name": name})
    assert r.status_code == 422, r.text
    names = {n["id"]: n["name"] for n in client.get("/api/notebooks", headers=users["alice"]).json()}
    assert names[nb["id"]] == "Has a name"


def test_spaces_and_invisible_characters_around_a_notebook_name_go(client, users):
    nb = client.post("/api/notebooks", headers=users["alice"], json={"name": "  ​Suzuki couplings ‎ "}).json()
    assert nb["name"] == "Suzuki couplings"
    r = client.patch(f"/api/notebooks/{nb['id']}", headers=users["alice"], json={"name": " Suzuki couplings 2 "})
    assert r.json()["name"] == "Suzuki couplings 2"


def test_patching_a_notebook_you_cant_open_gives_404(client, users):
    """M7 (A3): already done in 6bfd827's batch; checked here once more."""
    priv = client.post("/api/notebooks", headers=users["alice"], json={"name": "Alice only", "visibility": "private"}).json()
    r = client.patch(f"/api/notebooks/{priv['id']}", headers=users["bob"], json={"name": "Mine now"})
    missing = client.patch("/api/notebooks/987654", headers=users["bob"], json={"name": "Mine now"})
    assert r.status_code == missing.status_code == 404


def test_follow_up_refused_because_the_notebook_is_archived_says_so(client, users):
    nb = client.post("/api/notebooks", headers=users["alice"], json={"name": "Archived for follow-ups"}).json()
    e = _exp(client, users["alice"], nb["id"])
    assert client.post(f"/api/experiments/{e['id']}/sign", headers=users["alice"], json={"password": PW}).status_code == 200
    assert client.patch(f"/api/notebooks/{nb['id']}", headers=users["alice"], json={"archived": True}).status_code == 200
    r = client.post(f"/api/experiments/{e['id']}/follow-up", headers=users["alice"], json={})
    assert r.status_code == 409 and "archived" in r.json()["detail"], r.text
    assert "Archived for follow-ups" in r.json()["detail"]


# ------------------------------------------------------------------ .eln exports

TITLES = {
    "ru": "Сочетание Сузуки 4-броманизола с фенилбороновой кислотой, партия 2",
    "zh": "4-溴苯甲醚与苯硼酸的铃木偶联反应：碳酸钾与磷酸钾的比较",
}


def _members(data: bytes):
    z = zipfile.ZipFile(io.BytesIO(data))
    root = z.namelist()[0].split("/")[0]
    return z, root, json.loads(z.read(f"{root}/ro-crate-metadata.json"))["@graph"]


@pytest.mark.parametrize("lang", list(TITLES))
def test_non_latin_titles_stay_letters_in_the_eln(client, users, notebook, lang):
    e = _exp(client, users["alice"], notebook["id"], TITLES[lang])
    client.post(f"/api/experiments/{e['id']}/attachments", headers=users["alice"],
                files={"file": ("核磁共振氢谱数据第二批次粗产物重结晶后的样品谱图.jdx", b"##TITLE=x\n##END=\n", "chemical/x-jcamp-dx")})
    data = client.get(f"/api/experiments/{e['id']}/export.eln", headers=users["alice"]).content
    z, root, graph = _members(data)
    folder = next(n["@id"] for n in graph if n.get("@type") == "Dataset" and n["@id"] != "./")
    assert TITLES[lang][:10].replace(" ", "%20") in folder and "%D0" not in folder and "%E" not in folder, folder
    for n in graph:
        i = n["@id"]
        if i.startswith("./") and i != "./":
            assert all(len(p.encode()) <= 255 for p in i.split("/")), i  # the checker uses the @id as a file name
            # every file is where its @id says, once the spaces' %20 are read back
            if n.get("@type") == "File":
                from urllib.parse import unquote
                assert f"{root}/{unquote(i[2:])}" in z.namelist(), i
    assert all(len(p.encode()) <= 255 for m in z.namelist() for p in m.split("/"))


def _import(client, h, data: bytes, nb_id: int):
    pv = client.post("/api/eln/preview", headers=h, files={"file": ("x.eln", data, "application/zip")})
    assert pv.status_code == 200, pv.text
    pv = pv.json()
    r = client.post(f"/api/eln/import/{pv['token']}", headers=h, json={"notebook_id": nb_id, "items": [it["key"] for it in pv["items"]]})
    assert r.status_code == 200, r.text
    return r.json()


def test_benchtop_reads_its_old_and_new_non_latin_exports(client, users, notebook, monkeypatch):
    from app import eln
    e = _exp(client, users["alice"], notebook["id"], TITLES["ru"])
    client.post(f"/api/experiments/{e['id']}/attachments", headers=users["alice"],
                files={"file": ("спектр ЯМР.jdx", b"##TITLE=x\n##END=\n", "chemical/x-jcamp-dx")})
    new = client.get(f"/api/experiments/{e['id']}/export.eln", headers=users["alice"]).content
    monkeypatch.setattr(eln, "_id", lambda path: "./" + quote(path, safe="/-_.~()'!*,;=+@"))  # as 0.30 and before wrote them
    old = client.get(f"/api/experiments/{e['id']}/export.eln", headers=users["alice"]).content
    monkeypatch.undo()
    assert "%D0" in json.dumps(_members(old)[2]) and "%D0" not in json.dumps(_members(new)[2])
    for data in (old, new):
        dest = client.post("/api/notebooks", headers=users["alice"], json={"name": "Imported back"}).json()
        _import(client, users["alice"], data, dest["id"])
        got = client.get(f"/api/experiments?notebook_id={dest['id']}", headers=users["alice"]).json()
        assert [x["title"] for x in got] == [TITLES["ru"]]
        full = client.get(f"/api/experiments/{got[0]['id']}", headers=users["alice"]).json()
        assert any(a["filename"] == "спектр ЯМР.jdx" for a in full["attachments"]), full["attachments"]


def test_zenodo_eln_is_named_after_its_folder(client, users, notebook, tmp_path):
    from app import eln, publishing
    from app.db import engine
    from app.models import Experiment, User
    e = _exp(client, users["alice"], notebook["id"], "Suzuki coupling: K2CO3 vs K3PO4")
    with Session(engine) as s:
        exp, u = s.get(Experiment, e["id"]), s.exec(select(User).where(User.username == "alice")).one()
        pdf, eln_name = publishing._names(exp)
        out = tmp_path / "z.eln"
        res = eln.export(s, u, [exp], out, f"{exp.eid} - {exp.title}", licence="cc-by-4.0")  # as publishing does
    root = zipfile.ZipFile(out).namelist()[0].split("/")[0]
    assert eln_name == res["name"] == f"{root}.eln" and pdf == f"{root}.pdf"


@pytest.mark.parametrize("title, expected", [
    ("..", "EXP-#####.eln"),
    ("‮exe.txt‬", "EXP-##### - exe.txt.eln"),
    ("zero​width‍name", "EXP-##### - zerowidthname.eln"),
    ("Synthesis of the ligand and its complexes with ruthenium, osmium and iron from (S)-2-(4-bromophenyl)propanoic "
     "acid in dry THF", "EXP-##### - Synthesis of the ligand and its complexes with ruthenium, osmium and iron from.eln"),
])
def test_odd_titles_give_tidy_export_names(client, users, notebook, title, expected):
    e = _exp(client, users["alice"], notebook["id"], title)
    r = client.get(f"/api/experiments/{e['id']}/export.eln", headers=users["alice"])
    from app.eln import file_stem
    name = file_stem(f"{e['eid']} - {title}") + ".eln"
    assert name == expected.replace("EXP-#####", e["eid"]), name
    assert quote(name) in r.headers["content-disposition"] or name in r.headers["content-disposition"]
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist()[0].split("/")[0] + ".eln" == name


# ------------------------------------------------------------------ preferences

def test_spectrum_expanded_is_stored_as_true_or_false(client, users):
    h = users["bob"]
    for sent in (True, 1, False, 0, "yes", True, 0):
        prefs = client.patch("/api/auth/me", headers=h, json={"preferences": {"spectrum_expanded": sent}}).json()["preferences"]
        kept = prefs.get("spectrum_expanded")
        assert type(kept) is bool, (sent, kept)  # 0.30: 1 was kept as 1
        if type(sent) is bool:
            assert kept is sent
