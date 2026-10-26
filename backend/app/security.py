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
# Things a script never does with a token: sign, witness or void records; publish them on Zenodo; change passwords,
# sessions, tokens or other accounts; administration. Those always need a person signed in.
_TOKEN_BLOCKED = re.compile(r"^/api/(auth/|users|admin|webhooks|plugins/(?!sections|readers)|setup)|/(sign|witness|reject|withdraw|void)$"
                            r"|/zenodo(/[a-z]+)?$")  # publishing on Zenodo (0.28) is public and permanent: a person does it


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
    path, method = request.url.path, request.method.upper()
    if _TOKEN_BLOCKED.search(path) and not (path == "/api/auth/me" and method == "GET"):
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
