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
