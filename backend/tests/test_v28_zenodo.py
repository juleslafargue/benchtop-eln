"""0.28: publishing a signed record on Zenodo, as an open dataset with a DOI.

Everything here runs against tests/fake_zenodo.py (the parts of Zenodo's deposit API Benchtop uses, with its checks);
nothing is sent to the real zenodo.org. The two steps: prepare (a draft with a reserved DOI, the PDF and .eln uploaded;
it can be discarded), then publish (permanent). Publishing never changes what a signature covers."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from app import publishing, sitesettings, zenodo
from app.config import settings
from app.db import engine
from app.models import AuditEvent, Experiment, Publication, Signature, ZenodoAccount
from tests.fake_zenodo import GOOD, WRITE_ONLY, FakeZenodo

PW = "lab-bench-pw-2026"
SANDBOX = "https://sandbox.zenodo.org"


@pytest.fixture()
def fz(client, admin):
    """A fake sandbox.zenodo.org, chosen in Admin → Integrations; the real one is never reached."""
    fake = FakeZenodo(SANDBOX)
    zenodo.set_transport(fake.transport())
    r = client.put("/api/admin/zenodo", headers=admin, json={"site": "sandbox", "enabled": True, "community": ""})
    assert r.status_code == 200 and r.json()["sandbox"], r.text
    yield fake
    zenodo.set_transport(None)
    sitesettings.update("zenodo", {"url": zenodo.ZENODO, "enabled": True, "community": ""})
    with Session(engine) as s:
        for acc in s.exec(select(ZenodoAccount)):
            s.delete(acc)
        s.commit()


def connect(client, h, token=GOOD):
    return client.put("/api/auth/zenodo", headers=h, json={"token": token})


def signed_record(client, h, notebook, title="Suzuki coupling of 4-bromoanisole", witness=None, comment=True):
    exp = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": notebook["id"]}).json()
    eid = exp["id"]
    client.patch(f"/api/experiments/{eid}", headers=h, json={"tags": ["suzuki", "cross-coupling"]})
    client.post(f"/api/experiments/{eid}/entries", headers=h, json={"kind": "text", "title": "Aim", "content": {
        "html": "<p>To make 4-methoxybiphenyl by <b>Suzuki</b> coupling &amp; compare bases.</p>"}})
    client.post(f"/api/experiments/{eid}/entries", headers=h, json={"kind": "references", "title": "References", "content": {
        "items": [{"doi": "10.1021/om100106e", "title": "NMR chemical shifts of trace impurities", "authors": ["Fulmer, Gregory R."]},
                  {"doi": "", "title": "Typed in, no DOI"}]}})
    att = client.post(f"/api/experiments/{eid}/attachments", headers=h,
                      files={"file": ("crude-1H.jdx", b"##TITLE=crude\n##END=\n", "chemical/x-jcamp-dx")}).json()
    if comment:
        client.post(f"/api/experiments/{eid}/comments", headers=h, json={"body": "Is the peak at 7.26 just CHCl3?"})
    r = client.post(f"/api/experiments/{eid}/sign", headers=h, json={"password": PW})
    assert r.status_code == 200, r.text
    if witness:
        assert client.post(f"/api/experiments/{eid}/witness", headers=witness, json={"password": PW}).status_code == 200
    return client.get(f"/api/experiments/{eid}", headers=h).json() | {"att": att}


def details(client, h, exp, **changes):
    d = client.get(f"/api/experiments/{exp['id']}/zenodo/preview", headers=h).json()["details"]
    return {**d, **changes}


def prepare(client, h, exp, **changes):
    return client.post(f"/api/experiments/{exp['id']}/zenodo/prepare", headers=h, json=details(client, h, exp, **changes))


def publish(client, h, exp, confirm=True):
    return client.post(f"/api/experiments/{exp['id']}/zenodo/publish", headers=h, json={"confirm": confirm})


def eln_in(fake: FakeZenodo, dep_id: int) -> zipfile.ZipFile:
    name = next(n for n in fake.content[dep_id] if n.endswith(".eln"))
    return zipfile.ZipFile(io.BytesIO(fake.content[dep_id][name]))


# ====================================================================== the two steps, end to end

def test_prepare_then_publish_gives_a_doi_on_the_record(client, users, notebook, fz):
    a, b = users["alice"], users["bob"]
    client.patch("/api/auth/me", headers=a, json={"orcid": "0000-0002-1825-0097"})
    exp = signed_record(client, a, notebook, witness=b)
    assert connect(client, a).status_code == 200

    r = prepare(client, a, exp, creators=[{"name": "Alice, A.", "affiliation": "University of Toronto", "orcid": "0000-0002-1825-0097"},
                                          {"name": "Bob, B.", "affiliation": "", "orcid": ""}])
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["status"] == "draft" and pub["doi"].startswith("10.5072/zenodo.") and pub["sandbox"]
    assert pub["draft_url"] == f"{SANDBOX}/deposit/{pub['deposition_id']}"
    assert not fz.published()  # step 1 publishes nothing

    md = fz.deps[pub["deposition_id"]]["metadata"]
    assert md["upload_type"] == "dataset" and md["title"] == exp["title"] and md["license"] == "cc-by-4.0"
    assert md["access_right"] == "open" and md["prereserve_doi"]["doi"] == pub["doi"]
    assert md["creators"] == [{"name": "Alice, A.", "affiliation": "University of Toronto", "orcid": "0000-0002-1825-0097"},
                              {"name": "Bob, B."}]
    assert md["keywords"] == ["suzuki", "cross-coupling"]
    assert md["related_identifiers"] == [{"identifier": "10.1021/om100106e", "relation": "references", "scheme": "doi"}]
    assert "4-methoxybiphenyl" in md["description"] and "witnessed by Bob" in md["description"]
    assert exp_hash(client, a, exp) in md["notes"]

    # the PDF and the .eln, every attached file inside with its SHA-256, the discussion left out
    names = sorted(fz.content[pub["deposition_id"]])
    assert [n.rsplit(".", 1)[1] for n in names] == ["eln", "pdf"] and fz.content[pub["deposition_id"]][names[1]][:5] == b"%PDF-"
    z = eln_in(fz, pub["deposition_id"])
    graph = json.loads(z.read(next(n for n in z.namelist() if n.endswith("ro-crate-metadata.json"))))["@graph"]
    crude = next(n for n in graph if n.get("name") == "crude-1H.jdx")
    assert crude["sha256"] == exp["att"]["sha256"]
    record = json.loads(z.read(next(n for n in z.namelist() if n.endswith("record.benchtop.json"))))
    assert record["comments"] == [] and not any(n.get("@type") == "Comment" for n in graph)
    assert [s["meaning"] for s in record["signatures"]] == ["author", "witness"]

    assert publish(client, a, exp, confirm=False).status_code == 422  # it can't be undone: it has to be confirmed
    r = publish(client, a, exp)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "published" and done["doi"] == pub["doi"] and done["url"] == f"{SANDBOX}/records/{pub['deposition_id']}"
    assert done["doi_url"] == ""  # sandbox DOIs don't resolve at doi.org
    assert [d["doi"] for d in fz.published()] == [pub["doi"]]

    page = client.get(f"/api/experiments/{exp['id']}", headers=b).json()  # colleagues see the DOI too
    assert page["publication"]["status"] == "published" and page["publication"]["doi"] == pub["doi"]
    actions = [e["action"] for e in client.get(f"/api/experiments/{exp['id']}/audit", headers=a).json()]
    assert "zenodo_prepare" in actions and "zenodo_publish" in actions
    assert prepare(client, a, exp).status_code == 409  # once published, it stays as it is
    assert client.delete(f"/api/experiments/{exp['id']}/zenodo", headers=a).status_code == 409


def exp_hash(client, h, exp) -> str:
    return client.get(f"/api/experiments/{exp['id']}/verify", headers=h).json()["content_hash"]


def test_publishing_never_changes_what_the_signature_covers(client, users, notebook, fz):
    a, b = users["alice"], users["bob"]
    exp = signed_record(client, a, notebook, title="Signed and published", witness=b)
    before = client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()
    with Session(engine) as s:
        row = s.get(Experiment, exp["id"])
        row_before = (row.title, row.description, row.status, row.updated_at, row.tags)
        sigs_before = [(g.id, g.meaning, g.content_hash) for g in s.exec(select(Signature).where(Signature.experiment_id == exp["id"]))]
    entries_before = [(e["id"], e["version"], e["content"]) for e in exp["entries"]]
    connect(client, a)
    assert prepare(client, a, exp).status_code == 200
    assert publish(client, a, exp).status_code == 200

    after = client.get(f"/api/experiments/{exp['id']}/verify", headers=a).json()
    assert after["intact"] and after["content_hash"] == before["content_hash"]
    assert all(s["matches_current"] for s in after["signatures"])
    assert [s["content_hash"] for s in after["signatures"]] == [s["content_hash"] for s in before["signatures"]]
    with Session(engine) as s:
        row = s.get(Experiment, exp["id"])
        assert (row.title, row.description, row.status, row.updated_at, row.tags) == row_before
        assert [(g.id, g.meaning, g.content_hash) for g in s.exec(select(Signature).where(Signature.experiment_id == exp["id"]))] == sigs_before
    now = client.get(f"/api/experiments/{exp['id']}", headers=a).json()
    assert [(e["id"], e["version"], e["content"]) for e in now["entries"]] == entries_before
    assert now["status"] == "witnessed"


def test_the_preview_shows_everything_that_becomes_public(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Preview check")
    p = client.get(f"/api/experiments/{exp['id']}/zenodo/preview", headers=a).json()
    d = p["details"]
    assert d["title"] == "Preview check" and d["licence"] == "cc-by-4.0" and d["access"] == "open"
    assert d["description"].startswith("To make 4-methoxybiphenyl by Suzuki coupling & compare bases.")
    assert "signed by Alice on" in d["description"] and "witnessed" not in d["description"]
    assert d["creators"][0]["name"] == "Alice" and d["keywords"] == ["suzuki", "cross-coupling"]
    assert d["include_comments"] is False
    assert p["references"] == ["10.1021/om100106e"]
    pdf, eln = p["files"]
    assert pdf["name"].endswith(".pdf") and eln["name"].endswith(".eln") and pdf["name"].startswith(exp["eid"])
    assert "files/crude-1H.jdx" in eln["contains"] and eln["comments"] == 1
    assert not any("discussion" in c for c in eln["contains"])
    with_comments = client.get(f"/api/experiments/{exp['id']}/zenodo/preview?comments=true", headers=a).json()
    assert "the discussion (1 comment)" in with_comments["files"][1]["contains"]


def test_the_discussion_goes_only_when_the_author_includes_it(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="With discussion")
    connect(client, a)
    pub = prepare(client, a, exp, include_comments=True).json()
    record = json.loads(next(eln_in(fz, pub["deposition_id"]).read(n) for n in eln_in(fz, pub["deposition_id"]).namelist()
                             if n.endswith("record.benchtop.json")))
    assert [c["body"] for c in record["comments"]] == ["Is the peak at 7.26 just CHCl3?"]


def test_a_draft_can_be_discarded_and_prepared_again(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Discard me")
    connect(client, a)
    first = prepare(client, a, exp).json()
    r = client.delete(f"/api/experiments/{exp['id']}/zenodo", headers=a)
    assert r.status_code == 200 and first["deposition_id"] not in fz.deps
    assert client.get(f"/api/experiments/{exp['id']}", headers=a).json()["publication"] is None
    assert "zenodo_discard" in [e["action"] for e in client.get(f"/api/experiments/{exp['id']}/audit", headers=a).json()]
    again = prepare(client, a, exp).json()
    assert again["status"] == "draft" and again["deposition_id"] != first["deposition_id"]


def test_changing_the_details_keeps_the_draft_and_its_doi(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Change details")
    connect(client, a)
    first = prepare(client, a, exp).json()
    second = prepare(client, a, exp, title="A better title", licence="cc0-1.0").json()
    assert second["deposition_id"] == first["deposition_id"] and second["doi"] == first["doi"]
    md = fz.deps[first["deposition_id"]]["metadata"]
    assert md["title"] == "A better title" and md["license"] == "cc0-1.0"
    assert len(fz.content[first["deposition_id"]]) == 2  # the old files were replaced, not added to
    assert details(client, a, exp)["title"] == "A better title"  # the preview opens with what was sent


# ====================================================================== who can, and what can be published

def test_only_signed_records_and_only_their_author(client, users, admin, notebook, fz):
    a, b, v = users["alice"], users["bob"], users["vic"]
    connect(client, a)
    draft = client.post("/api/experiments", headers=a, json={"title": "Still a draft", "notebook_id": notebook["id"]}).json()
    r = client.post(f"/api/experiments/{draft['id']}/zenodo/prepare", headers=a, json=details(client, a, draft))
    assert r.status_code == 409 and "Only signed records" in r.json()["detail"]
    exp = signed_record(client, a, notebook, title="Someone else's")
    d = details(client, a, exp)
    for who, code in ((b, 403), (admin, 403), (v, 403)):
        r = client.post(f"/api/experiments/{exp['id']}/zenodo/prepare", headers=who, json=d)
        assert r.status_code == code, r.text
    assert "author" in client.post(f"/api/experiments/{exp['id']}/zenodo/prepare", headers=b, json=d).json()["detail"]
    assert client.get(f"/api/experiments/{exp['id']}/zenodo", headers=b).json()["why_not"].startswith("Only the record's author")
    assert fz.calls == [("GET", "/api/deposit/depositions")]  # alice's token check, and nothing for anyone else


def test_api_tokens_cant_publish(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Not by script")
    connect(client, a)
    tok = client.post("/api/auth/tokens", headers=a, json={"name": "script", "scope": "write"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.post(f"/api/experiments/{exp['id']}/zenodo/prepare", headers=h, json=details(client, a, exp)).status_code == 403
    assert client.post(f"/api/experiments/{exp['id']}/zenodo/publish", headers=h, json={"confirm": True}).status_code == 403
    assert client.put("/api/auth/zenodo", headers=h, json={"token": GOOD}).status_code == 403


def test_a_record_witnessed_after_preparing_must_be_prepared_again(client, users, notebook, fz):
    a, b = users["alice"], users["bob"]
    exp = signed_record(client, a, notebook, title="Witnessed later")
    connect(client, a)
    prepare(client, a, exp)
    assert client.post(f"/api/experiments/{exp['id']}/witness", headers=b, json={"password": PW}).status_code == 200
    r = publish(client, a, exp)
    assert r.status_code == 409 and "Prepare it again" in r.json()["detail"] and not fz.published()
    pub = prepare(client, a, exp).json()
    assert publish(client, a, exp).status_code == 200
    record = json.loads(next(eln_in(fz, pub["deposition_id"]).read(n) for n in eln_in(fz, pub["deposition_id"]).namelist()
                             if n.endswith("record.benchtop.json")))
    assert [s["meaning"] for s in record["signatures"]] == ["author", "witness"]


def test_files_changed_on_zenodos_website_are_not_published(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Changed on Zenodo")
    connect(client, a)
    pub = prepare(client, a, exp).json()
    fz.content[pub["deposition_id"]]["extra.txt"] = b"added on the website"
    r = publish(client, a, exp)
    assert r.status_code == 409 and "different files" in r.json()["detail"] and not fz.published()


# ====================================================================== plain messages when something goes wrong

def test_no_token_says_where_to_connect_one(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="No token yet")
    r = prepare(client, a, exp)
    assert r.status_code == 409 and "Connect your sandbox.zenodo.org account first" in r.json()["detail"]
    assert client.get("/api/auth/zenodo", headers=a).json()["connected"] is False


def test_a_wrong_token_is_refused_and_never_kept(client, users, fz):
    a = users["alice"]
    r = connect(client, a, "x" * 40)
    assert r.status_code == 422 and "didn't accept your access token" in r.json()["detail"]
    assert connect(client, a, "short").status_code == 422
    assert client.get("/api/auth/zenodo", headers=a).json()["connected"] is False


def test_the_token_is_kept_encrypted_and_never_sent_back(client, users, fz):
    a = users["alice"]
    r = connect(client, a)
    assert r.status_code == 200 and r.json()["connected"] and r.json()["checked"] and r.json()["hint"] == GOOD[-4:]
    for body in (r.text, client.get("/api/auth/zenodo", headers=a).text):
        assert GOOD not in body
    with Session(engine) as s:
        acc = s.exec(select(ZenodoAccount)).first()
        assert acc.token.startswith("enc:") and GOOD not in acc.token and sitesettings.decrypt(acc.token) == GOOD
    assert client.put("/api/auth/zenodo", headers=users["vic"], json={"token": GOOD}).status_code == 403  # viewers don't publish
    assert client.delete("/api/auth/zenodo", headers=a).json()["connected"] is False


def test_an_expired_token_says_so_and_doesnt_sign_you_out(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Token expired")
    connect(client, a)
    fz.tokens.pop(GOOD)  # deleted on Zenodo, or expired
    r = prepare(client, a, exp)
    assert r.status_code == 502 and "didn't accept your access token" in r.json()["detail"]
    assert client.get("/api/auth/me", headers=a).status_code == 200


def test_a_token_without_the_actions_scope_cant_publish(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Write-only token")
    assert connect(client, a, WRITE_ONLY).status_code == 200
    assert prepare(client, a, exp).status_code == 200
    r = publish(client, a, exp)
    assert r.status_code == 502 and "deposit:actions" in r.json()["detail"] and not fz.published()
    assert client.get(f"/api/experiments/{exp['id']}/zenodo", headers=a).json()["publication"]["status"] == "draft"


def test_zenodo_unreachable(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Offline")
    connect(client, a)
    fz.mode = "down"
    r = prepare(client, a, exp)
    assert r.status_code == 503 and "Couldn't reach sandbox.zenodo.org" in r.json()["detail"] and "Nothing was sent" in r.json()["detail"]
    fz.mode = "ok"
    assert prepare(client, a, exp).json()["status"] == "draft"


def test_connecting_while_zenodo_is_unreachable_keeps_the_token_unchecked(client, users, fz):
    fz.mode = "down"
    r = connect(client, users["alice"])
    assert r.status_code == 200 and r.json()["connected"] and r.json()["checked"] is False


def test_an_upload_cut_off_part_way_can_be_retried_or_discarded(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Cut off")
    connect(client, a)
    fz.cut_upload = 2
    r = prepare(client, a, exp)
    assert r.status_code == 502, r.text
    msg = r.json()["detail"]
    assert "cut off while sending" in msg and "prepare it again to retry, or discard it" in msg
    st = client.get(f"/api/experiments/{exp['id']}/zenodo", headers=a).json()["publication"]
    assert st["status"] == "failed" and st["error"] == msg and len(fz.content[st["deposition_id"]]) == 1
    assert publish(client, a, exp).status_code == 409  # a half-sent draft can't be published
    fz.cut_upload = None
    again = prepare(client, a, exp).json()
    assert again["status"] == "draft" and again["deposition_id"] == st["deposition_id"] and len(fz.content[st["deposition_id"]]) == 2
    assert "zenodo_prepare_failed" in [e["action"] for e in client.get(f"/api/experiments/{exp['id']}/audit", headers=a).json()]


def test_a_failed_draft_can_be_discarded(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Cut off, then discarded")
    connect(client, a)
    fz.cut_upload = 1
    dep = prepare(client, a, exp)
    assert dep.status_code == 502
    assert client.delete(f"/api/experiments/{exp['id']}/zenodo", headers=a).status_code == 200 and not fz.deps


def test_a_damaged_upload_is_noticed(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Damaged")
    connect(client, a)
    fz.damage_upload = True
    r = prepare(client, a, exp)
    assert r.status_code == 502 and "arrived at Zenodo damaged" in r.json()["detail"]


def test_files_too_large(client, users, notebook, fz, monkeypatch):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Too large")
    connect(client, a)
    fz.max_file = 1000  # Zenodo says no
    r = prepare(client, a, exp)
    assert r.status_code == 413 and "too large for sandbox.zenodo.org" in r.json()["detail"]
    monkeypatch.setattr(publishing, "MAX_FILE_BYTES", 100)  # Benchtop knows before sending anything
    calls = len(fz.calls)
    r = prepare(client, a, exp)
    assert r.status_code == 413 and "Zenodo takes up to" in r.json()["detail"] and len(fz.calls) == calls


def test_zenodo_refusing_the_details_says_why(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Refused")
    connect(client, a)
    sitesettings.update("zenodo", {"community": "Not Valid!"})  # past the admin page's own check
    r = prepare(client, a, exp)
    assert r.status_code == 502 and "refused it: communities: Invalid community identifier" in r.json()["detail"]


def test_a_publish_whose_answer_was_lost_is_still_recorded(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Answer lost")
    connect(client, a)
    pub = prepare(client, a, exp).json()
    fz.publish_answer_lost = True
    r = publish(client, a, exp)
    assert r.status_code == 200 and r.json()["status"] == "published" and r.json()["doi"] == pub["doi"]


def test_the_details_are_checked(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Checked details")
    connect(client, a)
    assert prepare(client, a, exp, title="  ").status_code == 422
    assert prepare(client, a, exp, creators=[]).status_code == 422
    r = prepare(client, a, exp, creators=[{"name": "Ruiz, Mateo", "orcid": "0000-0002-1825-0098"}])
    assert r.status_code == 422 and r.json()["detail"].startswith("Ruiz, Mateo: That ORCID iD has a typo")
    assert prepare(client, a, exp, licence="all-rights-reserved").status_code == 422
    past = (date.today() - timedelta(days=1)).isoformat()
    assert prepare(client, a, exp, access="embargoed", embargo_date=past).status_code == 422
    assert fz.calls == [("GET", "/api/deposit/depositions")]  # the details are checked before anything is sent
    later = (date.today() + timedelta(days=200)).isoformat()
    pub = prepare(client, a, exp, access="embargoed", embargo_date=later).json()
    md = fz.deps[pub["deposition_id"]]["metadata"]
    assert md["access_right"] == "embargoed" and md["embargo_date"] == later


# ====================================================================== settings, demo, unit bits

def test_admin_chooses_the_site_and_can_turn_it_off(client, users, admin, notebook, fz):
    a = users["alice"]
    assert client.get("/api/admin/zenodo", headers=a).status_code == 403
    assert client.put("/api/admin/zenodo", headers=admin, json={"site": "elsewhere"}).status_code == 422
    assert client.put("/api/admin/zenodo", headers=admin, json={"community": "Has Spaces"}).status_code == 422
    r = client.put("/api/admin/zenodo", headers=admin, json={"community": "https://zenodo.org/communities/ruiz-lab"})
    assert r.json()["community"] == "ruiz-lab"
    exp = signed_record(client, a, notebook, title="In a community")
    connect(client, a)
    pub = prepare(client, a, exp).json()
    assert fz.deps[pub["deposition_id"]]["metadata"]["communities"] == [{"identifier": "ruiz-lab"}]
    client.delete(f"/api/experiments/{exp['id']}/zenodo", headers=a)
    client.put("/api/admin/zenodo", headers=admin, json={"enabled": False})
    r = prepare(client, a, exp)
    assert r.status_code == 409 and "turned off" in r.json()["detail"]
    assert client.get(f"/api/experiments/{exp['id']}/zenodo", headers=a).json()["unavailable"]
    actions = [e.action for e in Session(engine).exec(select(AuditEvent).where(AuditEvent.action == "settings_zenodo"))]
    assert actions


def test_a_setting_from_the_environment_is_fixed(client, admin, fz, monkeypatch):
    monkeypatch.setenv("BENCHTOP_ZENODO_URL", "https://sandbox.zenodo.org")
    out = client.get("/api/admin/zenodo", headers=admin).json()
    assert out["url"] == SANDBOX and "url" in out["fixed"]
    assert client.put("/api/admin/zenodo", headers=admin, json={"site": "zenodo"}).status_code == 409


def test_a_token_for_the_other_site_is_not_used(client, users, admin, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Other site")
    connect(client, a)  # for the sandbox
    client.put("/api/admin/zenodo", headers=admin, json={"site": "zenodo"})
    st = client.get("/api/auth/zenodo", headers=a).json()
    assert st["connected"] is False and st["token_site"] == "sandbox.zenodo.org"
    r = prepare(client, a, exp)
    assert r.status_code == 409 and "Your Zenodo token is for sandbox.zenodo.org" in r.json()["detail"]


def test_a_token_that_cant_be_read_any_more_asks_for_it_again(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Secret key changed")
    connect(client, a)
    with Session(engine) as s:
        acc = s.exec(select(ZenodoAccount)).first()
        acc.token = "enc:gAAAAAB-not-readable-with-this-key"
        s.add(acc)
        s.commit()
    assert client.get("/api/auth/zenodo", headers=a).json()["unreadable"]
    assert "can't be read any more" in prepare(client, a, exp).json()["detail"]


def test_the_demo_lab_never_publishes(client, users, notebook, fz, monkeypatch):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Demo")
    connect(client, a)
    calls = len(fz.calls)
    monkeypatch.setattr(settings, "demo", True)
    r = prepare(client, a, exp)
    assert r.status_code == 409 and "demo lab" in r.json()["detail"]
    assert connect(client, a).status_code == 409
    assert client.get(f"/api/experiments/{exp['id']}/zenodo/preview", headers=a).status_code == 200  # looking is fine
    assert len(fz.calls) == calls


def test_benchtop_stopping_part_way_leaves_a_draft_that_can_be_retried(client, users, notebook, fz):
    a = users["alice"]
    exp = signed_record(client, a, notebook, title="Stopped part-way")
    connect(client, a)
    with Session(engine) as s:  # as if Benchtop stopped while sending it
        s.add(Publication(experiment_id=exp["id"], server=SANDBOX, user_id=exp["owner"]["id"], status="working"))
        s.commit()
    st = client.get(f"/api/experiments/{exp['id']}/zenodo", headers=a).json()["publication"]
    assert st["status"] == "failed" and "Benchtop stopped" in st["error"]
    assert prepare(client, a, exp).json()["status"] == "draft"


@pytest.mark.parametrize("name, zenodo_name", [("Hanna Weiss", "Weiss, Hanna"), ("Mateo Ruiz García", "García, Mateo Ruiz"),
                                                ("Weiss, Hanna", "Weiss, Hanna"), ("Prince", "Prince"), ("  ", "")])
def test_names_are_written_family_name_first(name, zenodo_name):
    assert publishing.family_first(name) == zenodo_name


def test_zenodo_errors_are_read_in_both_shapes():
    r = zenodo.httpx.Response(400, json={"message": "Validation error.", "errors": [{"field": "metadata.title", "message": "Required."}]})
    assert zenodo._messages(r) == "title: Required."
    r = zenodo.httpx.Response(400, json={"status": 400, "message": "Something odd."})
    assert zenodo._messages(r) == "Something odd."


def test_the_token_only_goes_to_its_own_zenodo_site(fz):
    c = zenodo.Client(SANDBOX, GOOD)
    with pytest.raises(zenodo.ZenodoError, match="another address"):
        c.upload("https://evil.example/api/files/abc", "x.pdf", b"%PDF-", "0" * 32)
    assert not fz.calls
