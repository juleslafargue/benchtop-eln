"""0.35.1: a PI's dashboard starts with the group's work.

A PI who writes no records of their own (in the demo lab, the Ruiz group's lead) saw "Your first record" and three
empty lists of their own records above the group's yield trend. The dashboard now says whether you lead a group, so
the app leaves the first-record card out for a lead. A member with no records (a new student) still gets it."""
from __future__ import annotations

from tests.conftest import _login


def _user(client, admin, username):
    r = client.post("/api/users", headers=admin, json={"username": username, "password": "lab-bench-pw-2026", "must_change": False,
                                                       "full_name": username.title(), "role": "scientist"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"], _login(client, username, "lab-bench-pw-2026")


def test_the_dashboard_says_whether_you_lead_a_group(client, admin):
    pi_id, pi = _user(client, admin, "pi_v351")
    st_id, student = _user(client, admin, "student_v351")
    loner_id, loner = _user(client, admin, "loner_v351")
    g = client.post("/api/groups", headers=admin, json={"name": "PI group (v35.1)"}).json()
    assert client.put(f"/api/groups/{g['id']}/members/{pi_id}", headers=admin, json={"role": "lead"}).status_code == 200
    assert client.put(f"/api/groups/{g['id']}/members/{st_id}", headers=admin, json={"role": "member"}).status_code == 200

    lead = client.get("/api/dashboard", headers=pi).json()
    assert lead["leads_group"] is True
    assert lead["lists"]["recent"] == [], "the lead has written nothing: the case this release is about"
    assert client.get("/api/dashboard", headers=student).json()["leads_group"] is False
    assert client.get("/api/dashboard", headers=loner).json()["leads_group"] is False, "in no group at all"

    # made a plain member, the PI isn't a lead any more; a deleted group leads nobody
    client.put(f"/api/groups/{g['id']}/members/{pi_id}", headers=admin, json={"role": "member"})
    assert client.get("/api/dashboard", headers=pi).json()["leads_group"] is False
    client.put(f"/api/groups/{g['id']}/members/{pi_id}", headers=admin, json={"role": "lead"})
    assert client.get("/api/dashboard", headers=pi).json()["leads_group"] is True
    assert client.delete(f"/api/groups/{g['id']}", headers=admin).status_code == 200
    assert client.get("/api/dashboard", headers=pi).json()["leads_group"] is False


def test_the_lists_hidden_on_a_first_day_hold_only_your_own_records(client, admin):
    """The app hides open, waiting, recent, returned and stale for someone with no records yet. That's only safe if
    none of them can hold anyone else's: a lead who sees the group's records must still get them all empty."""
    pi_id, pi = _user(client, admin, "pi2_v351")
    st_id, student = _user(client, admin, "student2_v351")
    g = client.post("/api/groups", headers=admin, json={"name": "PI group 2 (v35.1)"}).json()
    client.put(f"/api/groups/{g['id']}/members/{pi_id}", headers=admin, json={"role": "lead"})
    client.put(f"/api/groups/{g['id']}/members/{st_id}", headers=admin, json={"role": "member"})
    nb = client.post("/api/notebooks", headers=student, json={"name": "Student NB (v35.1)", "group_id": g["id"], "visibility": "group"}).json()
    exp = client.post("/api/experiments", headers=student, json={"title": "Suzuki coupling", "notebook_id": nb["id"]}).json()
    assert client.get(f"/api/experiments/{exp['id']}", headers=pi).status_code == 200, "the lead should see the group's record"
    d = client.get("/api/dashboard", headers=pi).json()
    for k in ("open", "waiting", "recent", "returned", "stale"):
        assert d["lists"][k] == [], k
