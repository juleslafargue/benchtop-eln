"""v0.10: API tokens, webhooks and plugin files (custom sections, file readers)."""
import hashlib
import hmac
import json

import pytest

from app import webhooks
from tests.test_v2 import new_exp

PW = "lab-bench-pw-2026"


# ---------------------------------------------------------------- API tokens

def test_api_tokens_read_write_and_limits(client, users, notebook):
    alice = users["alice"]
    r = client.post("/api/auth/tokens", headers=alice, json={"name": "Plate reader", "scope": "read", "days": 30})
    assert r.status_code == 201, r.text
    raw = r.json()["token"]
    assert raw.startswith("bt_") and r.json()["prefix"] == raw[:10]
    assert "token" not in client.get("/api/auth/tokens", headers=alice).json()[0]  # shown once only
    rt = {"Authorization": f"Bearer {raw}"}
    assert client.get("/api/auth/me", headers=rt).json()["username"] == "alice"
    assert client.get("/api/notebooks", headers=rt).status_code == 200
    # read-only token can't write
    assert client.post("/api/notebooks", headers=rt, json={"name": "nope"}).status_code == 403
    # a write token can, but never signs, manages accounts or makes more tokens
    w = client.post("/api/auth/tokens", headers=alice, json={"name": "ELN sync", "scope": "write", "days": None}).json()
    wt = {"Authorization": f"Bearer {w['token']}"}
    exp = client.post("/api/experiments", headers=wt, json={"title": "From a script", "notebook_id": notebook["id"]})
    assert exp.status_code == 201, exp.text
    eid = exp.json()["id"]
    assert client.post(f"/api/experiments/{eid}/sign", headers=wt, json={"password": PW}).status_code == 403
    assert client.post("/api/auth/tokens", headers=wt, json={"name": "x"}).status_code == 403
    assert client.post("/api/auth/password", headers=wt, json={"current_password": PW, "new_password": "whatever-long-pass"}).status_code == 403
    assert client.get("/api/users", headers=wt).status_code == 403
    # the audit trail names the person behind the token
    ev = client.get(f"/api/experiments/{eid}/audit", headers=alice).json()
    assert ev[-1]["username"] == "alice"
    # revoke
    assert client.delete(f"/api/auth/tokens/{w['id']}", headers=alice).status_code == 200
    assert client.get("/api/auth/me", headers=wt).status_code == 401
    # someone else can't revoke alice's token; a garbage token is just refused
    tid = client.get("/api/auth/tokens", headers=alice).json()[0]["id"]
    assert client.delete(f"/api/auth/tokens/{tid}", headers=users["bob"]).status_code == 404
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer bt_nottoken"}).status_code == 401
    # viewers only get read tokens
    assert client.post("/api/auth/tokens", headers=users["vic"], json={"name": "v", "scope": "write"}).status_code == 403


# ---------------------------------------------------------------- webhooks

@pytest.fixture()
def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(webhooks, "synchronous", True)
    monkeypatch.setattr(webhooks, "sender", lambda url, body, headers: sent.append((url, body, headers)) or 200)
    return sent


def test_webhooks_fire_after_commit_and_are_signed(client, admin, users, notebook, capture):
    r = client.post("/api/webhooks", headers=admin, json={"name": "Lab bot", "url": "https://example.org/hook",
                                                         "events": ["experiment.signed", "experiment.witnessed", "comment.created"]})
    assert r.status_code == 201, r.text
    hook, secret = r.json(), r.json()["secret"]
    exp = new_exp(client, users["alice"], notebook)
    assert capture == []  # experiment.created isn't subscribed
    client.post(f"/api/experiments/{exp['id']}/sign", headers=users["alice"], json={"password": PW})
    url, body, headers = capture[-1]
    p = json.loads(body)
    assert p["event"] == "experiment.signed" and p["experiment"]["eid"] == exp["eid"] and p["actor"]["username"] == "alice"
    assert "entries" not in p["experiment"] and "content" not in json.dumps(p)  # identifiers, never record content
    assert headers["X-Benchtop-Signature"] == "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # a failed action (wrong password) sends nothing
    n = len(capture)
    client.post(f"/api/experiments/{exp['id']}/witness", headers=users["bob"], json={"password": "wrong-password-x"})
    assert len(capture) == n
    client.post(f"/api/experiments/{exp['id']}/witness", headers=users["bob"], json={"password": PW})
    assert json.loads(capture[-1][1])["event"] == "experiment.witnessed"
    # Slack format is plain text with a link
    client.patch(f"/api/webhooks/{hook['id']}", headers=admin, json={"format": "slack"})
    client.post(f"/api/experiments/{exp['id']}/comments", headers=users["bob"], json={"body": "Nice work"})
    slack = json.loads(capture[-1][1])
    assert list(slack) == ["text"] and "commented on" in slack["text"] and exp["eid"] in slack["text"]
    # history and test button
    d = client.get(f"/api/webhooks/{hook['id']}/deliveries", headers=admin).json()
    assert d and d[0]["ok"]
    assert client.post(f"/api/webhooks/{hook['id']}/test", headers=admin).json()["ok"]
    # only admins manage webhooks; bad input is refused
    assert client.get("/api/webhooks", headers=users["alice"]).status_code == 403
    assert client.post("/api/webhooks", headers=admin, json={"url": "ftp://x", "events": ["experiment.signed"]}).status_code == 422
    assert client.post("/api/webhooks", headers=admin, json={"url": "https://x.org", "events": ["nope"]}).status_code == 422
    client.delete(f"/api/webhooks/{hook['id']}", headers=admin)


def test_private_notebooks_stay_out_of_webhooks(client, admin, users, capture):
    hook = client.post("/api/webhooks", headers=admin, json={"url": "https://example.org/h2", "events": ["experiment.created"]}).json()
    nb = client.post("/api/notebooks", headers=users["alice"], json={"name": "Secret", "visibility": "private"}).json()
    new_exp(client, users["alice"], nb)
    assert capture == []
    client.patch(f"/api/webhooks/{hook['id']}", headers=admin, json={"include_private": True})
    new_exp(client, users["alice"], nb)
    assert len(capture) == 1
    client.delete(f"/api/webhooks/{hook['id']}", headers=admin)


def test_failing_receiver_is_recorded(client, admin, users, notebook, monkeypatch):
    monkeypatch.setattr(webhooks, "synchronous", True)
    monkeypatch.setattr(webhooks, "sender", lambda *a: 500)
    hook = client.post("/api/webhooks", headers=admin, json={"url": "https://example.org/down", "events": ["experiment.created"]}).json()
    new_exp(client, users["alice"], notebook)
    d = client.get(f"/api/webhooks/{hook['id']}/deliveries", headers=admin).json()[0]
    assert not d["ok"] and d["status_code"] == 500 and d["attempts"] == 3
    client.delete(f"/api/webhooks/{hook['id']}", headers=admin)


# ---------------------------------------------------------------- plugins

GLOVE = """
[plugin]
id = "test-glove"
name = "Test glovebox"
version = "0.1"
api = 1

[[sections]]
id = "glove"
name = "Glove log"
  [[sections.fields]]
  id = "o2"
  label = "O2"
  type = "number"
  unit = "ppm"
  required = true
  [[sections.fields]]
  id = "box"
  label = "Box"
  type = "select"
  options = ["A", "B"]
  [[sections.fields]]
  id = "ok"
  label = "Purifier checked"
  type = "checkbox"
  required = true
  [[sections.tables]]
  id = "items"
  label = "Items"
    [[sections.tables.columns]]
    id = "item"
    label = "Item"
    type = "text"

[[readers]]
id = "plate"
name = "Plate CSV"
filename = "plate"
x = "Well"
y = "OD600"
header = true
"""


def _install(client, admin, text, name="p.toml"):
    return client.post("/api/plugins", headers=admin, files={"file": (name, text.encode(), "application/toml")})


def test_plugin_files_are_checked_and_installed(client, admin, users):
    bad = _install(client, admin, "[plugin]\nid='Bad Id'\napi=1\n")
    assert bad.status_code == 422 and "lowercase" in bad.json()["detail"]
    assert "select needs" in _install(client, admin, GLOVE.replace('options = ["A", "B"]', "")).json()["detail"]
    assert "API 2" in _install(client, admin, GLOVE.replace("api = 1", "api = 2")).json()["detail"]
    chk = client.post("/api/plugins/check", headers=admin, files={"file": ("p.toml", GLOVE.encode())}).json()
    assert chk["ok"] and chk["plugin"]["sections"][0]["name"] == "Glove log"
    r = _install(client, admin, GLOVE)
    assert r.status_code == 201, r.text
    lst = client.get("/api/plugins", headers=admin).json()["plugins"]
    mine = next(p for p in lst if p["id"] == "test-glove")
    assert mine["enabled"] and not mine["bundled"]
    ex = next(p for p in lst if p["id"] == "glovebox-log")
    assert ex["bundled"] and not ex["enabled"] and not ex["errors"]  # examples ship switched off
    keys = [s["key"] for s in client.get("/api/plugins/sections", headers=users["alice"]).json()]
    assert "test-glove/glove" in keys and "glovebox-log/glovebox" not in keys
    assert client.get("/api/plugins/readers", headers=users["alice"]).json()[0]["x"] == "Well"
    assert client.get("/api/plugins", headers=users["alice"]).status_code == 403
    assert _install(client, users["alice"], GLOVE).status_code == 403
    # turning an example on
    client.post("/api/plugins/glovebox-log/enabled", headers=admin, json={"enabled": True})
    assert "glovebox-log/glovebox" in [s["key"] for s in client.get("/api/plugins/sections", headers=users["alice"]).json()]
    client.post("/api/plugins/glovebox-log/enabled", headers=admin, json={"enabled": False})
    assert client.delete("/api/plugins/glovebox-log", headers=admin).status_code == 404  # bundled: switch off, not delete


def test_custom_sections_store_their_definition_and_gate_signing(client, admin, users, notebook):
    _install(client, admin, GLOVE)
    exp = new_exp(client, users["alice"], notebook)
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=users["alice"], json={"kind": "custom", "plugin_section": "test-glove/glove"})
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["title"] == "Glove log" and e["content"]["schema"]["fields"][0]["unit"] == "ppm"
    assert e["content"]["values"] == {"o2": None, "box": None, "ok": False}
    # values are typed and checked against the definition; unknown keys and bad options are dropped
    c = e["content"]
    c["values"] = {"o2": "0.8", "box": "Z", "ok": False, "evil": "<script>"}
    c["rows"] = {"items": [{"item": "Pd2(dba)3", "junk": 1}]}
    up = client.put(f"/api/entries/{e['id']}", headers=users["alice"], json={"content": c}).json()
    assert up["content"]["values"] == {"o2": 0.8, "box": None, "ok": False}
    assert up["content"]["rows"] == {"items": [{"item": "Pd2(dba)3"}]}
    # a tampered definition is refused
    c2 = json.loads(json.dumps(up["content"]))
    c2["schema"]["fields"][0]["type"] = "html"
    assert client.put(f"/api/entries/{e['id']}", headers=users["alice"], json={"content": c2}).status_code == 422
    # can't sign until required items are done
    s = client.post(f"/api/experiments/{exp['id']}/sign", headers=users["alice"], json={"password": PW})
    assert s.status_code == 422 and "Purifier checked" in s.json()["detail"]
    c = up["content"]
    c["values"]["ok"] = True
    client.put(f"/api/entries/{e['id']}", headers=users["alice"], json={"content": c})
    # the record survives the plugin being removed: searchable, exportable, signable
    client.delete("/api/plugins/test-glove", headers=admin)
    assert client.post(f"/api/experiments/{exp['id']}/entries", headers=users["alice"],
                       json={"kind": "custom", "plugin_section": "test-glove/glove"}).status_code == 422
    assert client.get("/api/search", headers=users["alice"], params={"q": "Pd2(dba)3"}).json()["experiments"]
    html = client.get(f"/api/experiments/{exp['id']}/export", headers=users["alice"]).text
    assert "Purifier checked" in html and "0.8 ppm" in html
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=users["alice"], json={"password": PW}).status_code == 200


def test_bundled_examples_are_valid():
    from app import plugins
    ps = [p for p in plugins.discover() if p.bundled]
    assert {p.id for p in ps} >= {"glovebox-log", "hydrogenation-checklist", "instrument-readers", "cell-biology", "pharmacology"}
    assert all(not p.errors for p in ps), [p.errors for p in ps]


def test_backups_carry_the_labs_plugin_files(client, admin):
    import zipfile

    from app import backup, plugins
    _install(client, admin, GLOVE.replace('id = "test-glove"', 'id = "backup-glove"'))
    b = backup.create_backup()
    assert "plugins/backup-glove.toml" in zipfile.ZipFile(b["path"]).namelist()
    (plugins.user_dir() / "backup-glove.toml").unlink()
    backup.restore(b["path"])
    assert (plugins.user_dir() / "backup-glove.toml").exists()
