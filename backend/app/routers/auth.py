from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import select

from .. import audit
from ..models import Role, User
from ..config import settings
from .. import passwords
from ..patching import cleaned
from ..models import AuthSession, utcnow
from ..security import (MAX_PER_ACCOUNT, MAX_PER_IP, DB, Admin, CurrentUser, burn_time, check_throttle, clear_failures,
                        client_ip, end_sessions, hash_password, needs_rehash, record_failure, start_session, verify_password)

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str
    remember: bool = False  # keep me signed in for 30 days


REMEMBER_HOURS = 24 * 30


def user_out(u: User) -> dict:
    from ..security import has_password
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "email": u.email,
            "role": u.role.value, "active": u.active, "preferences": u.preferences or {},
            "has_password": has_password(u.password_hash), "must_change_password": bool(u.must_change_password),
            "sample_prefix": u.sample_prefix, "orcid": u.orcid}


def clean_orcid(value: str) -> str | None:
    """An ORCID iD as 0000-0002-1825-0097 (pasting the https://orcid.org/… link works too), or None to remove it.
    The last character is a checksum (ISO 7064 11,2), so a typo is caught here rather than on someone's thesis."""
    v = re.sub(r"^\s*(https?://)?(www\.)?orcid\.org/", "", value or "", flags=re.I).strip().upper().replace(" ", "")
    if not v:
        return None
    digits = v.replace("-", "")
    if not re.fullmatch(r"\d{15}[\dX]", digits):
        raise HTTPException(422, "An ORCID iD has 16 characters, like 0000-0002-1825-0097")
    total = 0
    for ch in digits[:15]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    if digits[15] != ("X" if check == 10 else str(check)):
        raise HTTPException(422, "That ORCID iD has a typo: its last character doesn't match the others. Copy it from orcid.org")
    return "-".join(digits[i:i + 4] for i in range(0, 16, 4))


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
NAV_KEYS = ("dashboard", "inbox", "reactions", "compounds", "search", "materials", "inventory", "templates", "groups", "admin")
PALETTES = ("benchtop", "notebook", "steel", "litmus", "contrast")  # frontend/scripts/themes.py
PREF_KEYS = {"theme": ("system", "light", "dark"), "palette": PALETTES, "default_notebook_id": None, "spectrum_expanded": (True, False)}


def _check_email(email: str) -> str:
    email = email.strip()
    if email and not EMAIL.match(email):
        raise HTTPException(422, "That doesn't look like an email address")
    return email


def _check_username(session, name: str, me: int | None = None) -> str:
    name = name.strip().lower()
    if not USERNAME.match(name):
        raise HTTPException(422, "Usernames are 2–32 characters: lower-case letters, digits, '.', '_' or '-'")
    other = session.exec(select(User).where(User.username == name)).first()
    if other and other.id != me:
        raise HTTPException(409, "That username is taken")
    return name


WIDGET_TYPES = {"list", "actions", "note", "header", "yield", "month", "spectra", "inventory"}
LIST_KINDS = {"recent", "open", "awaiting", "returned", "pinned", "stale", "waiting"}


def _clean_dashboard(d: Any) -> dict[str, Any] | None:
    if not isinstance(d, dict) or not isinstance(d.get("widgets"), list):
        return None
    widgets = []
    for w in d["widgets"][:40]:
        if not isinstance(w, dict) or w.get("type") not in WIDGET_TYPES:
            continue
        num = lambda k, lo, hi, dflt: max(lo, min(hi, int(w.get(k, dflt)) if isinstance(w.get(k, dflt), (int, float)) else dflt))  # noqa: E731
        item = {"i": str(w.get("i") or "")[:40] or f"w{len(widgets)}", "type": w["type"],
                "x": num("x", 0, 11, 0), "y": num("y", 0, 500, 0), "w": num("w", 2, 12, 4), "h": num("h", 2, 20, 4)}
        if w["type"] == "list":
            item["list"] = w.get("list") if w.get("list") in LIST_KINDS else "recent"
        if w["type"] in ("note", "header"):
            item["text"] = str(w.get("text") or "")[:4000]
        if isinstance(w.get("title"), str):
            item["title"] = w["title"][:80]
        widgets.append(item)
    return {"widgets": widgets}


def _clean_prefs(p: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in p.items():
        if k == "dashboard":
            cleaned = _clean_dashboard(v)
            out[k] = cleaned
            continue
        if k == "pinned":
            out[k] = [int(i) for i in (v or [])[:100] if isinstance(i, (int, float))] if isinstance(v, list) else []
            continue
        if k == "email_notify":  # which emails this person gets
            out[k] = {t: bool(v[t]) for t in ("witness", "results", "system") if isinstance(v, dict) and t in v}
            continue
        if k == "guide_dismissed":  # the dashboard's "New to Benchtop?" card was put away: it stays away on every computer
            if isinstance(v, bool):
                out[k] = v
            continue
        if k in ("nav", "nav_hidden"):  # sidebar order and hidden items, as known keys
            out[k] = list(dict.fromkeys(i for i in v if i in NAV_KEYS)) if isinstance(v, list) else []
            continue
        if k not in PREF_KEYS:
            continue
        allowed = PREF_KEYS[k]
        if allowed is None:
            out[k] = int(v) if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()) else None
        elif v in allowed:
            out[k] = v
    return out


class ProfileIn(BaseModel):
    full_name: str | None = None
    email: str | None = None
    orcid: str | None = None
    preferences: dict[str, Any] | None = None


@router.patch("/auth/me")
def update_me(body: ProfileIn, user: CurrentUser, session: DB):
    changes: dict[str, Any] = {}
    if body.full_name is not None:
        name = body.full_name.strip()
        if not name:
            raise HTTPException(422, "Your name can't be empty; it appears on your signatures")
        if name != user.full_name:
            changes["full_name"] = {"from": user.full_name, "to": name}
            user.full_name = name
    if body.email is not None:
        email = _check_email(body.email)
        if email != user.email:
            changes["email"] = {"from": user.email, "to": email}
            user.email = email
    if body.orcid is not None:
        orcid = clean_orcid(body.orcid)
        if orcid != user.orcid:
            changes["orcid"] = {"from": user.orcid, "to": orcid}
            user.orcid = orcid
    if body.preferences is not None:
        prefs = {**(user.preferences or {}), **_clean_prefs(body.preferences)}
        if prefs != (user.preferences or {}):
            user.preferences = prefs
            changes["preferences"] = prefs
    if changes:
        session.add(user)
        audit.record(session, user, "update_profile", "user", user.id, changes=changes)
        session.commit()
    return user_out(user)


class UsernameIn(BaseModel):
    new_username: str
    password: str


@router.post("/auth/username")
def change_username(body: UsernameIn, user: CurrentUser, session: DB):
    _confirm_password(session, user, body.password)
    new = _check_username(session, body.new_username, user.id)
    old = user.username
    if new != old:
        user.username = new
        session.add(user)
        audit.record(session, user, "change_username", "user", user.id, old=old, new=new)
        session.commit()
    return user_out(user)


def _confirm_password(session, user: User, password: str) -> None:
    """Re-check someone's password before a sensitive change; guesses are throttled like sign-ins."""
    key = f"confirm:{user.id}"
    check_throttle(session, (key, MAX_PER_ACCOUNT))
    if not verify_password(password, user.password_hash):
        record_failure(session, key)
        audit.record(session, user, "password_check_failed", "user", user.id)
        session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password is wrong")
    clear_failures(session, key)


def _default_admin_untouched(session) -> bool:
    """A fresh install that still has the built-in admin/admin login (only possible before setup)."""
    return settings.admin_password == "admin" and _needs_setup(session)


def _is_local(request: Request) -> bool:
    return client_ip(request) in ("127.0.0.1", "::1", "localhost", "testclient")


@router.post("/auth/login")
def login(body: LoginIn, request: Request, session: DB):
    name = body.username.strip().lower()
    acct_key, ip_key = f"login:{name}", f"ip:{client_ip(request)}"
    check_throttle(session, (acct_key, MAX_PER_ACCOUNT), (ip_key, MAX_PER_IP))
    user = (session.exec(select(User).where(User.username == body.username)).first()
            or session.exec(select(User).where(User.username == name)).first())
    if user is None:
        burn_time(body.password)  # same delay as a real check, so usernames can't be probed by timing
    ok = bool(user) and user.active and verify_password(body.password, user.password_hash)
    if ok and user.username == settings.admin_username and _default_admin_untouched(session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Finish setting up Benchtop first: create your own account on the welcome screen")
    if not ok:
        record_failure(session, acct_key, ip_key)
        audit.record(session, None, "login_failed", "user", user.id if user else None,
                     username=body.username, ip=client_ip(request))
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    if not settings.password_login and user.role != Role.admin:  # admins keep a password as a way in if SSO breaks
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This Benchtop uses single sign-on; use the sign-in button instead")
    clear_failures(session, acct_key)
    if needs_rehash(user.password_hash):  # quietly move older hashes to the current strength
        user.password_hash = hash_password(body.password)
        session.add(user)
    token = start_session(session, user, request, REMEMBER_HOURS if body.remember else None)
    audit.record(session, user, "login", "user", user.id, remember=body.remember, ip=client_ip(request))
    session.commit()
    return {"token": token, "user": user_out(user)}


# ------------------------------------------------------------------ first-run setup

def _needs_setup(session) -> bool:
    """True on a fresh install: the only account is the built-in admin, still on its default password."""
    users = session.exec(select(User)).all()
    if len(users) != 1:
        return False
    u = users[0]
    return u.username == settings.admin_username and u.active and verify_password(settings.admin_password, u.password_hash)


@router.get("/setup")
def setup_status(session: DB):
    return {"needs_setup": _needs_setup(session)}


class SetupIn(BaseModel):
    full_name: str
    username: str
    email: str = ""
    password: str
    load_demo: bool = False


@router.post("/setup", status_code=201)
def run_setup(body: SetupIn, request: Request, session: DB):
    """Create the first real account (an admin) and retire the default admin login."""
    if not _needs_setup(session):
        raise HTTPException(409, "Setup has already been done; sign in instead")
    if settings.admin_password == "admin" and not _is_local(request):
        # with the built-in default, only the computer Benchtop runs on may claim the admin account
        raise HTTPException(403, "Finish setup on the computer Benchtop is running on (open http://localhost:8000 there)")
    if not body.full_name.strip():
        raise HTTPException(422, "Your name appears on your signatures, so it can't be empty")
    username = _check_username(session, body.username)
    passwords.check(body.password, username, body.full_name, body.email)
    default = session.exec(select(User)).one()
    if username == default.username:
        raise HTTPException(422, f"Pick a username other than '{default.username}'")
    me = User(username=username, full_name=body.full_name.strip(), email=_check_email(body.email),
              role=Role.admin, password_hash=hash_password(body.password), password_changed_at=utcnow())
    session.add(me)
    session.flush()
    default.active = False  # kept (not deleted) so earlier audit entries still resolve
    session.add(default)
    audit.record(session, me, "setup", "user", me.id, username=me.username, retired_default=default.username)
    session.commit()
    demo = False
    if body.load_demo:
        from fastapi.testclient import TestClient

        from ..main import app
        from ..seed import load_demo
        demo = load_demo(TestClient(app))
    token = start_session(session, me, request, REMEMBER_HOURS, method="setup")
    session.commit()
    return {"token": token, "user": user_out(me), "demo_loaded": demo}


@router.get("/auth/email-status")
def email_status(_: CurrentUser):
    """Whether this Benchtop sends email (for Account settings)."""
    from .. import mailer
    return {"configured": mailer.configured()}


@router.get("/auth/me")
def me(user: CurrentUser):
    return user_out(user)


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/password")
def change_password(body: PasswordIn, user: CurrentUser, session: DB):
    """Change your password, or (for an account made by single sign-on) set one: signatures need it.
    Every other signed-in browser is signed out, in case the old password was the problem."""
    from ..security import has_password
    if has_password(user.password_hash) and not user.must_change_password:
        try:
            _confirm_password(session, user, body.current_password)
        except HTTPException as e:
            if e.status_code == 400:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong") from None
            raise
    passwords.check(body.new_password, user.username, user.full_name, user.email)
    if has_password(user.password_hash) and verify_password(body.new_password, user.password_hash):
        raise HTTPException(422, "Choose a password different from your current one.")
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = None
    user.password_changed_at = utcnow()
    session.add(user)
    ended = end_sessions(session, user.id, keep=getattr(user, "_sid", None))
    audit.record(session, user, "password_changed", "user", user.id, other_sessions_ended=ended)
    session.commit()
    return {"ok": True, "other_sessions_ended": ended}


class PasswordCheckIn(BaseModel):
    password: str
    username: str = ""
    full_name: str = ""
    email: str = ""


@router.post("/auth/password-check")
def password_check(body: PasswordCheckIn):
    """Live feedback while someone types a new password (the same rules the server enforces)."""
    return {"problems": passwords.problems(body.password, body.username, body.full_name, body.email), "min_length": passwords.MIN_LEN}


# ------------------------------------------------------------------ sessions

def _session_out(s: AuthSession, current: str | None) -> dict:
    return {"id": s.id, "created_at": s.created_at.isoformat() + "Z", "last_seen_at": s.last_seen_at.isoformat() + "Z",
            "expires_at": s.expires_at.isoformat() + "Z", "method": s.method, "user_agent": s.user_agent, "ip": s.ip,
            "current": s.id == current}


@router.get("/auth/sessions")
def my_sessions(user: CurrentUser, session: DB):
    rows = session.exec(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at == None,  # noqa: E711
                                                  AuthSession.expires_at > utcnow()).order_by(AuthSession.last_seen_at.desc())).all()
    cur = getattr(user, "_sid", None)
    return [_session_out(s, cur) for s in rows]


@router.delete("/auth/sessions/{sid}")
def end_session(sid: str, user: CurrentUser, session: DB):
    s = session.get(AuthSession, sid)
    if s is None or s.user_id != user.id:
        raise HTTPException(404, "No such session")
    if s.revoked_at is None:
        s.revoked_at = utcnow()
        session.add(s)
        audit.record(session, user, "session_ended", "user", user.id, ip=s.ip)
        session.commit()
    return {"ok": True}


@router.post("/auth/sessions/end-others")
def end_other_sessions(user: CurrentUser, session: DB):
    n = end_sessions(session, user.id, keep=getattr(user, "_sid", None))
    audit.record(session, user, "sessions_ended", "user", user.id, count=n)
    session.commit()
    return {"ended": n}


@router.post("/auth/logout")
def logout(user: CurrentUser, session: DB):
    s = session.get(AuthSession, getattr(user, "_sid", ""))
    if s and s.revoked_at is None:
        s.revoked_at = utcnow()
        session.add(s)
        audit.record(session, user, "logout", "user", user.id)
        session.commit()
    return {"ok": True}


# ------------------------------------------------------------------ user admin

@router.get("/users")
def list_users(_: CurrentUser, session: DB):
    from ..instruments import SYSTEM_USERNAME  # the account instrument files arrive under isn't a person
    return [user_out(u) for u in session.exec(select(User).where(User.username != SYSTEM_USERNAME).order_by(User.username))]


class UserIn(BaseModel):
    username: str
    password: str
    full_name: str = ""
    email: str = ""
    role: Role = Role.scientist
    must_change: bool = True  # they choose their own password at first sign-in


@router.post("/users", status_code=201)
def create_user(body: UserIn, actor: Admin, session: DB):
    if session.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Username taken")
    uname = _check_username(session, body.username)
    passwords.check(body.password, uname, body.full_name, body.email)
    # the person chooses their own password the first time they sign in
    u = User(username=uname, full_name=body.full_name, email=_check_email(body.email),
             role=body.role, password_hash=hash_password(body.password), must_change_password=True if body.must_change else None)
    session.add(u)
    session.flush()
    audit.record(session, actor, "create", "user", u.id, username=u.username, role=u.role.value)
    session.commit()
    return user_out(u)


class UserPatch(BaseModel):
    username: str | None = None
    full_name: str | None = None
    email: str | None = None
    role: Role | None = None
    active: bool | None = None
    password: str | None = None


@router.patch("/users/{uid}")
def update_user(uid: int, body: UserPatch, actor: Admin, session: DB):
    u = session.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    changes = cleaned(u, body.model_dump(exclude_unset=True))
    if "username" in changes:
        changes["username"] = _check_username(session, changes["username"], u.id)
    if "email" in changes and changes["email"] is not None:
        changes["email"] = _check_email(changes["email"])
    ended = 0
    if "password" in changes:
        new = changes.pop("password")
        passwords.check(new, u.username, u.full_name, u.email)
        u.password_hash = hash_password(new)
        u.must_change_password = True  # a password someone else knows: they pick their own at next sign-in
        changes["password"] = "(reset)"
        ended = end_sessions(session, u.id)
    if changes.get("active") is False:
        ended = end_sessions(session, u.id)
    for k, v in changes.items():
        if k != "password":
            setattr(u, k, v)
    session.add(u)
    audit.record(session, actor, "update", "user", u.id, changes=changes, sessions_ended=ended)
    session.commit()
    return user_out(u)


@router.post("/users/{uid}/sign-out")
def sign_out_user(uid: int, actor: Admin, session: DB):
    """Admin: end every session of an account (a lost laptop, someone leaving the lab)."""
    u = session.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    n = end_sessions(session, u.id, keep=getattr(actor, "_sid", None) if actor.id == u.id else None)
    audit.record(session, actor, "sessions_ended", "user", u.id, count=n, by_admin=True)
    session.commit()
    return {"ended": n}
