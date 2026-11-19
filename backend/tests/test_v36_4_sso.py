"""0.36.4: single sign-on (found by the 0.36.2 stress test; it only matters when SSO is on, but then it's critical).

- A first sign-in is linked to an existing account only by an email address the provider says it verified, from the
  same person's ID token or userinfo; never by preferred_username or upn; never to an admin's account. Before, a
  provider account with someone's email (and no email_verified, or "false" as text) signed in as them, admins
  included. Anyone can still link their account themselves: Account › Sign-in › Link, after typing their password.
- Odd replies from the provider (not JSON, broken JSON, a list) give a plain message, not "Internal Server Error".
- The ID token must be for this Benchtop: its issuer, audience, expiry and nonce are checked; userinfo about another
  person (another sub) is never used."""
from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

ISS = "https://idp.example.org"
PW = "lab-bench-pw-2026"


def _jwt(claims) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


@pytest.fixture()
def idp(monkeypatch):
    """A fake provider. `nxt` says what the next sign-in gets: the ID token's claims, userinfo, or raw replies."""
    from app.config import settings
    from app.routers import sso
    monkeypatch.setattr(settings, "oidc_issuer", ISS)
    monkeypatch.setattr(settings, "oidc_client_id", "benchtop")
    monkeypatch.setattr(settings, "oidc_client_secret", "s3cret")
    monkeypatch.setattr(settings, "oidc_domains", [])
    monkeypatch.setattr(settings, "oidc_auto_create", True)
    monkeypatch.setattr(settings, "oidc_trust_email", False, raising=False)
    sso._discovery.clear()
    nxt: dict = {}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISS, "authorization_endpoint": f"{ISS}/authorize",
                                             "token_endpoint": f"{ISS}/token", "userinfo_endpoint": f"{ISS}/userinfo"})
        if req.url.path == "/token":
            if "token_raw" in nxt:
                return httpx.Response(nxt.get("token_status", 200), content=nxt["token_raw"].encode(),
                                      headers={"content-type": nxt.get("token_ct", "application/json")})
            claims = {"iss": ISS, "aud": "benchtop", "exp": time.time() + 300, "nonce": nxt["nonce"], **nxt["claims"]}
            claims = {k: v for k, v in claims.items() if v is not None}
            return httpx.Response(200, json={"access_token": "at", "id_token": nxt.get("id_token") or _jwt(claims)})
        if req.url.path == "/userinfo":
            if "userinfo_raw" in nxt:
                return httpx.Response(200, content=nxt["userinfo_raw"].encode(), headers={"content-type": "application/json"})
            if nxt.get("userinfo") is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=nxt["userinfo"])
        return httpx.Response(404)
    real = httpx.Client
    monkeypatch.setattr(sso, "_client", lambda: real(transport=httpx.MockTransport(handler)))
    return nxt


def _login(client, idp, claims=None, start="/api/auth/sso/start", **kw):
    """Through the provider and back; (who is signed in as, or None; the message, if refused; the redirect)."""
    idp.clear()
    idp.update({"claims": claims or {}, "userinfo": None, **kw})
    r = client.get(start, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    idp["nonce"] = q["nonce"][0]
    cb = client.get("/api/auth/sso/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.status_code == 302, f"{cb.status_code} {cb.text[:200]}"
    loc = cb.headers["location"]
    frag = parse_qs(urlparse(loc).fragment)
    who = None
    if "sso" in frag:
        who = client.get("/api/auth/me", headers={"Authorization": f"Bearer {frag['sso'][0]}"}).json()["username"]
    return who, (frag.get("sso_error") or [""])[0], loc


@pytest.fixture(scope="module")
def accounts(client, admin):
    for u, role in (("sso_admin", "admin"), ("sso_sci", "scientist"), ("sso_pw", "scientist")):
        r = client.post("/api/users", headers=admin, json={"username": u, "password": PW, "must_change": False, "full_name": u,
                                                           "role": role, "email": f"{u.replace('_', '.')}@lab.example.org"})
        assert r.status_code == 201, r.text
    return {u: {"Authorization": "Bearer " + client.post("/api/auth/login", json={"username": u, "password": PW}).json()["token"]}
            for u in ("sso_admin", "sso_sci", "sso_pw")}


@pytest.mark.parametrize("case", [
    {"sub": "evil-1", "email": "sso.admin@lab.example.org"},  # no email_verified
    {"sub": "evil-2", "email": "sso.admin@lab.example.org", "email_verified": "false"},
    {"sub": "evil-3", "email": "SSO.Admin@lab.example.org", "email_verified": True},  # verified, but an admin's account
    {"sub": "evil-4", "preferred_username": "sso.admin@lab.example.org", "email_verified": True},
    {"sub": "evil-5", "upn": "sso.sci@lab.example.org"},
    {"sub": "evil-6", "email": "sso.sci@lab.example.org"},
    {"sub": "evil-7", "email": "sso.sci@lab.example.org", "email_verified": "no"},
], ids=["no-verified", "verified-false-text", "admin-verified", "preferred-username", "upn", "sci-unverified", "verified-no"])
def test_a_provider_account_with_someones_email_never_signs_in_as_them(client, idp, accounts, case):
    who, err, _ = _login(client, idp, case)
    assert who not in ("sso_admin", "sso_sci"), f"signed in as {who}"


def test_a_verified_email_links_an_ordinary_account_and_says_why_otherwise(client, idp, accounts):
    who, err, _ = _login(client, idp, {"sub": "sci-1", "email": "sso.sci@lab.example.org", "email_verified": True})
    assert who == "sso_sci", err
    assert _login(client, idp, {"sub": "sci-1"})[0] == "sso_sci"  # linked: no email needed next time
    who, err, _ = _login(client, idp, {"sub": "adm-1", "email": "sso.admin@lab.example.org", "email_verified": True})
    assert who is None and "already a Benchtop account" in err and "Account" in err
    who, err, _ = _login(client, idp, {"sub": "x-2", "email": "sso.pw@lab.example.org", "email_verified": "true"})  # text "true"
    assert who == "sso_pw", err


def test_userinfo_about_someone_else_is_never_used(client, idp, accounts):
    who, err, _ = _login(client, idp, {"sub": "u-1", "email": "new.one@lab.example.org", "email_verified": True},
                         userinfo={"sub": "someone-else", "email": "sso.sci@lab.example.org", "email_verified": True})
    assert who is None and "didn't match" in err
    # the same person's userinfo with another address: the ID token's "verified" doesn't vouch for it
    who, err, _ = _login(client, idp, {"sub": "u-2", "email": "mine@lab.example.org", "email_verified": True},
                         userinfo={"sub": "u-2", "email": "sso.pw@lab.example.org"})
    assert who != "sso_pw"


@pytest.mark.parametrize("claims,why", [
    ({"sub": "t-1", "iss": "https://evil.example"}, "didn't match"),
    ({"sub": "t-2", "aud": "another-app"}, "didn't match"),
    ({"sub": "t-3", "aud": ["another-app", "benchtop"], "azp": "another-app"}, "didn't match"),
    ({"sub": "t-4", "exp": 1000}, "took too long"),
    ({"sub": "t-5", "exp": None}, "took too long"),
    ({"sub": "t-6", "nonce": None}, "didn't match"),
    ({"sub": "t-7", "nonce": "someone-elses"}, "didn't match"),
    ({"sub": {"a": 1}}, "didn't say who"),
], ids=["iss", "aud", "azp", "expired", "no-exp", "no-nonce", "wrong-nonce", "sub-object"])
def test_an_id_token_for_another_sign_in_is_refused(client, idp, claims, why):
    who, err, _ = _login(client, idp, claims)
    assert who is None and why in err, (who, err)


def test_an_issuer_written_with_a_final_slash_and_a_list_audience_are_fine(client, idp):
    assert _login(client, idp, {"sub": "ok-1", "iss": ISS + "/", "aud": ["benchtop"], "email": "ok1@x.example"})[0]


@pytest.mark.parametrize("reply", [
    {"token_raw": "<html>oops</html>", "token_ct": "text/html"},
    {"token_raw": "{bad json", "token_status": 500},
    {"token_raw": "{bad json"},
    {"token_raw": "[1, 2]"},
    {"token_raw": json.dumps({"access_token": "a", "id_token": "eyJhbGciOiJub25lIn0.WzFd."})},  # payload a list
    {"token_raw": json.dumps({"access_token": "a", "id_token": 5})},
    {"token_raw": json.dumps({"access_token": "a"})},  # no ID token at all
    {"userinfo_raw": "<html>"},
    {"userinfo_raw": "[1]"},
], ids=["html", "500-broken", "200-broken", "list", "payload-list", "token-number", "no-id-token", "userinfo-html", "userinfo-list"])
def test_odd_replies_from_the_provider_give_a_plain_message(client, idp, reply):
    who, err, _ = _login(client, idp, {"sub": "odd-1", "email": "odd@x.example"}, **reply)
    assert who is None and err, "no message"


def test_linking_from_account_settings(client, idp, accounts, admin):
    h = accounts["sso_admin"]
    assert client.post("/api/auth/sso/link", headers=h, json={"password": "wrong"}).status_code == 400
    r = client.post("/api/auth/sso/link", headers=h, json={"password": PW})
    assert r.status_code == 200, r.text
    who, err, loc = _login(client, idp, {"sub": "adm-own", "email": "sso.admin@lab.example.org"}, start=r.json()["url"])
    assert who is None and not err and urlparse(loc).path == "/account" and "sso_linked=1" in loc
    assert _login(client, idp, {"sub": "adm-own"})[0] == "sso_admin"  # now it signs in as them
    assert [x["email"] for x in client.get("/api/auth/sso/linked", headers=h).json()] == ["sso.admin@lab.example.org"]
    # a sign-in already linked to someone else isn't moved
    r = client.post("/api/auth/sso/link", headers=accounts["sso_pw"], json={"password": PW})
    who, err, loc = _login(client, idp, {"sub": "adm-own"}, start=r.json()["url"])
    assert "already linked" in err and urlparse(loc).path == "/account"
    assert _login(client, idp, {"sub": "adm-own"})[0] == "sso_admin"
    # a made-up or expired ticket links nothing
    r = client.get("/api/auth/sso/start", params={"link": "forged.ticket"}, follow_redirects=False)
    assert "sso_error" in r.headers["location"]
    events = [e for e in client.get("/api/audit", headers=admin, params={"limit": 500}).json() if e["action"] == "sso_linked"]
    assert events and events[0]["username"] == "sso_admin"


def test_trusting_the_providers_addresses_is_a_choice(client, idp, accounts, monkeypatch):
    """BENCHTOP_OIDC_TRUST_EMAIL=on for a provider whose addresses the organisation sets (Microsoft Entra ID never
    sends email_verified); still never an admin's account."""
    from app.config import settings
    monkeypatch.setattr(settings, "oidc_trust_email", True)
    assert _login(client, idp, {"sub": "entra-1", "email": "sso.pw@lab.example.org"})[0] == "sso_pw"
    who, err, _ = _login(client, idp, {"sub": "entra-2", "email": "sso.admin@lab.example.org"})
    assert who is None and "already a Benchtop account" in err
    assert _login(client, idp, {"sub": "entra-3", "email": "sso.sci@lab.example.org", "email_verified": False})[0] is None
