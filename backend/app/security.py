"""Password hashing (PBKDF2-SHA256) and stateless signed bearer tokens (HMAC-SHA256).

Standard library only, so there is nothing to compile on install.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from .config import settings
from .db import get_session
from .models import AuthFailure, AuthSession, Role, User, utcnow

# OWASP's 2023 recommendation for PBKDF2-HMAC-SHA256. Older hashes (390k) are upgraded the next time
# the person signs in, so nobody has to reset anything.
_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


SSO_ONLY = "sso-only$"  # accounts created by single sign-on start without a password


def has_password(stored: str) -> bool:
    return bool(stored) and not stored.startswith(SSO_ONLY)


_DUMMY = "pbkdf2_sha256$600000$00000000000000000000000000000000$" + "0" * 64


def burn_time(password: str) -> None:
    """Spend as long as a real check would, so 'no such user' can't be told apart by timing."""
    verify_password(password, _DUMMY)


def needs_rehash(stored: str) -> bool:
    try:
        algo, iters, _, _ = stored.split("$")
        return algo == "pbkdf2_sha256" and int(iters) < _ITERATIONS
    except ValueError:
        return False


def verify_password(password: str, stored: str) -> bool:
    if not has_password(stored):
        return False
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except ValueError:
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def create_token(user_id: int, hours: float | None = None, sid: str | None = None) -> str:
    payload = {"sub": user_id, "exp": int(time.time()) + int((hours or settings.token_ttl_hours) * 3600)}
    if sid:
        payload["sid"] = sid
    return _sign(payload)


def decode_payload(token: str) -> dict | None:
    try:
        body, sig = token.split(".")
        expected = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        payload = json.loads(_unb64(body))
        if payload["exp"] < time.time():
            return None
        int(payload["sub"])
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def decode_token(token: str) -> int | None:
    p = decode_payload(token)
    return int(p["sub"]) if p else None


# ------------------------------------------------------------------ sessions

def client_ip(request: Request | None) -> str:
    if request is None or request.client is None:
        return ""
    return request.client.host or ""


def start_session(session: Session, user: User, request: Request | None, hours: float | None = None, method: str = "password") -> str:
    """Record a new signed-in browser and return its token."""
    h = hours or settings.token_ttl_hours
    sid = secrets.token_urlsafe(24)
    ua = (request.headers.get("user-agent", "") if request is not None else "")[:300]
    session.add(AuthSession(id=sid, user_id=user.id, expires_at=utcnow() + timedelta(hours=h), method=method,
                            user_agent=ua, ip=client_ip(request)[:64]))
    return create_token(user.id, h, sid)


def end_sessions(session: Session, user_id: int, keep: str | None = None) -> int:
    """Sign a person out everywhere (except the session `keep`). Returns how many were ended."""
    n = 0
    now = utcnow()
    for s in session.exec(select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at == None)):  # noqa: E711
        if s.id != keep:
            s.revoked_at = now
            session.add(s)
            n += 1
    return n


def revoke_tokens(session: Session, user_id: int) -> int:
    """Revoke every API token of a person. With a new password, or when an admin signs them out everywhere or resets
    their password (0.36.4: a token made with a stolen password kept working, and never expires unless told to)."""
    from .models import ApiToken
    n = 0
    for t in session.exec(select(ApiToken).where(ApiToken.user_id == user_id, ApiToken.revoked_at == None)):  # noqa: E711
        t.revoked_at = utcnow()
        session.add(t)
        n += 1
    return n


# ------------------------------------------------------------------ throttling password guesses

FAIL_WINDOW = timedelta(minutes=15)
MAX_PER_ACCOUNT = 5     # wrong passwords for one account (or one person's signature) per window
MAX_PER_IP = 30         # wrong passwords from one address per window, across accounts


def _recent(session: Session, key: str) -> list[AuthFailure]:
    return list(session.exec(select(AuthFailure).where(AuthFailure.key == key, AuthFailure.at > utcnow() - FAIL_WINDOW)
                             .order_by(AuthFailure.at)))


def check_throttle(session: Session, *keys_limits: tuple[str, int]) -> None:
    """Refuse (429) while any key has had too many recent failures; says how long to wait."""
    for key, limit in keys_limits:
        fails = _recent(session, key)
        if len(fails) >= limit:
            wait = fails[-limit].at + FAIL_WINDOW - utcnow()
            mins = max(1, int(wait.total_seconds() // 60) + 1)
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"Too many wrong passwords. For safety, try again in {mins} minute{'s' if mins != 1 else ''}.")


def record_failure(session: Session, *keys: str) -> None:
    for k in keys:
        session.add(AuthFailure(key=k))
    # keep the table small
    old = session.exec(select(AuthFailure).where(AuthFailure.at < utcnow() - timedelta(days=1))).all()
    for f in old[:500]:
        session.delete(f)


def reserve_attempt(*keys_limits: tuple[str, int]) -> list[int]:
    """Count a password attempt as a wrong one before checking it, as one step with the throttle check, and return
    what to take back if it turns out right (release_attempt). 0.36.4: the check, the slow password comparison and
    noting a failure were separate steps, so 25 guesses sent at the same moment were all checked, not 5 and then
    "too many". Done in its own short transaction, so it counts at once whatever the request does next."""
    from . import audit
    from .db import engine
    with Session(engine) as s:
        audit._take_write_lock(s)
        check_throttle(s, *keys_limits)  # 429, and nothing written
        rows = [AuthFailure(key=k) for k, _ in keys_limits]
        s.add_all(rows)
        for f in s.exec(select(AuthFailure).where(AuthFailure.at < utcnow() - timedelta(days=1)).limit(500)).all():
            s.delete(f)  # keep the table small
        s.commit()
        return [r.id for r in rows]


def release_attempt(session: Session, ids: list[int]) -> None:
    """The password was right after all: it isn't a wrong guess."""
    for i in ids:
        f = session.get(AuthFailure, i)
        if f is not None:
            session.delete(f)


def clear_failures(session: Session, key: str) -> None:
    for f in session.exec(select(AuthFailure).where(AuthFailure.key == key)):
        session.delete(f)


_bearer = HTTPBearer(auto_error=False)


# what someone whose password was reset by an admin may do before choosing a new one
_RESET_ALLOWED = {"/api/auth/me", "/api/auth/password", "/api/auth/logout", "/api/auth/password-check"}


def current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    if creds.credentials.startswith(TOKEN_PREFIX):
        return _token_user(request, creds.credentials, session)
    p = decode_payload(creds.credentials)
    sid = p.get("sid") if p else None
    sess = session.get(AuthSession, sid) if sid else None
    now = utcnow()
    if not p or sess is None or sess.revoked_at is not None or sess.expires_at < now or sess.user_id != int(p["sub"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has ended; please sign in again")
    user = session.get(User, sess.user_id)
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if now - sess.last_seen_at > timedelta(minutes=5):
        sess.last_seen_at = now
        session.add(sess)
        session.commit()
        session.refresh(user)
    if user.must_change_password and request.url.path not in _RESET_ALLOWED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Choose a new password first: an administrator reset yours")
    object.__setattr__(user, "_sid", sess.id)
    return user


# ---------------------------------------------------------------- personal access tokens
TOKEN_PREFIX = "bt_"
# What a script may do with a token (0.36.4: a list of what's allowed, not of what isn't; an admin's token could install
# plugins, change groups and scan instrument folders, because those weren't on the old list). Tokens work with records,
# inventory, the Inbox and documents, as you would in the app. Never: signing, witnessing, returning, withdrawing or
# voiding records; publishing on Zenodo; tokens, passwords, sessions, single sign-on or other accounts; anything an
# admin does (users, groups, instrument folders, plugins, webhooks, settings, backups, the lab's audit trail). Those
# always need a person signed in. docs/API.md says the same; tests/test_v36_4_tokens.py tries every endpoint.
_ID = r"[^/]+"
_TOKEN_ALLOWED: list[tuple[str, re.Pattern[str]]] = [(m, re.compile(p)) for m, p in [
    ("GET", r"/api/auth/me"),
    # records
    ("GET POST", r"/api/experiments"),
    ("GET PATCH", rf"/api/experiments/{_ID}"),
    ("POST", rf"/api/experiments/{_ID}/(attachments|clone|comments|entries|follow-up|log|reorder|samples)"),
    ("GET", rf"/api/experiments/{_ID}/(audit|comments|export|export\.eln|mentioned-in|safety|shares|verify|witness-candidates|writeup)"),
    ("PUT DELETE", rf"/api/experiments/{_ID}/shares/{_ID}"),
    ("PUT", rf"/api/experiments/{_ID}/witness-request"),
    ("POST", rf"/api/experiments/{_ID}/witness-request/remind"),
    ("PUT DELETE", rf"/api/entries/{_ID}"),
    ("POST", rf"/api/entries/{_ID}/restore"),
    ("GET", rf"/api/entries/{_ID}/versions"),
    ("GET", rf"/api/attachments/{_ID}(/preview)?"),
    ("POST", rf"/api/attachments/{_ID}/not-mine"),
    ("PATCH DELETE", rf"/api/comments/{_ID}"),
    ("GET POST", r"/api/notebooks"),
    ("PATCH DELETE", rf"/api/notebooks/{_ID}"),
    ("GET", rf"/api/notebooks/{_ID}/export\.eln"),
    ("GET", r"/api/export\.eln"),
    ("POST", r"/api/eln/preview"),
    ("POST DELETE", rf"/api/eln/import/{_ID}"),
    ("GET", rf"/api/signatures/{_ID}/timestamp"),
    ("DELETE", rf"/api/samples/{_ID}"),
    # inventory
    ("GET POST", r"/api/materials"),
    ("POST", r"/api/materials/register-product"),
    ("GET PATCH", rf"/api/materials/{_ID}"),
    ("POST", rf"/api/materials/{_ID}/(lots|safety/refresh|safety/acknowledge)"),
    ("GET", r"/api/lots"),
    ("PATCH", rf"/api/lots/{_ID}"),
    ("GET POST", rf"/api/lots/{_ID}/transactions"),
    ("POST", rf"/api/lots/transactions/{_ID}/reverse"),
    # the Inbox
    ("GET", r"/api/inbox(/count|/targets)?"),
    ("GET", rf"/api/inbox/{_ID}/files/{_ID}"),
    ("POST", rf"/api/inbox/{_ID}/(assign|follow-up|ignore|unignore)"),
    # documents: supporting information, the yearly archive
    ("GET POST", r"/api/si-documents"),
    ("GET PUT DELETE", rf"/api/si-documents/{_ID}"),
    ("GET", r"/api/archive/(options|pdfa|preview)"),
    ("POST", r"/api/archive/jobs"),
    ("GET DELETE", rf"/api/archive/jobs/{_ID}"),
    ("POST", rf"/api/archive/jobs/{_ID}/build"),
    ("GET", rf"/api/archive/jobs/{_ID}/download"),
    ("PUT", rf"/api/archive/jobs/{_ID}/spectra/{_ID}"),
    ("POST", rf"/api/archive/jobs/{_ID}/spectra/{_ID}/skip"),
    # looking things up
    ("GET", r"/api/(scan|compounds|reactions|search|spectra|frequent-materials|dashboard|doi|lookup|samples/lookup|notifications"
            r"|safety/status|nmr/predict|nmr/status|plugins/sections|plugins/readers|groups|health|demo)"),
    ("GET", rf"/api/lookup/{_ID}"),
    ("POST", r"/api/(reactions/similar|notifications/read)"),
    ("GET", r"/api/chem/(depict|hrms|structure-check)"),
    ("POST", r"/api/chem/(properties|reaction/parse|stoichiometry)"),
]]


def token_may(method: str, path: str) -> bool:
    """May an API token make this request? HEAD and OPTIONS count as GET."""
    m = "GET" if method in ("HEAD", "OPTIONS") else method
    return any(m in methods.split() and rx.fullmatch(path) for methods, rx in _TOKEN_ALLOWED)


def hash_api_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_api_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _token_user(request: Request, raw: str, session: Session) -> User:
    from .models import ApiToken
    tok = session.exec(select(ApiToken).where(ApiToken.token_hash == hash_api_token(raw))).first()
    now = utcnow()
    if tok is None or tok.revoked_at is not None or (tok.expires_at and tok.expires_at < now):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This API token is invalid, expired or revoked")
    user = session.get(User, tok.user_id)
    if user is None or not user.active or user.must_change_password:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This API token's account can't be used right now")
    path, method = request.scope.get("path") or request.url.path, request.method.upper()
    if not token_may(method, path):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API tokens can't do this; sign in to Benchtop to do it yourself")
    if tok.scope != "write" and method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This API token is read-only")
    if tok.last_used_at is None or now - tok.last_used_at > timedelta(minutes=1):
        tok.last_used_at, tok.last_used_ip = now, client_ip(request)
        session.add(tok)
        session.commit()
        session.refresh(user)
    object.__setattr__(user, "_sid", None)
    object.__setattr__(user, "_token", tok.id)
    return user


def writer(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role == Role.viewer:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only account")
    return user


def admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
Writer = Annotated[User, Depends(writer)]
Admin = Annotated[User, Depends(admin)]
DB = Annotated[Session, Depends(get_session)]
