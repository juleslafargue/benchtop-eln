"""0.36.4: plugin sections (cell culture, western blot, dose–response, PK…), found by the 0.36.2 stress test.

- Text is kept as typed. It was stripped on every save, and the page took the stripped copy back while the person was
  still typing, so a pause after a space or Enter ate it ("DMEM " + "high glucose" became "DMEMhigh glucose").
  Spaces alone still don't fill a required item.
- A section's definition (its fields, and which are required) comes from the server, never from the request: a new
  section gets the installed plugin's, an edit keeps the one stored with the section. Sending the definition without
  "required" let a record be signed with its checklist empty, and a section from a made-up plugin was accepted.
- Sections from a plugin that has since been changed or removed still load, save, export and sign as before."""
from __future__ import annotations

import copy

import pytest

PW = "lab-bench-pw-2026"

ASSAY = """
[plugin]
id = "test-assay-364"
name = "Test assay"
version = "1"
api = 1

[[sections]]
id = "assay"
name = "Assay"
  [[sections.fields]]
  id = "medium"
  label = "Medium"
  type = "text"
  [[sections.fields]]
  id = "notes"
  label = "Notes"
  type = "longtext"
  [[sections.fields]]
  id = "cell_line"
  label = "Cell line"
  type = "text"
  required = true
  [[sections.fields]]
  id = "checked"
  label = "Checked by a second person"
  type = "checkbox"
  required = true
  [[sections.tables]]
  id = "wells"
  label = "Wells"
  min_rows = 1
    [[sections.tables.columns]]
    id = "well"
    label = "Well"
    type = "text"
    required = true
"""


@pytest.fixture(scope="module")
def assay(client, admin):
    r = client.post("/api/plugins", headers=admin, files={"file": ("assay.toml", ASSAY.encode(), "application/toml")})
    assert r.status_code == 201, r.text
    return "test-assay-364/assay"


def _exp(client, h, nb, title):
    r = client.post("/api/experiments", headers=h, json={"title": title, "notebook_id": nb["id"], "sections": "no_chemistry"})
    assert r.status_code == 201, r.text
    return r.json()


def _section(client, h, exp, key):
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=h, json={"kind": "custom", "plugin_section": key})
    assert r.status_code == 201, r.text
    return r.json()


def _put(client, h, entry, content):
    return client.put(f"/api/entries/{entry['id']}", headers=h, json={"content": content})


def test_spaces_and_new_lines_are_kept_as_typed(client, users, notebook, assay):
    a = users["alice"]
    e = _section(client, a, _exp(client, a, notebook, "Typing in a plugin section"), assay)
    c = e["content"]
    # what the page sends a second after a space, and after Enter, while the person is still typing
    for medium, notes in (("DMEM ", "line one\n"), ("DMEM high glucose", "line one\nline two"), ("  indented", "\n\nafter two blank lines  ")):
        c = {**c, "values": {**c["values"], "medium": medium, "notes": notes}}
        r = _put(client, a, e, c)
        assert r.status_code == 200, r.text
        assert r.json()["content"]["values"]["medium"] == medium
        assert r.json()["content"]["values"]["notes"] == notes
    rows = {"wells": [{"well": "A1 "}]}
    assert _put(client, a, e, {**c, "rows": rows}).json()["content"]["rows"] == rows


def test_spaces_alone_dont_fill_a_required_item(client, users, notebook, assay):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Only spaces")
    e = _section(client, a, exp, assay)
    c = {**e["content"], "values": {**e["content"]["values"], "cell_line": "   \n", "checked": True}, "rows": {"wells": [{"well": " "}]}}
    assert _put(client, a, e, c).status_code == 200
    r = client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW})
    assert r.status_code == 422 and "Cell line" in r.json()["detail"] and "Wells" in r.json()["detail"], r.text
    # Markdown export doesn't show spaces as a value
    from app import plugins
    assert "Cell line" not in plugins.text_of(c)
    assert plugins.text_of({**c, "values": {**c["values"], "cell_line": " HEK293T  "}}).startswith("Cell line: HEK293T\n")


def test_the_definition_sent_with_an_edit_is_never_used(client, users, notebook, assay):
    a = users["alice"]
    exp = _exp(client, a, notebook, "Checklist skipped")
    e = _section(client, a, exp, assay)
    loose = copy.deepcopy(e["content"]["schema"])
    for f in loose["fields"]:
        f.pop("required", None)
    for t in loose["tables"]:
        t["min_rows"] = 0
        for col in t["columns"]:
            col.pop("required", None)
    loose["fields"][0]["label"] = "Medium (relabelled)"
    r = _put(client, a, e, {**e["content"], "schema": loose})
    assert r.status_code == 422, r.text
    stored = client.get(f"/api/experiments/{exp['id']}", headers=a).json()["entries"]
    stored = next(x for x in stored if x["id"] == e["id"])["content"]["schema"]
    assert stored == e["content"]["schema"]
    s = client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW})
    assert s.status_code == 422 and "Cell line" in s.json()["detail"], s.text
    # sent back unchanged, as the app does (numbers may come back as 0 rather than 0.0), or left out: both save
    ok = _put(client, a, e, {**e["content"], "values": {**e["content"]["values"], "medium": "RPMI"}})
    assert ok.status_code == 200 and ok.json()["content"]["schema"] == e["content"]["schema"]
    without = _put(client, a, ok.json(), {"values": {"medium": "RPMI 1640"}, "rows": {}})
    assert without.status_code == 200 and without.json()["content"]["schema"] == e["content"]["schema"]


def test_a_new_section_gets_the_installed_plugins_definition(client, users, notebook, assay):
    a = users["alice"]
    exp = _exp(client, a, notebook, "New section by script")
    fake = {"plugin": "made-up", "id": "anything", "name": "Official safety sign-off", "title": "Fake", "description": "",
            "fields": [{"id": "ok", "label": "Approved by the safety officer", "type": "checkbox"}], "tables": []}
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=a, json={"kind": "custom", "content": {"schema": fake, "values": {"ok": True}}})
    assert r.status_code == 422, r.text
    off = {**fake, "plugin": "glovebox-log", "id": "glovebox"}  # a bundled example that is switched off here
    assert client.post(f"/api/experiments/{exp['id']}/entries", headers=a,
                       json={"kind": "custom", "content": {"schema": off, "values": {}}}).status_code == 422
    # the installed plugin, its definition loosened: the installed one is used
    plugin, sid = assay.split("/")
    loose = {"plugin": plugin, "id": sid, "name": "Assay", "fields": [{"id": "medium", "label": "Medium", "type": "text"}], "tables": []}
    r = client.post(f"/api/experiments/{exp['id']}/entries", headers=a,
                    json={"kind": "custom", "content": {"schema": loose, "values": {"medium": "DMEM "}}})
    assert r.status_code == 201, r.text
    sch = r.json()["content"]["schema"]
    assert [f["id"] for f in sch["fields"]] == ["medium", "notes", "cell_line", "checked"]
    assert next(f for f in sch["fields"] if f["id"] == "cell_line")["required"] is True
    assert r.json()["content"]["values"]["medium"] == "DMEM "
    s = client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW})
    assert s.status_code == 422 and "Cell line" in s.json()["detail"]


def test_a_section_from_a_removed_plugin_still_saves_exports_and_signs(client, users, notebook, admin):
    a = users["alice"]
    text = ASSAY.replace("test-assay-364", "gone-assay-364")
    assert client.post("/api/plugins", headers=admin, files={"file": ("gone.toml", text.encode())}).status_code == 201
    exp = _exp(client, a, notebook, "Plugin removed later")
    e = _section(client, a, exp, "gone-assay-364/assay")
    assert client.delete("/api/plugins/gone-assay-364", headers=admin).status_code == 200
    c = {**e["content"], "values": {**e["content"]["values"], "medium": "DMEM ", "cell_line": "HeLa", "checked": True},
         "rows": {"wells": [{"well": "B2"}]}}
    r = _put(client, a, e, c)
    assert r.status_code == 200 and r.json()["content"]["schema"] == e["content"]["schema"], r.text
    assert "Cell line" in client.get(f"/api/experiments/{exp['id']}/export", headers=a).text
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=a, json={"password": PW}).status_code == 200
    v = client.get(f"/api/experiments/{exp['id']}/verify", headers=a)
    assert v.status_code == 200 and v.json().get("ok", v.json().get("valid", True)) is not False, v.text
