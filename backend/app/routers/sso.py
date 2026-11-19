"""Single sign-on with OpenID Connect (authorization code flow with PKCE).

Works with any standard provider: Microsoft Entra ID (Microsoft 365 / university accounts),
Google Workspace, Okta, Auth0, Keycloak, Authentik… Configure with BENCHTOP_OIDC_ISSUER,
BENCHTOP_OIDC_CLIENT_ID and BENCHTOP_OIDC_CLIENT_SECRET; register the redirect URI
<public URL>/api/auth/sso/callback with the provider.

The flow is stateless on our side: state, nonce and the PKCE verifier travel in a short-lived,
HMAC-signed cookie. Tokens come straight from the provider's token endpoint over TLS, in exchange for
the client secret, so the ID token's signature isn't checked (OIDC Core §3.1.3.7 allows this); its
issuer, audience, expiry and nonce are (0.36.4). The user's details are read from the userinfo
endpoint when there is one, and used only if it describes the same person (the same `sub`).

Who is who (0.36.4): a sign-in is linked to the Benchtop account it was linked to before. A first
sign-in is linked to an existing account only by an email address the provider says it has verified
(`email_verified` true), never by `preferred_username` or `upn`, and never to an admin's account:
an admin, or anyone whose provider doesn't vouch for their email (Microsoft Entra ID doesn't send
`email_verified`), links their account themselves under Account › Sign-in, after typing their password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import select

from .. import __version__, audit
from ..config import settings
from ..models import Role, User, UserIdentity, utcnow
from ..security import DB, SSO_ONLY, CurrentUser, start_session

router = APIRouter(prefix="/api/auth/sso", tags=["single sign-on"])
COOKIE = "benchtop_sso"
_discovery: dict[str, Any] = {}


def enabled() -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)


def provider_name() -> str:
    if settings.oidc_name:
        return settings.oidc_name
    iss = settings.oidc_issuer.lower()
    for key, name in (("microsoftonline", "Microsoft"), ("login.microsoft", "Microsoft"), ("accounts.google", "Google"),
                      ("okta", "Okta"), ("auth0", "Auth0")):
        if key in iss:
            return name
    return "single sign-on"


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0, headers={"User-Agent": f"Benchtop-ELN/{__version__}"}, follow_redirects=True)


def discovery() -> dict[str, Any]:
    cached = _discovery.get(settings.oidc_issuer)
    if cached and cached["_at"] > time.time() - 3600:
        return cached
    try:
        with _client() as c:
            r = c.get(f"{settings.oidc_issuer}/.well-known/openid-configuration")
            r.raise_for_status()
            d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"Couldn't reach the sign-in provider ({e.__class__.__name__}); try again, or sign in with a password") from e
    if not isinstance(d, dict):
        raise HTTPException(502, UNREADABLE)
    for k in ("authorization_endpoint", "token_endpoint"):
        if not d.get(k):
            raise HTTPException(502, f"The sign-in provider's configuration has no {k}")
    d["_at"] = time.time()
    _discovery[settings.oidc_issuer] = d
    return d


def _sign(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(settings.secret_key.encode(), f"sso.{body}".encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _unsign(value: str) -> dict[str, Any] | None:
    try:
        body, mac = value.rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(settings.secret_key.encode(), f"sso.{body}".encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if data.get("exp", 0) > time.time() else None
    except (ValueError, json.JSONDecodeError):
        return None


def _base(request: Request) -> str:
    return settings.public_url or str(request.base_url).rstrip("/")


UNREADABLE = ("The sign-in service gave an answer Benchtop couldn't read, so you weren't signed in. Try again in a "
              "moment; if it keeps happening, sign in with your password and tell your admin.")
MISMATCH = "The sign-in response didn't match this request. Please try again."
CLOCK_LEEWAY = 120  # seconds: the provider's clock and this computer's may differ a little


def _claims_from_id_token(tok: Any) -> dict[str, Any] | None:
    """The ID token's claims, or None if it isn't one (not text, not three parts, not a JSON object)."""
    if not isinstance(tok, str) or tok.count(".") != 2:
        return None
    try:
        part = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _json_object(r: httpx.Response) -> dict[str, Any] | None:
    """A reply's JSON object, or None (not JSON, broken JSON, or a list)."""
    try:
        d = r.json()
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _same_issuer(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and bool(a) and a.rstrip("/") == b.rstrip("/")


def _token_problem(claims: dict[str, Any], d: dict[str, Any], nonce: str) -> str | None:
    """Why this ID token isn't one for this sign-in, or None (OIDC Core §3.1.3.7: issuer, audience, expiry, nonce)."""
    iss = claims.get("iss")
    if not (_same_issuer(iss, settings.oidc_issuer) or _same_issuer(iss, d.get("issuer"))):
        return MISMATCH
    aud = claims.get("aud")
    auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if settings.oidc_client_id not in auds:
        return MISMATCH
    if len(auds) > 1 and claims.get("azp") not in (None, settings.oidc_client_id):
        return MISMATCH
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp + CLOCK_LEEWAY < time.time():
        return "The sign-in took too long or came from another window. Please try again."
    got = claims.get("nonce")
    if not isinstance(got, str) or not hmac.compare_digest(got, nonce):
        return MISMATCH
    return None


def _verified(v: Any) -> bool:
    """Has the provider checked this email address? Only a clear yes counts (some providers send the text "true")."""
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def _fail(request: Request, msg: str, page: str = "/") -> RedirectResponse:
    r = RedirectResponse(f"{_base(request)}{page}#{urlencode({'sso_error': msg})}", status_code=302)
    r.delete_cookie(COOKIE, path="/api/auth/sso")
    return r


@router.get("")
def sso_info():
    return {"enabled": enabled(), "name": provider_name() if enabled() else None, "password_login": settings.password_login}


@router.get("/start")
def start(request: Request, remember: bool = True, link: str = ""):
    if not enabled():
        raise HTTPException(404, "Single sign-on isn't set up on this Benchtop")
    linking = None
    if link:  # Account › Sign-in › Link: the ticket names the signed-in person who asked (see link_start)
        t = _unsign(link)
        if not t or not isinstance(t.get("link"), int):
            return _fail(request, "That link has expired. Open Account › Sign-in and choose Link again.")
        linking = t["link"]
    d = discovery()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    params = {"response_type": "code", "client_id": settings.oidc_client_id, "redirect_uri": f"{_base(request)}/api/auth/sso/callback",
              "scope": "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    r = RedirectResponse(f"{d['authorization_endpoint']}?{urlencode(params)}", status_code=302)
    r.set_cookie(COOKIE, _sign({"s": state, "n": nonce, "v": verifier, "r": remember, "l": linking, "exp": time.time() + 600}),
                 max_age=600, httponly=True, samesite="lax", secure=_base(request).startswith("https://"), path="/api/auth/sso")
    return r


def _username_for(session, email: str, name: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", (email.split("@")[0] if email else name.replace(" ", ".")).lower())[:28] or "user"
    if not base[0].isalnum():
        base = "u" + base
    cand, n = base, 1
    while session.exec(select(User).where(User.username == cand)).first():
        n += 1
        cand = f"{base}{n}"
    return cand


class LinkIn(BaseModel):
    password: str = ""


@router.post("/link")
def link_start(body: LinkIn, user: CurrentUser, session: DB):
    """Link your single sign-on account to this Benchtop account (Account › Sign-in): your password first, then the
    browser goes to the sign-in provider and back. The only way an admin's account is linked (0.36.4)."""
    from .auth import _confirm_password
    if not enabled():
        raise HTTPException(404, "Single sign-on isn't set up on this Benchtop")
    from ..security import has_password
    if not has_password(user.password_hash):
        raise HTTPException(409, "You already sign in with single sign-on")
    _confirm_password(session, user, body.password)
    ticket = _sign({"link": user.id, "exp": time.time() + 300})
    return {"url": f"/api/auth/sso/start?{urlencode({'link': ticket})}"}


@router.get("/linked")
def linked(user: CurrentUser, session: DB):
    """The single sign-on accounts linked to yours."""
    rows = session.exec(select(UserIdentity).where(UserIdentity.user_id == user.id).order_by(UserIdentity.id)).all()
    return [{"email": r.email, "created_at": r.created_at.isoformat() + "Z",
             "last_login_at": r.last_login_at.isoformat() + "Z" if r.last_login_at else None} for r in rows]


@router.get("/callback")
def callback(request: Request, session: DB, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return _fail(request, error_description or error)
    saved = _unsign(request.cookies.get(COOKIE, ""))

    def fail(msg: str) -> RedirectResponse:  # back to Account when linking from there
        return _fail(request, msg, "/account" if saved and saved.get("l") is not None else "/")
    if not saved or not state or not isinstance(saved.get("s"), str) or not isinstance(saved.get("n"), str) \
            or not hmac.compare_digest(saved["s"], state):
        return fail("The sign-in took too long or came from another window. Please try again.")
    try:
        d = discovery()
    except HTTPException as e:
        return fail(str(e.detail))
    try:
        with _client() as c:
            tr = c.post(d["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": f"{_base(request)}/api/auth/sso/callback",
                "client_id": settings.oidc_client_id, "client_secret": settings.oidc_client_secret, "code_verifier": saved["v"]})
            tok = _json_object(tr)
            if tr.status_code != 200:
                said = (tok or {}).get("error_description") or (tok or {}).get("error")
                return fail(f"The sign-in provider refused the login ({str(said)[:200] if said else tr.status_code})")
            if tok is None:
                return fail(UNREADABLE)
            claims = _claims_from_id_token(tok.get("id_token"))
            if claims is None:
                return fail(UNREADABLE)
            problem = _token_problem(claims, d, saved["n"])
            if problem:
                return fail(problem)
            sub = claims.get("sub")
            if not isinstance(sub, str) or not sub:
                return fail("The sign-in provider didn't say who you are")
            if d.get("userinfo_endpoint") and isinstance(tok.get("access_token"), str):
                ur = c.get(d["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok['access_token']}"})
                if ur.status_code == 200:
                    info = _json_object(ur)
                    if info is None:
                        return fail(UNREADABLE)
                    if info.get("sub") != sub:  # about someone else: never used (OIDC Core §5.3.2)
                        return fail(MISMATCH)
                    if "email" in info and info["email"] != claims.get("email"):
                        # another address than the ID token's: only what userinfo says about it counts as verified
                        claims = {k: v for k, v in claims.items() if k != "email_verified"}
                    claims = {**claims, **{k: v for k, v in info.items() if k not in ("iss", "aud", "exp", "nonce", "sub")}}
    except httpx.HTTPError as e:
        return fail(f"Couldn't reach the sign-in provider ({e.__class__.__name__})")

    def text(v: Any) -> str:
        return v.strip() if isinstance(v, str) else ""
    email = text(claims.get("email")).lower()
    # BENCHTOP_OIDC_TRUST_EMAIL=on: your organisation sets everyone's address (Microsoft Entra ID, which never sends
    # email_verified, with a single-tenant app registration)
    verified = bool(email) and (_verified(claims.get("email_verified")) or
                                (settings.oidc_trust_email and "email_verified" not in claims))
    # what the domain check reads; preferred_username and upn only when there's no email (Microsoft Entra ID)
    address = email or (text(claims.get("preferred_username")) or text(claims.get("upn"))).lower()
    name = (text(claims.get("name")) or " ".join(x for x in (text(claims.get("given_name")), text(claims.get("family_name"))) if x))[:200]
    if claims.get("email_verified") is False or (isinstance(claims.get("email_verified"), str) and not verified):
        return fail("Your email address isn't verified with the sign-in provider")
    if settings.oidc_domains and address.split("@")[-1] not in settings.oidc_domains:
        audit.record(session, None, "login_failed", "user", None, username=address, method="sso", reason="domain not allowed")
        session.commit()
        return fail(f"Accounts from {address.split('@')[-1] or 'that provider'} can't sign in here")

    issuer = str(claims["iss"])
    audit._take_write_lock(session)  # the same person signing in twice at once gets one account, linked once
    ident = session.exec(select(UserIdentity).where(UserIdentity.issuer == issuer, UserIdentity.subject == sub)).first()
    if ident is None:  # linked when the provider wrote its issuer with (or without) a final /
        other = issuer[:-1] if issuer.endswith("/") else issuer + "/"
        ident = session.exec(select(UserIdentity).where(UserIdentity.issuer == other, UserIdentity.subject == sub)).first()
    user = session.get(User, ident.user_id) if ident else None

    if saved.get("l") is not None:  # linking from Account › Sign-in: this sign-in becomes theirs
        me = session.get(User, saved["l"])
        if me is None or not me.active:
            return fail("This account has been deactivated")
        if user is not None and user.id != me.id:
            return fail("That sign-in is already linked to another Benchtop account. Ask an admin.")
        if ident is None:
            session.add(UserIdentity(user_id=me.id, issuer=issuer, subject=sub, email=email, last_login_at=utcnow()))
            audit.record(session, me, "sso_linked", "user", me.id, issuer=issuer, email=email)
            session.commit()
        r = RedirectResponse(f"{_base(request)}/account#{urlencode({'sso_linked': '1'})}", status_code=302)
        r.delete_cookie(COOKIE, path="/api/auth/sso")
        return r

    created = False
    if user is None and email:  # first sign-in: an existing account with the same email address
        same = [u for u in session.exec(select(User)) if (u.email or "").strip().lower() == email]
        if same:
            if not verified or len(same) > 1 or same[0].role == Role.admin:
                why = ("isn't verified by the sign-in provider" if not verified else
                       "belongs to an admin" if len(same) == 1 else "is on more than one account")
                audit.record(session, None, "login_failed", "user", None, username=email, method="sso",
                             reason=f"email {why}: not linked automatically")
                session.commit()
                return fail(f"There's already a Benchtop account for {email}. Sign in with its password once, "
                                      "then link single sign-on under Account › Sign-in.")
            user = same[0]
    if user is None:
        if not settings.oidc_auto_create:
            audit.record(session, None, "login_failed", "user", None, username=address, method="sso", reason="no account")
            session.commit()
            return fail(f"There's no Benchtop account for {address or 'you'} yet. Ask an admin to add one with this email address.")
        role = Role.viewer if settings.oidc_default_role == "viewer" else Role.scientist
        # the email address is kept only if the provider vouches for it: it decides who is who at later sign-ins
        user = User(username=_username_for(session, email or address, name), full_name=name or address,
                    email=email if verified else "", role=role, password_hash=SSO_ONLY + secrets.token_hex(8))
        session.add(user)
        session.flush()
        created = True
        audit.record(session, None, "create", "user", user.id, username=user.username, role=role.value, method="sso", issuer=issuer)
    if not user.active:
        return fail("This account has been deactivated")
    if ident is None:
        ident = UserIdentity(user_id=user.id, issuer=issuer, subject=sub, email=email)
    ident.last_login_at = utcnow()
    session.add(ident)
    token = start_session(session, user, request, 24 * 30 if saved.get("r") else None, method="sso")
    audit.record(session, user, "login", "user", user.id, method="sso", new_account=created)
    session.commit()
    r = RedirectResponse(f"{_base(request)}/#{urlencode({'sso': token, **({'welcome': '1'} if created else {})})}", status_code=302)
    r.delete_cookie(COOKIE, path="/api/auth/sso")
    return r
