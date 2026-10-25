"""API tokens (for scripts), webhooks (Benchtop calls other services) and plugin files."""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import select

from .. import audit, plugins, webhooks
from ..models import ApiToken, PluginState, Webhook, WebhookDelivery, utcnow
from ..security import DB, Admin, CurrentUser, hash_api_token, new_api_token

router = APIRouter(prefix="/api", tags=["integrations"])


# ====================================================================== API tokens

def token_out(t: ApiToken) -> dict[str, Any]:
    now = utcnow()
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "scope": t.scope, "created_at": t.created_at.isoformat() + "Z",
            "expires_at": t.expires_at.isoformat() + "Z" if t.expires_at else None,
            "last_used_at": t.last_used_at.isoformat() + "Z" if t.last_used_at else None, "last_used_ip": t.last_used_ip,
            "expired": bool(t.expires_at and t.expires_at < now)}


class TokenIn(BaseModel):
    name: str
    scope: str = "read"
    days: int | None = 90  # None = never expires


@router.get("/auth/tokens")
def list_tokens(user: CurrentUser, session: DB):
    rows = session.exec(select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at == None)  # noqa: E711
                        .order_by(ApiToken.id.desc())).all()
    return [token_out(t) for t in rows]


@router.post("/auth/tokens", status_code=201)
def create_token(body: TokenIn, user: CurrentUser, session: DB):
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(422, "Give the token a name, like “Plate reader script”")
    if body.scope not in ("read", "write"):
        raise HTTPException(422, "Scope must be read or write")
    if body.scope == "write" and user.role.value == "viewer":
        raise HTTPException(403, "Read-only accounts can only make read tokens")
    if body.days is not None and not 1 <= body.days <= 366:
        raise HTTPException(422, "Tokens last between 1 and 366 days (or never expire)")
    if len(list_tokens(user, session)) >= 25:
        raise HTTPException(409, "You have 25 tokens already; revoke some you no longer use")
    raw = new_api_token()
    t = ApiToken(user_id=user.id, name=name, token_hash=hash_api_token(raw), prefix=raw[:10], scope=body.scope,
                 expires_at=utcnow() + timedelta(days=body.days) if body.days else None)
    session.add(t)
    session.flush()
    audit.record(session, user, "api_token_created", "user", user.id, token_id=t.id, name=name, scope=t.scope, days=body.days)
    session.commit()
    return {**token_out(t), "token": raw}  # the only time the full token is shown


@router.delete("/auth/tokens/{token_id}")
def revoke_token(token_id: int, user: CurrentUser, session: DB):
    t = session.get(ApiToken, token_id)
    if t is None or t.revoked_at is not None or (t.user_id != user.id and user.role.value != "admin"):
        raise HTTPException(404, "No such token")
    t.revoked_at = utcnow()
    session.add(t)
    audit.record(session, user, "api_token_revoked", "user", t.user_id, token_id=t.id, name=t.name)
    session.commit()
    return {"ok": True}


# ====================================================================== webhooks

def hook_out(session, h: Webhook) -> dict[str, Any]:
    last = session.exec(select(WebhookDelivery).where(WebhookDelivery.webhook_id == h.id).order_by(WebhookDelivery.id.desc())).first()
    return {"id": h.id, "name": h.name, "url": h.url, "events": h.events or [], "format": h.format, "include_private": h.include_private,
            "active": h.active, "has_secret": bool(h.secret), "created_at": h.created_at.isoformat() + "Z",
            "last": delivery_out(last) if last else None}


def delivery_out(d: WebhookDelivery) -> dict[str, Any]:
    return {"id": d.id, "event": d.event, "created_at": d.created_at.isoformat() + "Z", "status_code": d.status_code, "ok": d.ok,
            "error": d.error, "attempts": d.attempts, "duration_ms": d.duration_ms}


class HookIn(BaseModel):
    name: str = ""
    url: str | None = None
    events: list[str] | None = None
    format: str | None = None
    include_private: bool | None = None
    active: bool | None = None


def _check_hook(body: HookIn) -> None:
    if body.url is not None:
        u = urlparse(body.url.strip())
        if u.scheme not in ("https", "http") or not u.netloc:
            raise HTTPException(422, "The address must start with https:// (or http:// on your own network)")
    if body.events is not None:
        bad = [e for e in body.events if e not in webhooks.EVENTS]
        if bad:
            raise HTTPException(422, f"Unknown event: {', '.join(bad)}")
        if not body.events:
            raise HTTPException(422, "Pick at least one event")
    if body.format is not None and body.format not in ("json", "slack", "teams"):
        raise HTTPException(422, "Format must be json, slack or teams")


@router.get("/webhooks")
def list_hooks(user: Admin, session: DB):
    return {"events": webhooks.EVENTS, "webhooks": [hook_out(session, h) for h in session.exec(select(Webhook).order_by(Webhook.id)).all()]}


@router.post("/webhooks", status_code=201)
def create_hook(body: HookIn, user: Admin, session: DB):
    if not body.url or not body.events:
        raise HTTPException(422, "Give an address and at least one event")
    _check_hook(body)
    fmt = body.format or ("slack" if "hooks.slack.com" in body.url else "json")
    h = Webhook(name=(body.name or urlparse(body.url).netloc)[:80], url=body.url.strip(), events=body.events, format=fmt,
                include_private=bool(body.include_private), active=True if body.active is None else body.active,
                secret=secrets.token_hex(24), created_by=user.id)
    session.add(h)
    session.flush()
    audit.record(session, user, "create", "webhook", h.id, name=h.name, host=urlparse(h.url).netloc, events=h.events)
    session.commit()
    return {**hook_out(session, h), "secret": h.secret}


@router.patch("/webhooks/{hook_id}")
def update_hook(hook_id: int, body: HookIn, user: Admin, session: DB):
    h = session.get(Webhook, hook_id)
    if h is None:
        raise HTTPException(404, "No such webhook")
    _check_hook(body)
    changes = {}
    for k in ("name", "url", "events", "format", "include_private", "active"):
        v = getattr(body, k)
        if v is not None and v != getattr(h, k):
            setattr(h, k, v.strip()[:80] if k == "name" else v.strip() if k == "url" else v)
            changes[k] = v if k != "url" else urlparse(v).netloc
    if changes:
        session.add(h)
        audit.record(session, user, "update", "webhook", h.id, changes=changes)
        session.commit()
    return hook_out(session, h)


@router.post("/webhooks/{hook_id}/secret")
def rotate_secret(hook_id: int, user: Admin, session: DB):
    h = session.get(Webhook, hook_id)
    if h is None:
        raise HTTPException(404, "No such webhook")
    h.secret = secrets.token_hex(24)
    session.add(h)
    audit.record(session, user, "webhook_secret_rotated", "webhook", h.id)
    session.commit()
    return {"secret": h.secret}


@router.delete("/webhooks/{hook_id}")
def delete_hook(hook_id: int, user: Admin, session: DB):
    h = session.get(Webhook, hook_id)
    if h is None:
        raise HTTPException(404, "No such webhook")
    for d in session.exec(select(WebhookDelivery).where(WebhookDelivery.webhook_id == h.id)).all():
        session.delete(d)
    audit.record(session, user, "delete", "webhook", h.id, name=h.name)
    session.delete(h)
    session.commit()
    return {"ok": True}


@router.get("/webhooks/{hook_id}/deliveries")
def hook_deliveries(hook_id: int, user: Admin, session: DB):
    return [delivery_out(d) for d in session.exec(select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook_id)
                                                  .order_by(WebhookDelivery.id.desc()).limit(50)).all()]


@router.post("/webhooks/{hook_id}/test")
def test_hook(hook_id: int, user: Admin, session: DB):
    h = session.get(Webhook, hook_id)
    if h is None:
        raise HTTPException(404, "No such webhook")
    payload = {"event": "test", "timestamp": utcnow().isoformat() + "Z", "benchtop": "test",
               "actor": {"username": user.username, "name": user.full_name or user.username},
               "message": "Test from Benchtop: if you can read this, the webhook works."}
    d = webhooks.deliver(h.id, payload, retries=1)
    return delivery_out(d)


# ====================================================================== plugins

@router.get("/plugins")
def list_plugins(user: Admin, session: DB):
    return {"api": plugins.API_VERSION, "folder": str(plugins.user_dir()), "plugins": [p.out() for p in plugins.with_state(session)]}


@router.get("/plugins/sections")
def plugin_sections(user: CurrentUser, session: DB):
    """Section types from switched-on plugins, for the "Add section" menu."""
    return [{"key": f"{s['plugin']}/{s['id']}", "name": s["name"], "description": s["description"], "plugin": s["plugin"]}
            for s in plugins.enabled_sections(session)]


@router.get("/plugins/readers")
def plugin_readers(user: CurrentUser, session: DB):
    return plugins.enabled_readers(session)


class PluginToggle(BaseModel):
    enabled: bool


@router.post("/plugins/{plugin_id}/enabled")
def toggle_plugin(plugin_id: str, body: PluginToggle, user: Admin, session: DB):
    p = next((p for p in plugins.discover() if p.id == plugin_id), None)
    if p is None:
        raise HTTPException(404, "No such plugin")
    if body.enabled and p.errors:
        raise HTTPException(422, "Fix this plugin's errors before switching it on")
    st = session.get(PluginState, plugin_id) or PluginState(plugin_id=plugin_id)
    st.enabled, st.changed_by, st.changed_at = body.enabled, user.id, utcnow()
    session.add(st)
    audit.record(session, user, "plugin_enabled" if body.enabled else "plugin_disabled", "plugin", None, plugin=plugin_id, version=p.version)
    session.commit()
    return {"ok": True}


@router.post("/plugins/check")
async def check_plugin(user: Admin, file: UploadFile = File(...)):
    """Read a plugin file without installing it: what it adds, or what's wrong with it."""
    try:
        p = plugins.parse((await file.read(200_001))[:200_000].decode("utf-8"))
    except (plugins.PluginError, UnicodeDecodeError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "plugin": p.out()}


@router.post("/plugins", status_code=201)
def install_plugin(user: Admin, session: DB, file: UploadFile = File(...)):
    raw = file.file.read(200_001)
    if len(raw) > 200_000:
        raise HTTPException(413, "Plugin files are small text files; this one is too big")
    try:
        text = raw.decode("utf-8")
        p = plugins.parse(text)
    except UnicodeDecodeError:
        raise HTTPException(422, "That isn't a text file") from None
    except plugins.PluginError as e:
        raise HTTPException(422, str(e)) from None
    dest = plugins.user_dir() / f"{p.id}.toml"
    replaced = dest.exists()
    for other in plugins.discover():  # an older copy of the same plugin under another file name
        if other.id == p.id and not other.bundled and other.path and other.path != str(dest):
            __import__("pathlib").Path(other.path).unlink(missing_ok=True)
            replaced = True
    dest.write_text(text, encoding="utf-8")
    st = session.get(PluginState, p.id) or PluginState(plugin_id=p.id, enabled=True)
    st.changed_by, st.changed_at = user.id, utcnow()
    session.add(st)
    audit.record(session, user, "plugin_installed", "plugin", None, plugin=p.id, version=p.version, replaced=replaced,
                 sha256=__import__("hashlib").sha256(raw).hexdigest())
    session.commit()
    return {"plugin": p.out(), "replaced": replaced}


@router.delete("/plugins/{plugin_id}")
def remove_plugin(plugin_id: str, user: Admin, session: DB):
    from pathlib import Path
    mine = [Path(p.path) for p in plugins.discover() if p.id == plugin_id and not p.bundled]
    mine += [f for f in plugins.user_dir().glob("*.toml") if f.stem == plugin_id and f not in mine]
    if not re.match(r"^[a-z0-9-]+$", plugin_id) or not mine:
        raise HTTPException(404, "Only plugins you added can be removed; switch bundled examples off instead")
    for f in mine:
        f.unlink(missing_ok=True)
    st = session.get(PluginState, plugin_id)
    if st:
        session.delete(st)
    audit.record(session, user, "plugin_removed", "plugin", None, plugin=plugin_id)
    session.commit()
    return {"ok": True}
