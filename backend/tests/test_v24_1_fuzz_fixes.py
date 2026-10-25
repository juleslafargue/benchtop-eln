"""0.24.1: what fuzzing the API found (tests/fuzz_api.py sends thousands of odd but well-formed requests).

Each of these answered "Internal Server Error" instead of a plain message:
- an edit sending an empty value (null) for something that can't be empty: an experiment's title or description, a
  notebook's name or "archived", a group's description, a material's name, a lot's location, a person's "active".
  Now a text field or list with a default is cleared (null description → ""), and anything else is refused (422).
- a new experiment from a template that doesn't exist (deleted while the "New from template" dialog was open);
  template 0 slipped through the first fix, read as "no template"; the same slip let an inventory use name experiment 0
- registering a product with a structure that can't be read
"""
from __future__ import annotations

import pytest

PW = "lab-bench-pw-2026"


def _exp(client, h, nb, **kw):
    r = client.post("/api/experiments", headers=h, json={"title": "Fuzz fixes", "notebook_id": nb["id"], **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_an_experiment_edit_with_null_clears_text_and_refuses_the_title(client, users, notebook):
    h = users["alice"]
    exp = _exp(client, h, notebook, description="Pd(PPh3)4, 80 °C", tags=["suzuki"])
    r = client.patch(f"/api/experiments/{exp['id']}", headers=h, json={"description": None, "tags": None})
    assert r.status_code == 200, r.text
    got = client.get(f"/api/experiments/{exp['id']}", headers=h).json()
    assert got["description"] == "" and got["tags"] == []
    r = client.patch(f"/api/experiments/{exp['id']}", headers=h, json={"title": None})
    assert r.status_code == 422 and r.json()["detail"] == "Title can't be empty"
    assert client.get(f"/api/experiments/{exp['id']}", headers=h).json()["title"] == "Fuzz fixes"


def test_notebook_group_material_lot_and_user_edits_with_null(client, admin, users, notebook):
    h = users["alice"]
    assert client.patch(f"/api/notebooks/{notebook['id']}", headers=h, json={"archived": None}).status_code == 422
    assert client.patch(f"/api/notebooks/{notebook['id']}", headers=h, json={"name": None}).status_code == 422

    g = client.post("/api/groups", headers=admin, json={"name": "Fuzz group", "description": "Flow chemistry"}).json()
    r = client.patch(f"/api/groups/{g['id']}", headers=admin, json={"description": None})
    assert r.status_code == 200 and r.json()["description"] == "", r.text

    m = client.post("/api/materials", headers=h, json={"name": "Fuzzamine", "kind": "reagent", "notes": "keep cold"}).json()
    r = client.patch(f"/api/materials/{m['id']}", headers=h, json={"notes": None})
    assert r.status_code == 200, r.text
    assert client.patch(f"/api/materials/{m['id']}", headers=h, json={"name": None}).status_code == 422
    lot = client.post(f"/api/materials/{m['id']}/lots", headers=h, json={"amount": 5, "unit": "g", "location": "Shelf A"}).json()
    r = client.patch(f"/api/lots/{lot['id']}", headers=h, json={"location": None})
    assert r.status_code == 200 and r.json()["location"] == "", r.text

    vic = next(u for u in client.get("/api/users", headers=admin).json() if u["username"] == "vic")
    r = client.patch(f"/api/users/{vic['id']}", headers=admin, json={"active": None})
    assert r.status_code == 422 and r.json()["detail"] == "Active can't be empty"
    assert next(u for u in client.get("/api/users", headers=admin).json() if u["username"] == "vic")["active"] is True


def test_a_template_that_doesnt_exist_is_a_plain_not_found_and_nothing_is_made(client, users, notebook):
    h = users["alice"]
    before = len(client.get("/api/experiments?limit=1000", headers=h).json())
    for missing in (987654, 0, -3):  # 0 slipped through the first fix: it was read as "no template"
        r = client.post("/api/experiments", headers=h, json={"title": "From a deleted template", "notebook_id": notebook["id"],
                                                             "template_id": missing})
        assert r.status_code == 404, (missing, r.text)
    assert len(client.get("/api/experiments?limit=1000", headers=h).json()) == before


def test_registering_a_product_with_an_unreadable_structure_says_so(client, users, notebook):
    h = users["alice"]
    exp = _exp(client, h, notebook)
    r = client.post("/api/materials/register-product", headers=h,
                    json={"experiment_id": exp["id"], "name": "Mystery product", "smiles": "C1CC(", "amount_mg": 12})
    assert r.status_code == 422 and "Structure" in r.json()["detail"], r.text


@pytest.mark.parametrize("path", ["/api/experiments/{e}", "/api/notebooks/{n}"])
def test_an_empty_edit_still_works(client, users, notebook, path):
    h = users["alice"]
    exp = _exp(client, h, notebook)
    r = client.patch(path.format(e=exp["id"], n=notebook["id"]), headers=h, json={})
    assert r.status_code == 200, r.text


def test_an_inventory_use_for_experiment_0_is_refused(client, users):
    """The same 0-is-missing slip as templates: experiment 0 skipped the check and broke the save."""
    h = users["alice"]
    m = client.post("/api/materials", headers=h, json={"name": "Zeroethane", "kind": "reagent"}).json()
    lot = client.post(f"/api/materials/{m['id']}/lots", headers=h, json={"amount": 5, "unit": "g"}).json()
    r = client.post(f"/api/lots/{lot['id']}/transactions", headers=h, json={"amount": 1, "unit": "g", "experiment_id": 0})
    assert r.status_code == 404, r.text
    assert client.get(f"/api/materials/{m['id']}", headers=h).json()["lots"][0]["amount"] == 5
