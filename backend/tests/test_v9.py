"""v0.9: similar reactions, groups and permissions, comments and mentions, single sign-on."""
from tests.test_v2 import new_exp


def _reaction(client, h, nb, title, rows, cond=None):
    exp = new_exp(client, h, nb, title=title)
    rx = next(e for e in exp["entries"] if e["kind"] == "reaction")
    r = client.put(f"/api/entries/{rx['id']}", headers=h, json={"content": {"rows": rows, "conditions": cond or {}}, "version": rx["version"]})
    assert r.status_code == 200, r.text
    return exp


SUZUKI = [
    {"role": "reactant", "name": "4-bromoanisole", "smiles": "COc1ccc(Br)cc1", "basis": "mass", "mass_mg": 187, "limiting": True},
    {"role": "reactant", "name": "phenylboronic acid", "smiles": "OB(O)c1ccccc1", "basis": "equiv", "equiv": 1.2},
    {"role": "reagent", "name": "K2CO3", "smiles": "O=C([O-])[O-].[K+].[K+]", "basis": "mass", "mass_mg": 276},
    {"role": "catalyst", "name": "Pd(PPh3)4", "smiles": "[Pd]", "basis": "equiv", "equiv": 0.03},
    {"role": "solvent", "name": "dioxane", "smiles": "C1COCCO1", "basis": "volume", "volume_ml": 5},
    {"role": "product", "name": "4-methoxybiphenyl", "smiles": "COc1ccc(-c2ccccc2)cc1", "actual_mass_mg": 150},
]


def test_similar_reactions(client, users, notebook):
    h = users["alice"]
    done = _reaction(client, h, notebook, "Suzuki reference", SUZUKI, {"temperature_c": 90, "time_h": 16, "atmosphere": "N2"})
    _reaction(client, h, notebook, "An esterification", [
        {"role": "reactant", "smiles": "CC(=O)O", "limiting": True, "basis": "mass", "mass_mg": 60},
        {"role": "reactant", "smiles": "CCO", "basis": "equiv", "equiv": 3}, {"role": "product", "smiles": "CC(=O)OCC"}])
    analogue = [{"role": "reactant", "smiles": "Cc1ccc(Br)cc1", "limiting": True}, {"role": "reactant", "smiles": "OB(O)c1ccccc1"},
                {"role": "product", "smiles": "Cc1ccc(-c2ccccc2)cc1"}]
    r = client.post("/api/reactions/similar", headers=users["bob"], json={"rows": analogue}).json()
    hits = {x["title"]: x for x in r["results"]}
    assert "Suzuki reference" in hits and "An esterification" not in hits
    s = hits["Suzuki reference"]
    assert s["kind"] == "analogue" and s["yield_pct"] and s["temperature_c"] == 90
    # amounts typed as mass come back as the equivalents they worked out to, so they scale
    k2co3 = next(x for x in s["condition_rows"] if x["name"] == "K2CO3")
    assert k2co3["basis"] == "equiv" and abs(k2co3["equiv"] - 2.0) < 0.01 and "mass_mg" not in k2co3
    # the same reaction ranks as "same"; the experiment being edited is never its own match
    same = client.post("/api/reactions/similar", headers=h, json={"rows": SUZUKI, "experiment_id": done["id"]}).json()
    assert all(x["experiment_id"] != done["id"] for x in same["results"])
    other = client.post("/api/reactions/similar", headers=h, json={"rows": SUZUKI}).json()
    assert other["results"][0]["kind"] == "same" and other["results"][0]["score"] >= 0.95
    # nothing to compare yet / junk structures
    assert client.post("/api/reactions/similar", headers=h, json={"rows": [{"role": "reagent", "name": "NaH"}]}).json()["results"] == []
    assert client.post("/api/reactions/similar", headers=h, json={"rows": [{"role": "reactant", "smiles": "not-a-smiles"}]}).status_code == 200


# ---------------------------------------------------------------- groups and permissions

import pytest  # noqa: E402

from tests.conftest import _login  # noqa: E402


@pytest.fixture(scope="module")
def lab(client, admin, users):
    """Smith lab: alice leads, bob and dave are members; carol is in another lab; vic is a viewer."""
    for u in ("carol", "dave"):
        client.post("/api/users", headers=admin, json={"username": u, "password": "lab-bench-pw-2026", "must_change": False, "full_name": u.title()})
    h = {**users, **{u: _login(client, u, "lab-bench-pw-2026") for u in ("carol", "dave")}}
    ids = {u["username"]: u["id"] for u in client.get("/api/users", headers=admin).json()}
    g = client.post("/api/groups", headers=admin, json={"name": "Smith lab"}).json()
    assert client.post("/api/groups", headers=h["alice"], json={"name": "Nope"}).status_code == 403   # admins create groups
    client.put(f"/api/groups/{g['id']}/members/{ids['alice']}", headers=admin, json={"role": "lead"})
    for u in ("bob", "dave"):  # a lead can add members
        assert client.put(f"/api/groups/{g['id']}/members/{ids[u]}", headers=h["alice"], json={}).status_code == 200
    assert client.put(f"/api/groups/{g['id']}/members/{ids['carol']}", headers=h["bob"], json={}).status_code == 403
    return {"h": h, "ids": ids, "group": g}


def _exp_with_everything(client, h, nb_id, title):
    exp = _reaction(client, h, {"id": nb_id}, title, SUZUKI, {"temperature_c": 90})
    up = client.post(f"/api/experiments/{exp['id']}/attachments", headers=h, files={"file": ("secret_1H.jdx", b"##TITLE=x\n##END=", "text/plain")})
    assert up.status_code == 201, up.text
    return exp, up.json()["id"]


def test_group_notebook_is_invisible_outside_the_group(client, lab):
    h, g = lab["h"], lab["group"]
    nb = client.post("/api/notebooks", headers=h["bob"], json={"name": "Bob's group work", "group_id": g["id"], "visibility": "group"}).json()
    assert nb["visibility"] == "group" and nb["group"]["name"] == "Smith lab"
    exp, att = _exp_with_everything(client, h["bob"], nb["id"], "Confidential Suzuki")
    eid = exp["id"]
    entry = next(e for e in client.get(f"/api/experiments/{eid}", headers=h["bob"]).json()["entries"])
    for who in ("alice", "dave", "admin" if "admin" in h else "bob"):
        assert client.get(f"/api/experiments/{eid}", headers=h[who]).status_code == 200, who

    c = h["carol"]
    paths = [f"/api/experiments/{eid}", f"/api/experiments/{eid}/audit", f"/api/experiments/{eid}/verify",
             f"/api/experiments/{eid}/writeup", f"/api/experiments/{eid}/export?format=json", f"/api/experiments/{eid}/safety",
             f"/api/experiments/{eid}/comments", f"/api/experiments/{eid}/shares", f"/api/attachments/{att}",
             f"/api/entries/{entry['id']}/versions"]
    for p in paths:
        assert client.get(p, headers=c).status_code == 404, p
    assert client.put(f"/api/entries/{entry['id']}", headers=c, json={"content": entry["content"], "version": entry["version"]}).status_code in (403, 404)
    assert client.post(f"/api/experiments/{eid}/clone", headers=c).status_code == 404
    assert client.post(f"/api/experiments/{eid}/comments", headers=c, json={"body": "hi"}).status_code == 404
    # and it isn't listed or findable anywhere
    assert nb["id"] not in [n["id"] for n in client.get("/api/notebooks", headers=c).json()]
    assert eid not in [e["id"] for e in client.get("/api/experiments", headers=c).json()]
    assert not client.get("/api/search", headers=c, params={"q": "Confidential"}).json()["experiments"]
    assert not client.get("/api/search", headers=c, params={"q": "secret_1H"}).json()["files"]
    assert eid not in [e["id"] for e in client.get("/api/search", headers=c, params={"q": "COc1ccc(Br)cc1", "mode": "substructure"}).json()["experiments"]]
    assert eid not in [r["experiment_id"] for r in client.get("/api/reactions", headers=c).json()]
    assert eid not in [r["experiment_id"] for r in client.post("/api/reactions/similar", headers=c, json={"rows": SUZUKI}).json()["results"]]
    assert all(eid not in [x["id"] for x in r["experiments"]] for r in client.get("/api/compounds", headers=c).json())
    assert eid not in [s["experiment_id"] for s in client.get("/api/spectra", headers=c).json()]
    dash = client.get("/api/dashboard", headers=c).json()
    assert eid not in [s["experiment_id"] for s in dash["spectra"]] + [y["experiment_id"] for y in dash["stats"]["yields"]]
    assert eid not in [e["id"] for e in dash["lists"]["awaiting"]]
    # carol can't add experiments to it or move hers into it
    assert client.post("/api/experiments", headers=c, json={"title": "x", "notebook_id": nb["id"]}).status_code == 404
    # members see it everywhere
    assert client.get("/api/search", headers=h["dave"], params={"q": "Confidential"}).json()["experiments"]


def test_private_notebooks_leads_and_rules(client, lab):
    h, g = lab["h"], lab["group"]
    nb = client.post("/api/notebooks", headers=h["bob"], json={"name": "Bob's private", "group_id": g["id"], "visibility": "private"}).json()
    exp = new_exp(client, h["bob"], nb, title="Half-baked idea")
    assert client.get(f"/api/experiments/{exp['id']}", headers=h["alice"]).status_code == 200    # the group's lead
    assert client.get(f"/api/experiments/{exp['id']}", headers=h["dave"]).status_code == 404     # another member
    # only members can put notebooks in a group; visibility must be valid; group notebooks need a group
    assert client.post("/api/notebooks", headers=h["carol"], json={"name": "x", "group_id": g["id"]}).status_code == 403
    assert client.post("/api/notebooks", headers=h["bob"], json={"name": "x", "visibility": "secret"}).status_code == 422
    assert client.post("/api/notebooks", headers=h["bob"], json={"name": "x", "visibility": "group"}).status_code == 422
    # legacy notebooks (no visibility set) are visible to everyone, as before
    old = client.post("/api/notebooks", headers=h["bob"], json={"name": "Open notebook"}).json()
    assert old["visibility"] == "everyone"
    assert client.get(f"/api/experiments/{new_exp(client, h['bob'], old)['id']}", headers=h["carol"]).status_code == 200
    # the owner can change the audience later
    r = client.patch(f"/api/notebooks/{old['id']}", headers=h["bob"], json={"group_id": g["id"], "visibility": "group"}).json()
    assert r["visibility"] == "group"
    assert client.patch(f"/api/notebooks/{old['id']}", headers=h["dave"], json={"visibility": "everyone"}).status_code == 403


def test_sharing_one_experiment(client, lab):
    h, g, ids = lab["h"], lab["group"], lab["ids"]
    nb = client.post("/api/notebooks", headers=h["bob"], json={"name": "Shared bits", "group_id": g["id"], "visibility": "group"}).json()
    exp = new_exp(client, h["bob"], nb, title="Ask Carol about this")
    eid = exp["id"]
    c = h["carol"]
    assert client.get(f"/api/experiments/{eid}", headers=c).status_code == 404
    assert client.put(f"/api/experiments/{eid}/shares/{ids['carol']}", headers=h["dave"], json={}).status_code == 403  # members can't share
    r = client.put(f"/api/experiments/{eid}/shares/{ids['carol']}", headers=h["bob"], json={"permission": "view"})
    assert r.status_code == 200 and r.json()["shares"][0]["permission"] == "view"
    got = client.get(f"/api/experiments/{eid}", headers=c).json()
    assert got["can_edit"] is False and got["shared_with_me"] == "view"
    sec = got["entries"][0]
    assert client.put(f"/api/entries/{sec['id']}", headers=c, json={"title": "x", "content": sec["content"], "version": sec["version"]}).status_code == 403
    notes = client.get("/api/notifications", headers=c).json()
    assert notes["unread"] >= 1 and any(n["kind"] == "share" and n["experiment"]["id"] == eid for n in notes["items"])
    # edit access: can change sections, still can't sign or move it
    client.put(f"/api/experiments/{eid}/shares/{ids['carol']}", headers=h["bob"], json={"permission": "edit"})
    assert client.get(f"/api/experiments/{eid}", headers=c).json()["can_edit"] is True
    assert client.put(f"/api/entries/{sec['id']}", headers=c, json={"title": "Carol's notes", "content": sec["content"], "version": sec["version"]}).status_code == 200
    assert client.post(f"/api/experiments/{eid}/sign", headers=c, json={"password": "lab-bench-pw-2026"}).status_code == 403
    assert client.patch(f"/api/experiments/{eid}", headers=c, json={"notebook_id": 1}).status_code in (403, 404)
    # viewers can only be given view access
    assert client.put(f"/api/experiments/{eid}/shares/{ids['vic']}", headers=h["bob"], json={"permission": "edit"}).status_code == 422
    # unshare: gone again, and its notification no longer points at it
    client.delete(f"/api/experiments/{eid}/shares/{ids['carol']}", headers=h["bob"])
    assert client.get(f"/api/experiments/{eid}", headers=c).status_code == 404
    assert all((n["experiment"] or {}).get("id") != eid for n in client.get("/api/notifications", headers=c).json()["items"])


def test_deleting_a_group_never_widens_access(client, admin, lab):
    h, ids = lab["h"], lab["ids"]
    g = client.post("/api/groups", headers=admin, json={"name": "Short-lived"}).json()
    client.put(f"/api/groups/{g['id']}/members/{ids['bob']}", headers=admin, json={"role": "lead"})
    nb = client.post("/api/notebooks", headers=h["bob"], json={"name": "In a doomed group", "group_id": g["id"]}).json()
    assert client.delete(f"/api/groups/{g['id']}", headers=h["bob"]).status_code == 403
    assert client.delete(f"/api/groups/{g['id']}", headers=admin).json()["notebooks"] == [nb["id"]]
    after = next(n for n in client.get("/api/notebooks", headers=h["bob"]).json() if n["id"] == nb["id"])
    assert after["visibility"] == "private" and after["group"] is None
    assert nb["id"] not in [n["id"] for n in client.get("/api/notebooks", headers=h["carol"]).json()]


# ---------------------------------------------------------------- comments, mentions, notifications

def test_comments_mentions_and_notifications(client, lab):
    h, g, ids = lab["h"], lab["group"], lab["ids"]
    nb = client.post("/api/notebooks", headers=h["bob"], json={"name": "Discussed", "group_id": g["id"], "visibility": "group"}).json()
    exp = new_exp(client, h["bob"], nb, title="Needs a second opinion")
    eid = exp["id"]
    sec = exp["entries"][1]
    for who in ("bob", "alice", "dave", "carol"):
        client.post("/api/notifications/read", headers=h[who], json={"all": True})

    r = client.post(f"/api/experiments/{eid}/comments", headers=h["alice"],
                    json={"body": "Is the 13C peak at 170 real? @bob @carol @nobody", "entry_id": sec["id"]}).json()
    assert r["entry_id"] == sec["id"] and [u["username"] for u in r["unseen_mentions"]] == ["carol"]
    bob_n = client.get("/api/notifications?unread=true", headers=h["bob"]).json()
    assert bob_n["unread"] == 1 and bob_n["items"][0]["kind"] == "mention"          # mentioned AND owner: one notification
    assert client.get("/api/notifications?unread=true", headers=h["carol"]).json()["unread"] == 0  # can't see it, so not told

    reply = client.post(f"/api/experiments/{eid}/comments", headers=h["bob"], json={"body": "Yes, it's the ester C=O", "parent_id": r["id"]}).json()
    assert reply["parent_id"] == r["id"] and reply["entry_id"] == sec["id"]
    assert client.get("/api/notifications?unread=true", headers=h["alice"]).json()["items"][0]["kind"] == "reply"
    deep = client.post(f"/api/experiments/{eid}/comments", headers=h["dave"], json={"body": "+1", "parent_id": reply["id"]}).json()
    assert deep["parent_id"] == r["id"]                                               # one level of threading

    # only authors edit; owner resolves; resolving a reply makes no sense
    assert client.patch(f"/api/comments/{r['id']}", headers=h["bob"], json={"body": "changed"}).status_code == 403
    assert client.patch(f"/api/comments/{r['id']}", headers=h["alice"], json={"body": "Is the peak at 170.2 real? @bob"}).json()["edited_at"]
    assert client.patch(f"/api/comments/{reply['id']}", headers=h["bob"], json={"resolved": True}).status_code == 422
    assert client.patch(f"/api/comments/{r['id']}", headers=h["dave"], json={"resolved": True}).status_code == 403
    assert client.patch(f"/api/comments/{r['id']}", headers=h["bob"], json={"resolved": True}).json()["resolved_by"]["username"] == "bob"
    assert client.get(f"/api/experiments/{eid}", headers=h["bob"]).json()["open_comments"] == 0

    # comments work on signed records and don't change what was signed
    client.post(f"/api/experiments/{eid}/sign", headers=h["bob"], json={"password": "lab-bench-pw-2026"})
    before = client.get(f"/api/experiments/{eid}/verify", headers=h["bob"]).json()
    assert client.post(f"/api/experiments/{eid}/comments", headers=h["alice"], json={"body": "Looks good, witnessing"}).status_code == 201
    after = client.get(f"/api/experiments/{eid}/verify", headers=h["bob"]).json()
    assert before["intact"] and after["intact"] and before["content_hash"] == after["content_hash"]
    # deleting keeps a placeholder while replies exist; everything is in the audit trail
    assert client.delete(f"/api/comments/{r['id']}", headers=h["dave"]).status_code == 403
    client.delete(f"/api/comments/{r['id']}", headers=h["alice"])
    listed = {c["id"]: c for c in client.get(f"/api/experiments/{eid}/comments", headers=h["bob"]).json()}
    assert listed[r["id"]]["deleted"] and listed[r["id"]]["body"] == ""
    actions = [ev["action"] for ev in client.get(f"/api/experiments/{eid}/audit", headers=h["bob"]).json()]
    assert {"comment", "comment_edit", "comment_resolve", "comment_delete"} <= set(actions)
    # marking read
    assert client.post("/api/notifications/read", headers=h["alice"], json={"experiment_id": eid}).json()["marked"] >= 1
    assert client.get("/api/notifications?unread=true", headers=h["alice"]).json()["unread"] == 0
    # read-only accounts can comment on what they can see
    assert client.post(f"/api/experiments/{eid}/comments", headers=h["vic"], json={"body": "x"}).status_code == 404
    open_nb = client.post("/api/notebooks", headers=h["bob"], json={"name": "Public"}).json()
    pub = new_exp(client, h["bob"], open_nb, title="Public one")
    assert client.post(f"/api/experiments/{pub['id']}/comments", headers=h["vic"], json={"body": "Nice"}).status_code == 201


# ---------------------------------------------------------------- single sign-on (OpenID Connect)

import base64 as _b64  # noqa: E402
import json as _json  # noqa: E402
import time  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

import httpx  # noqa: E402


def _idtoken(claims):
    enc = lambda d: _b64.urlsafe_b64encode(_json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


@pytest.fixture()
def idp(monkeypatch):
    """A fake identity provider; `people[code]` decides who signs in with that code."""
    from app.config import settings
    from app.routers import sso
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.edu")
    monkeypatch.setattr(settings, "oidc_client_id", "benchtop")
    monkeypatch.setattr(settings, "oidc_client_secret", "s3cret")
    monkeypatch.setattr(settings, "oidc_domains", [])
    monkeypatch.setattr(settings, "oidc_auto_create", True)
    sso._discovery.clear()
    state = {"people": {}, "nonce": None, "token_calls": []}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": "https://idp.example.edu", "authorization_endpoint": "https://idp.example.edu/authorize",
                                             "token_endpoint": "https://idp.example.edu/token", "userinfo_endpoint": "https://idp.example.edu/userinfo"})
        if req.url.path == "/token":
            form = parse_qs(req.content.decode())
            state["token_calls"].append(form)
            who = state["people"].get(form["code"][0])
            if who is None:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad code"})
            # what a real provider puts in every ID token (checked since 0.36.4); a person's own claims can override them
            std = {"iss": "https://idp.example.edu", "aud": "benchtop", "exp": time.time() + 300, "nonce": state["nonce"]}
            return httpx.Response(200, json={"access_token": f"at-{form['code'][0]}", "id_token": _idtoken({**std, **who})})
        if req.url.path == "/userinfo":
            code = req.headers["authorization"].split("at-")[1]
            return httpx.Response(200, json=state["people"][code])
        return httpx.Response(404)
    real = httpx.Client
    monkeypatch.setattr(sso, "_client", lambda: real(transport=httpx.MockTransport(handler)))
    return state


def _sso_login(client, idp, code, person, remember=True):
    idp["people"][code] = person
    r = client.get("/api/auth/sso/start", params={"remember": remember}, follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["benchtop"] and q["redirect_uri"][0].endswith("/api/auth/sso/callback")
    idp["nonce"] = q["nonce"][0]
    cb = client.get("/api/auth/sso/callback", params={"code": code, "state": q["state"][0]}, follow_redirects=False)
    assert cb.status_code == 302
    frag = parse_qs(urlparse(cb.headers["location"]).fragment)
    return frag, idp["token_calls"][-1] if idp["token_calls"] else None


def test_sso_signs_in_creates_and_links_accounts(client, admin, idp):
    info = client.get("/api/auth/sso").json()
    assert info["enabled"] and info["name"] == "single sign-on"
    frag, call = _sso_login(client, idp, "c1", {"sub": "u-123", "email": "Pat.Lee@Example.edu", "name": "Pat Lee", "email_verified": True})
    assert "sso" in frag and frag.get("welcome") == ["1"]
    assert call["code_verifier"][0] and call["client_secret"] == ["s3cret"]              # PKCE + confidential client
    h = {"Authorization": f"Bearer {frag['sso'][0]}"}
    me = client.get("/api/auth/me", headers=h).json()
    assert me["username"] == "pat.lee" and me["full_name"] == "Pat Lee" and me["role"] == "scientist" and me["has_password"] is False
    # signing needs a password: SSO accounts are told to set one, then it works
    nb = client.post("/api/notebooks", headers=h, json={"name": "Pat's"}).json()
    exp = new_exp(client, h, nb)
    r = client.post(f"/api/experiments/{exp['id']}/sign", headers=h, json={"password": "anything"})
    assert r.status_code == 400 and "signing password" in r.json()["detail"]
    assert client.post("/api/auth/password", headers=h, json={"current_password": "", "new_password": "signing-pass-1"}).status_code == 200
    assert client.post(f"/api/experiments/{exp['id']}/sign", headers=h, json={"password": "signing-pass-1"}).status_code == 200
    # the same person again: same account, no "welcome"
    frag2, _ = _sso_login(client, idp, "c2", {"sub": "u-123", "email": "pat.lee@example.edu", "name": "Pat Lee"})
    assert "welcome" not in frag2
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {frag2['sso'][0]}"}).json()["id"] == me["id"]
    # an existing password account with the same email is linked rather than duplicated
    client.post("/api/users", headers=admin, json={"username": "quinn", "password": "lab-bench-pw-2026", "must_change": False, "email": "quinn@example.edu"})
    frag3, _ = _sso_login(client, idp, "c3", {"sub": "u-999", "email": "quinn@example.edu", "name": "Quinn", "email_verified": True})
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {frag3['sso'][0]}"}).json()["username"] == "quinn"


def test_sso_rejections(client, idp, monkeypatch):
    from app.config import settings
    # wrong domain
    monkeypatch.setattr(settings, "oidc_domains", ["utoronto.ca"])
    frag, _ = _sso_login(client, idp, "d1", {"sub": "x1", "email": "someone@gmail.com"})
    assert "sso" not in frag and "gmail.com" in frag["sso_error"][0]
    monkeypatch.setattr(settings, "oidc_domains", [])
    # no auto-create
    monkeypatch.setattr(settings, "oidc_auto_create", False)
    frag, _ = _sso_login(client, idp, "d2", {"sub": "x2", "email": "new.person@example.edu"})
    assert "no Benchtop account" in frag["sso_error"][0]
    monkeypatch.setattr(settings, "oidc_auto_create", True)
    # unverified email, provider error, forged state, replayed nonce
    frag, _ = _sso_login(client, idp, "d3", {"sub": "x3", "email": "a@example.edu", "email_verified": False})
    assert "verified" in frag["sso_error"][0]
    r = client.get("/api/auth/sso/callback", params={"error": "access_denied", "error_description": "User cancelled"}, follow_redirects=False)
    assert "User cancelled" in parse_qs(urlparse(r.headers["location"]).fragment)["sso_error"][0]
    client.get("/api/auth/sso/start", follow_redirects=False)
    r = client.get("/api/auth/sso/callback", params={"code": "d1", "state": "forged"}, follow_redirects=False)
    assert "try again" in parse_qs(urlparse(r.headers["location"]).fragment)["sso_error"][0]
    idp["people"]["d4"] = {"sub": "x4", "email": "b@example.edu"}
    s = client.get("/api/auth/sso/start", follow_redirects=False)
    q = parse_qs(urlparse(s.headers["location"]).query)
    idp["nonce"] = "someone-elses-nonce"
    r = client.get("/api/auth/sso/callback", params={"code": "d4", "state": q["state"][0]}, follow_redirects=False)
    assert "didn't match" in parse_qs(urlparse(r.headers["location"]).fragment)["sso_error"][0]


def test_sso_off_and_password_login_off(client, admin, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "oidc_issuer", "")
    assert client.get("/api/auth/sso").json()["enabled"] is False
    assert client.get("/api/auth/sso/start", follow_redirects=False).status_code == 404
    monkeypatch.setattr(settings, "password_login", False)
    assert client.post("/api/auth/login", json={"username": "bob", "password": "lab-bench-pw-2026"}).status_code == 403
    assert client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"}).status_code == 200  # break-glass
