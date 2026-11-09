"""0.35: a simpler first ten minutes.

A walk-through on a fresh install found that an untouched Work-up & purification section was recorded as flash
column chromatography: new sections started with method "column", so a student who never chose a method signed a
record whose export and draft write-up said "Purification by flash column chromatography afforded…". New sections now
start with no method; records made before keep exactly what they have."""
from __future__ import annotations

from tests.test_v34_archive import sign


def _new(client, h, nb, title="Fischer esterification"):
    exp = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"]}).json()
    return client.get(f"/api/experiments/{exp['id']}", headers=h).json()


def _purification(exp):
    return next(e for e in exp["entries"] if e["kind"] == "purification")


def test_a_new_experiment_has_no_purification_method_until_one_is_chosen(client, users, notebook):
    a = users["alice"]
    exp = _new(client, a, notebook)
    assert _purification(exp)["content"]["method"] == ""
    # a purification section added later starts the same way
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=a, json={"kind": "purification", "title": "Second column"})
    assert r.status_code in (200, 201), r.text
    assert r.json()["content"]["method"] == ""


def test_an_untouched_purification_section_is_not_written_up_or_exported_as_a_column(client, users, notebook):
    a = users["alice"]
    exp = _new(client, a, notebook)
    pur = _purification(exp)
    # the student fills in only the isolated mass and appearance, never the method, and signs
    r = client.put(f"/api/entries/{pur['id']}", headers=a, json={"content": {**pur["content"], "isolated_mg": 1100, "appearance": "colourless oil"}})
    assert r.status_code == 200, r.text
    sign(client, a, exp)
    draft = client.get(f"/api/experiments/{exp['id']}/writeup", headers=a).json()
    assert "column" not in draft["text"].lower() and "chromatography" not in draft["text"].lower()
    assert "purification method" in draft["missing"]
    html = client.get(f"/api/experiments/{exp['id']}/export", headers=a).text
    assert "Flash column" not in html and "1100 mg" in html
    md = client.get(f"/api/experiments/{exp['id']}/export?format=md", headers=a).text
    assert "Flash column" not in md and "purification:" not in md and "colourless oil" in md


def test_a_chosen_method_and_older_records_with_column_are_kept_as_they_are(client, users, notebook):
    a = users["alice"]
    exp = _new(client, a, notebook)
    pur = _purification(exp)
    client.put(f"/api/entries/{pur['id']}", headers=a, json={"content": {**pur["content"], "method": "column", "eluent": "10% EtOAc/hexanes"}})
    exp = client.get(f"/api/experiments/{exp['id']}", headers=a).json()
    assert _purification(exp)["content"]["method"] == "column"
    draft = client.get(f"/api/experiments/{exp['id']}/writeup", headers=a).json()
    assert "flash column chromatography" in draft["text"]
    assert "Flash column chromatography" in client.get(f"/api/experiments/{exp['id']}/export", headers=a).text


# ====================================================================== setting up the lab (admins)

def test_whoever_makes_a_group_can_lead_it(client, admin, users):
    """0.35: "I lead this group", ticked by default in the app, makes the admin creating the group its lead."""
    led = client.post("/api/groups", headers=admin, json={"name": "Carter group (v35)", "i_lead": True})
    assert led.status_code == 201, led.text
    assert [(m["username"], m["role"]) for m in led.json()["members"]] == [("admin", "lead")]
    assert led.json()["my_role"] == "lead"
    plain = client.post("/api/groups", headers=admin, json={"name": "Process chemistry (v35)"}).json()
    assert plain["members"] == [], "without it, nobody is added (as before)"
    # only admins make groups, with or without it
    assert client.post("/api/groups", headers=users["alice"], json={"name": "Alice's own (v35)", "i_lead": True}).status_code == 403
    events = client.get("/api/audit?limit=50&object_type=group", headers=admin).json()
    assert any(e["action"] == "group_member" and e["object_id"] == led.json()["id"] and e["details"]["role"] == "lead" for e in events), \
        "the lead isn't in the audit trail"


def test_the_sign_in_message_gives_the_servers_address(client, admin, users, monkeypatch):
    """0.35: the "can sign in now" message used the address of the computer the admin was on ("localhost" on the
    server itself). It now says the server's address, as the phone link does: the public address, then the
    address on the network, then the one in use."""
    from app.config import settings
    monkeypatch.setattr(settings, "public_url", "")
    monkeypatch.setattr(settings, "lan_url", "")
    assert client.get("/api/admin/address", headers=users["alice"]).status_code == 403
    local = client.get("http://localhost/api/admin/address", headers=admin).json()
    assert local["address"] is None and local["reachable"] is False and "./benchtop lab" in local["note"]
    other = client.get("/api/admin/address", headers=admin).json()  # opened at some other address: that one is used
    assert other["address"] is None and other["reachable"] is True
    monkeypatch.setattr(settings, "lan_url", "http://192.168.1.20:8000")
    assert client.get("http://localhost/api/admin/address", headers=admin).json()["address"] == "http://192.168.1.20:8000"
    monkeypatch.setattr(settings, "public_url", "https://eln.example.ac.uk")
    assert client.get("http://localhost/api/admin/address", headers=admin).json() == {"address": "https://eln.example.ac.uk", "reachable": True, "note": ""}


def test_the_first_record_card_can_be_put_away_for_good(client, users):
    h = users["bob"]
    me = client.patch("/api/auth/me", headers=h, json={"preferences": {"first_steps_dismissed": True}}).json()
    assert me["preferences"]["first_steps_dismissed"] is True
    me = client.patch("/api/auth/me", headers=h, json={"preferences": {"first_steps_dismissed": "yes"}}).json()
    assert me["preferences"].get("first_steps_dismissed") != "yes", "only true or false is kept"


def test_setup_leaves_the_examples_out_and_marks_the_retired_built_in_admin(tmp_path):
    """On a fresh database: setup without the examples adds no example accounts, and Admin → Users can tell the
    built-in admin that setup switched off from a person."""
    import os
    import subprocess
    import sys
    code = r'''
import os, sys
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    r = c.post("/api/setup", json={"full_name": "Helen Carter", "username": "helen", "password": "violet-anvil-river"})
    assert r.status_code == 201, r.text
    assert r.json()["demo_loaded"] is False
    h = {"Authorization": "Bearer " + r.json()["token"]}
    users = {u["username"]: u for u in c.get("/api/users", headers=h).json()}
    assert set(users) == {"admin", "helen"}, users
    assert users["admin"]["retired_builtin"] is True and users["admin"]["active"] is False
    assert "retired_builtin" not in users["helen"]
    assert c.post("/api/auth/login", json={"username": "alice", "password": "benchtop123"}).status_code == 401
print("ok")
'''
    env = {**os.environ, "BENCHTOP_DATA_DIR": str(tmp_path), "BENCHTOP_ADMIN_PASSWORD": "admin", "BENCHTOP_BACKUP_INTERVAL_HOURS": "0"}
    out = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(os.path.dirname(__file__)), env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-2000:]
