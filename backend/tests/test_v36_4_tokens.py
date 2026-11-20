"""0.36.4: sign-in, API tokens and passwords (found by the 0.36.2 stress test).

- API tokens may do only what's on a list (records, inventory, the Inbox, documents, looking things up), as
  docs/API.md says. The old list of what they may *not* do missed some: an admin's write token could install plugins,
  create groups and change who is in them, look at instrument folders and update the NMR data. Every endpoint is tried.
- Changing your password, an admin's "Sign out everywhere" and an admin's password reset revoke the person's API
  tokens too: a never-expiring token made with a stolen password kept working.
- Wrong passwords sent all at once are throttled like ones sent one after the other: 25 at once were all checked,
  instead of 5 and then "too many". The same for signing passwords.
- On a lab server (Docker, behind Caddy) each person's own address counts for the per-address limit; everyone looked
  like Caddy, so 30 wrong passwords from anyone locked the whole lab out for a while."""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

PW = "lab-bench-pw-2026"
REPO = Path(__file__).resolve().parents[2]
CANT = "API tokens can't do this; sign in to Benchtop to do it yourself"

# every endpoint a token may use (with the method); everything else needs a person signed in
ALLOWED = {
    "POST /api/archive/jobs", "DELETE /api/archive/jobs/{job_id}", "GET /api/archive/jobs/{job_id}",
    "POST /api/archive/jobs/{job_id}/build", "GET /api/archive/jobs/{job_id}/download",
    "PUT /api/archive/jobs/{job_id}/spectra/{att_id}", "POST /api/archive/jobs/{job_id}/spectra/{att_id}/skip",
    "GET /api/archive/options", "GET /api/archive/pdfa", "GET /api/archive/preview",
    "GET /api/attachments/{att_id}", "POST /api/attachments/{att_id}/not-mine", "GET /api/attachments/{att_id}/preview",
    "GET /api/auth/me",
    "GET /api/chem/depict", "GET /api/chem/hrms", "POST /api/chem/properties", "POST /api/chem/reaction/parse",
    "POST /api/chem/stoichiometry", "GET /api/chem/structure-check",
    "DELETE /api/comments/{cid}", "PATCH /api/comments/{cid}", "GET /api/compounds", "GET /api/dashboard", "GET /api/demo",
    "GET /api/doi", "DELETE /api/eln/import/{token}", "POST /api/eln/import/{token}", "POST /api/eln/preview",
    "DELETE /api/entries/{entry_id}", "PUT /api/entries/{entry_id}", "POST /api/entries/{entry_id}/restore",
    "GET /api/entries/{entry_id}/versions",
    "GET /api/experiments", "POST /api/experiments", "GET /api/experiments/{exp_id}", "PATCH /api/experiments/{exp_id}",
    "POST /api/experiments/{exp_id}/attachments", "GET /api/experiments/{exp_id}/audit", "POST /api/experiments/{exp_id}/clone",
    "GET /api/experiments/{exp_id}/comments", "POST /api/experiments/{exp_id}/comments", "POST /api/experiments/{exp_id}/entries",
    "GET /api/experiments/{exp_id}/export", "GET /api/experiments/{exp_id}/export.eln", "POST /api/experiments/{exp_id}/follow-up",
    "POST /api/experiments/{exp_id}/log", "GET /api/experiments/{exp_id}/mentioned-in", "POST /api/experiments/{exp_id}/reorder",
    "GET /api/experiments/{exp_id}/safety", "POST /api/experiments/{exp_id}/samples", "GET /api/experiments/{exp_id}/shares",
    "DELETE /api/experiments/{exp_id}/shares/{uid}", "PUT /api/experiments/{exp_id}/shares/{uid}",
    "GET /api/experiments/{exp_id}/verify", "GET /api/experiments/{exp_id}/witness-candidates",
    "PUT /api/experiments/{exp_id}/witness-request", "POST /api/experiments/{exp_id}/witness-request/remind",
    "GET /api/experiments/{exp_id}/writeup", "GET /api/export.eln", "GET /api/frequent-materials", "GET /api/groups",
    "GET /api/health", "GET /api/inbox", "GET /api/inbox/count", "GET /api/inbox/targets", "POST /api/inbox/{row_id}/assign",
    "GET /api/inbox/{row_id}/files/{n}", "POST /api/inbox/{row_id}/follow-up", "POST /api/inbox/{row_id}/ignore",
    "POST /api/inbox/{row_id}/unignore", "GET /api/lookup", "GET /api/lookup/{code}", "GET /api/lots",
    "POST /api/lots/transactions/{tx_id}/reverse", "PATCH /api/lots/{lot_id}", "GET /api/lots/{lot_id}/transactions",
    "POST /api/lots/{lot_id}/transactions", "GET /api/materials", "POST /api/materials", "POST /api/materials/register-product",
    "GET /api/materials/{mat_id}", "PATCH /api/materials/{mat_id}", "POST /api/materials/{mat_id}/lots",
    "POST /api/materials/{mat_id}/safety/acknowledge", "POST /api/materials/{mat_id}/safety/refresh",
    "GET /api/nmr/predict", "GET /api/nmr/status", "GET /api/notebooks", "POST /api/notebooks", "DELETE /api/notebooks/{nb_id}",
    "PATCH /api/notebooks/{nb_id}", "GET /api/notebooks/{nb_id}/export.eln", "GET /api/notifications",
    "POST /api/notifications/read", "GET /api/plugins/readers", "GET /api/plugins/sections", "GET /api/reactions",
    "POST /api/reactions/similar", "GET /api/safety/status", "GET /api/samples/lookup", "DELETE /api/samples/{sid}",
    "GET /api/scan", "GET /api/search", "GET /api/si-documents", "POST /api/si-documents", "DELETE /api/si-documents/{doc_id}",
    "GET /api/si-documents/{doc_id}", "PUT /api/si-documents/{doc_id}", "GET /api/signatures/{sig_id}/timestamp",
    "GET /api/spectra",
}
NEVER = {  # in the docs' words: signing and the like, publishing, accounts and sign-in, and anything an admin does
    "POST /api/experiments/{exp_id}/sign", "POST /api/experiments/{exp_id}/witness", "POST /api/experiments/{exp_id}/reject",
    "POST /api/experiments/{exp_id}/withdraw", "POST /api/experiments/{exp_id}/void", "POST /api/experiments/{exp_id}/zenodo/publish",
    "POST /api/auth/password", "POST /api/auth/tokens", "POST /api/auth/sso/link", "PATCH /api/users/{uid}", "POST /api/users",
    "POST /api/plugins", "GET /api/plugins", "POST /api/plugins/{plugin_id}/enabled", "DELETE /api/plugins/{plugin_id}",
    "POST /api/groups", "PATCH /api/groups/{gid}", "DELETE /api/groups/{gid}", "PUT /api/groups/{gid}/members/{uid}",
    "DELETE /api/groups/{gid}/members/{uid}", "POST /api/instruments", "POST /api/instruments/scan", "GET /api/instruments",
    "POST /api/nmr/update", "POST /api/safety/refresh", "POST /api/webhooks", "POST /api/admin/backups", "GET /api/audit",
}


def _routes():
    from app.main import app
    return [(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops]


def _concrete(p: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", p)


def test_tokens_may_do_exactly_whats_on_the_list():
    from app.security import token_may
    allowed = {f"{m} {p}" for m, p in _routes() if token_may(m, _concrete(p))}
    assert allowed - ALLOWED == set(), "a token may now do these: add them to docs/API.md and ALLOWED, or don't allow them"
    assert ALLOWED - allowed == set(), "no longer allowed (or gone)"
    assert not (ALLOWED & NEVER)
    for m, p in (("HEAD", "/api/experiments"), ("OPTIONS", "/api/lots"), ("GET", "/api/experiments/1/")):
        assert token_may(m, p) == (m != "GET"), (m, p)  # HEAD/OPTIONS as GET; no trailing-slash way round the list
    for p in ("/api/plugins/sections/../../plugins", "/api/experiments/1/sign/", "/api/experiments/1/x/sign", "/api/groupsx"):
        assert not token_may("POST", p), p


@pytest.fixture(scope="module")
def admin_tokens(client, admin):
    out = {}
    for scope in ("read", "write"):
        r = client.post("/api/auth/tokens", headers=admin, json={"name": f"matrix {scope}", "scope": scope})
        assert r.status_code == 201, r.text
        out[scope] = {"Authorization": f"Bearer {r.json()['token']}"}
    return out


def test_an_admins_token_is_refused_everything_not_on_the_list(client, admin_tokens):
    """Every endpoint that needs someone signed in, and isn't on the list, refuses an admin's write token before doing
    anything (and a read token too)."""
    refused, wrong = 0, []
    for m, p in _routes():
        if f"{m} {p}" in ALLOWED:
            continue
        path = _concrete(p)
        if client.request(m, path, json={}).status_code != 401:
            continue  # open to anyone (signing in, setup, single sign-on): a token changes nothing there
        for scope in ("write", "read"):
            r = client.request(m, path, headers=admin_tokens[scope], json={})
            if r.status_code != 403 or (scope == "write" and r.json().get("detail") != CANT):
                wrong.append((scope, m, p, r.status_code, r.text[:80]))
        refused += 1
    assert not wrong, wrong
    assert refused > 60


def test_the_admin_things_from_the_stress_test(client, admin, admin_tokens):
    w = admin_tokens["write"]
    toml = b'[plugin]\nid = "token-plugin"\nname = "x"\napi = 1\n[[sections]]\nid = "s"\nname = "S"\n  [[sections.fields]]\n  id = "a"\n  type = "text"\n'
    assert client.post("/api/plugins", headers=w, files={"file": ("p.toml", toml)}).status_code == 403
    assert "token-plugin" not in [p["id"] for p in client.get("/api/plugins", headers=admin).json()["plugins"]]
    assert client.get("/api/plugins", headers=w).status_code == 403
    assert client.post("/api/groups", headers=w, json={"name": "Made by a token"}).status_code == 403
    assert "Made by a token" not in [g["name"] for g in client.get("/api/groups", headers=admin).json()]
    g = client.post("/api/groups", headers=admin, json={"name": "Token membership"}).json()
    me = client.get("/api/auth/me", headers=admin).json()
    assert client.put(f"/api/groups/{g['id']}/members/{me['id']}", headers=w, json={"role": "lead"}).status_code == 403
    assert client.post("/api/instruments/scan", headers=w).status_code == 403
    assert client.post("/api/nmr/update", headers=w).status_code == 403
    # what the docs list still works
    assert client.get("/api/groups", headers=w).status_code == 200
    assert client.get("/api/plugins/sections", headers=w).status_code == 200


# ------------------------------------------------------------------ revoked with the password


def _person(client, admin, username):
    client.post("/api/users", headers=admin, json={"username": username, "password": PW, "must_change": False,
                                                   "full_name": username.title(), "role": "scientist"})
    r = client.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _token(client, h):
    r = client.post("/api/auth/tokens", headers=h, json={"name": "instrument PC", "scope": "write"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_a_new_password_revokes_the_persons_tokens(client, admin):
    h = _person(client, admin, "tok_pw")
    t = _token(client, h)
    assert client.get("/api/experiments", headers=t).status_code == 200
    r = client.post("/api/auth/password", headers=h, json={"current_password": PW, "new_password": "another-bench-pw-2027"})
    assert r.status_code == 200, r.text
    assert client.get("/api/experiments", headers=t).status_code == 401, "the token made with the old password still works"
    assert r.json()["tokens_revoked"] == 1  # and the page says so
    assert client.get("/api/experiments", headers=h).status_code == 200  # this browser stays signed in
    assert client.get("/api/experiments", headers=_token(client, h)).status_code == 200  # new ones work


def test_sign_out_everywhere_and_a_password_reset_revoke_tokens_too(client, admin):
    h = _person(client, admin, "tok_lost")
    uid = client.get("/api/auth/me", headers=h).json()["id"]
    t1 = _token(client, h)
    r = client.post(f"/api/users/{uid}/sign-out", headers=admin)
    assert r.status_code == 200
    assert client.get("/api/experiments", headers=t1).status_code == 401, "signed out everywhere, but the token still works"
    assert r.json()["tokens_revoked"] == 1
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": "tok_lost", "password": PW}).json()["token"]}
    t2 = _token(client, h)
    assert client.patch(f"/api/users/{uid}", headers=admin, json={"password": "a-temporary-pw-2026x"}).status_code == 200
    h = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": "tok_lost", "password": "a-temporary-pw-2026x"}).json()["token"]}
    assert client.post("/api/auth/password", headers=h, json={"current_password": "", "new_password": "my-own-bench-pw-2026"}).status_code == 200
    assert client.get("/api/experiments", headers=t2).status_code == 401, "the token came back once the password was chosen"


# ------------------------------------------------------------------ many wrong passwords at once


def _all_at_once(n, fn):
    gate = threading.Barrier(n)
    out = [None] * n

    def run(i):
        gate.wait()
        out[i] = fn()
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    return out


@pytest.fixture()
def fresh_failures():
    """Wrong passwords noted by this test only (others' from the same test address are left as they were)."""
    from sqlalchemy import func
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import AuthFailure
    with Session(engine) as s:
        start = s.exec(select(func.max(AuthFailure.id))).one() or 0
        for f in s.exec(select(AuthFailure).where(AuthFailure.key == "ip:testclient")).all():
            s.delete(f)  # earlier tests' wrong passwords from this same address mustn't count here
        s.commit()
    yield
    with Session(engine) as s:
        for f in s.exec(select(AuthFailure).where(AuthFailure.id > start)).all():
            s.delete(f)
        s.commit()


def test_wrong_passwords_sent_at_once_are_throttled(client, admin, fresh_failures):
    _person(client, admin, "tok_guess")
    codes = _all_at_once(25, lambda: client.post("/api/auth/login", json={"username": "tok_guess", "password": "wrong"}).status_code)
    assert codes.count(401) == 5 and codes.count(429) == 20, sorted(codes)
    assert client.post("/api/auth/login", json={"username": "tok_guess", "password": PW}).status_code == 429


def test_wrong_signing_passwords_sent_at_once_are_throttled(client, admin, fresh_failures):
    h = _person(client, admin, "tok_signer")
    nb = client.post("/api/notebooks", headers=h, json={"name": "Signing guesses"}).json()
    exp = client.post("/api/experiments", headers=h, json={"title": "Guessed", "notebook_id": nb["id"]}).json()
    codes = _all_at_once(20, lambda: client.post(f"/api/experiments/{exp['id']}/sign", headers=h, json={"password": "wrong"}).status_code)
    assert codes.count(401) == 5 and codes.count(429) == 15, sorted(codes)
    assert client.get(f"/api/experiments/{exp['id']}", headers=h).json()["status"] == "open"


def test_a_right_password_still_works_and_isnt_counted(client, admin, fresh_failures):
    _person(client, admin, "tok_ok")
    for _ in range(4):
        assert client.post("/api/auth/login", json={"username": "tok_ok", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "tok_ok", "password": PW}).status_code == 200
    for _ in range(4):  # the right one cleared the account's count
        assert client.post("/api/auth/login", json={"username": "tok_ok", "password": "wrong"}).status_code == 401
    codes = _all_at_once(6, lambda: client.post("/api/auth/login", json={"username": "tok_ok", "password": PW}).status_code)
    assert codes == [200] * 6, codes  # right passwords at once are never counted as wrong ones


# ------------------------------------------------------------------ the lab server (Docker, behind Caddy)


def test_the_lab_server_sees_each_persons_own_address():
    import asyncio

    import yaml
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    assert "--proxy-headers" in (REPO / "Dockerfile").read_text()
    for name in ("docker-compose.server.yml", "docker-compose.server-postgres.yml"):
        app = yaml.safe_load((REPO / "deploy" / name).read_text())["services"]["app"]
        assert "ports" not in app, f"{name}: Benchtop must be reachable only through Caddy if it trusts forwarded addresses"
        trusted = app["environment"]["FORWARDED_ALLOW_IPS"]
        seen = {}

        async def inner(scope, receive, send):
            seen["client"] = scope["client"][0]

        mw = ProxyHeadersMiddleware(inner, trusted_hosts=trusted)
        for peer, xff, expect in (("172.18.0.3", "203.0.113.7", "203.0.113.7"),  # through Caddy: the person's address
                                  ("192.168.16.2", "198.51.100.4", "198.51.100.4"),
                                  ("203.0.113.9", "10.0.0.1", "203.0.113.9")):  # straight in (can't happen): not believed
            scope = {"type": "http", "client": (peer, 1234), "headers": [(b"x-forwarded-for", xff.encode())], "scheme": "http"}
            asyncio.run(mw(scope, None, None))
            assert seen["client"] == expect, (name, peer, xff, seen)
    plain = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["app"]
    assert "FORWARDED_ALLOW_IPS" not in (plain.get("environment") or {}), "Benchtop on its own port must not trust the header"
