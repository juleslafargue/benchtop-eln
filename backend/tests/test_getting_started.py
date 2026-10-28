"""The Getting started guide: the dashboard's "New to Benchtop?" card, once put away, stays away for that person
on every computer (a preference on their account), and only a real yes or no is kept."""


def _prefs(client, h):
    return client.get("/api/auth/me", headers=h).json()["preferences"]


def test_a_new_account_has_not_put_the_card_away(client, admin):
    client.post("/api/users", headers=admin, json={"username": "newcomer", "password": "lab-bench-pw-2026", "must_change": False,
                                                   "full_name": "New Comer", "role": "scientist"})
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": "newcomer", "password": "lab-bench-pw-2026"}).json()["token"]}
    assert "guide_dismissed" not in _prefs(client, h)


def test_putting_the_card_away_is_kept_per_person(client, users):
    alice, bob = users["alice"], users["bob"]
    client.patch("/api/auth/me", headers=alice, json={"preferences": {"theme": "dark"}})
    r = client.patch("/api/auth/me", headers=alice, json={"preferences": {"guide_dismissed": True}})
    assert r.status_code == 200
    assert r.json()["preferences"]["guide_dismissed"] is True
    assert _prefs(client, alice)["theme"] == "dark", "other preferences are left as they were"
    assert not _prefs(client, bob).get("guide_dismissed"), "someone else still sees the card"
    # a fresh sign-in (another computer) gets the same answer
    r = client.post("/api/auth/login", json={"username": "alice", "password": "lab-bench-pw-2026"})
    assert r.json()["user"]["preferences"]["guide_dismissed"] is True
    client.patch("/api/auth/me", headers=alice, json={"preferences": {"theme": "system", "guide_dismissed": False}})
    assert _prefs(client, alice)["guide_dismissed"] is False


def test_only_yes_or_no_is_kept(client, users):
    vic = users["vic"]  # a read-only account can put the card away too
    assert client.patch("/api/auth/me", headers=vic, json={"preferences": {"guide_dismissed": True}}).status_code == 200
    for junk in ("yes", 1, None, {"x": 1}, ["a"]):
        r = client.patch("/api/auth/me", headers=vic, json={"preferences": {"guide_dismissed": junk}})
        assert r.json()["preferences"]["guide_dismissed"] is True, f"{junk!r} replaced a real answer"
