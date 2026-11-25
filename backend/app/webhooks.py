"""Outgoing webhooks: Benchtop calls other services when something happens.

Events are collected while a request runs (from the audit log) and sent only after its database
transaction commits, so a webhook never announces a change that was rolled back. Delivery happens on
background threads, never in the request that caused it; each request is signed with the webhook's secret
(X-Benchtop-Signature: sha256=<HMAC of the body>) so the receiver can check it came from Benchtop.

0.36.6, for receivers that are slow, never answer, answer with a flood, or are gone:
  * each try has short time limits (5 s to connect, 15 s in all) and reads only the status of the reply;
  * a failed delivery is tried three times in all, 30 s and 2 min apart, and never more;
  * each webhook has its own queue, worked on by one thread at a time, so a receiver that hangs only delays its
    own deliveries, never another webhook's or anyone's request;
  * webhooks only call public addresses: never this computer, the private network or cloud metadata, whether given
    as a name, a number in any form, or a name that later points there (each address is checked as it's
    called, then that address is used), unless the server's BENCHTOP_WEBHOOK_ALLOW lists it. Redirects aren't
    followed.

Payloads carry identifiers, titles and who did what, never the content of a record.
"""
from __future__ import annotations

import hashlib
import heapq
import hmac
import ipaddress
import itertools
import json
import logging
import socket
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import event as sa_event
from sqlmodel import Session, select

from . import __version__
from .config import settings
from .models import Experiment, Notebook, User, Webhook, WebhookDelivery, utcnow

log = logging.getLogger("benchtop.webhooks")

EVENTS = {
    "experiment.created": "An experiment is created (or cloned)",
    "experiment.signed": "The author signs an experiment",
    "experiment.witnessed": "An experiment is witnessed",
    "experiment.returned": "A witness returns an experiment to its author",
    "experiment.withdrawn": "An author withdraws their signature",
    "experiment.voided": "An experiment is voided",
    "comment.created": "Someone comments on an experiment",
    "material.created": "A material is registered",
}
_ACTIONS = {
    ("create", "experiment"): "experiment.created", ("clone", "experiment"): "experiment.created",
    ("sign:author", "experiment"): "experiment.signed", ("sign:witness", "experiment"): "experiment.witnessed",
    ("sign:rejected", "experiment"): "experiment.returned", ("sign:withdrawn", "experiment"): "experiment.withdrawn",
    ("sign:void", "experiment"): "experiment.voided", ("comment", "experiment"): "comment.created",
    ("create", "material"): "material.created",
}
VERBS = {
    "experiment.created": "created", "experiment.signed": "signed", "experiment.witnessed": "witnessed",
    "experiment.returned": "returned to its author", "experiment.withdrawn": "withdrew the signature on",
    "experiment.voided": "voided", "comment.created": "commented on", "material.created": "registered",
}

_KEY = "benchtop_events"
# tests set this to deliver inline; a callable(url, body, headers) -> status code may replace HTTP
synchronous = False
sender = None

CONNECT_SECONDS = 5.0    # to reach the receiver
READ_SECONDS = 5.0       # between two pieces of its reply
TRY_SECONDS = 15.0       # one try, in all, whatever the receiver does
RETRY_AFTER = (30, 120)  # seconds before the second and the third (last) try
WORKERS = 4              # deliveries under way at once, at most one per webhook
MAX_WAITING = 200        # per webhook: beyond this, new events for it are dropped (and the drop recorded)


def note(session: Session, user: User | None, action: str, object_type: str, object_id: int | None, details: dict) -> None:
    """Called by audit.record: remember the event until the transaction commits."""
    ev = _ACTIONS.get((action, object_type))
    if ev and object_id is not None:
        session.info.setdefault(_KEY, []).append((ev, object_type, object_id, user.id if user else None, dict(details)))


@sa_event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    # Never touch the database here: the request still holds its connection, and opening a second one
    # per commit exhausted the pool under load (requests waited 30 s and failed). The worker does it.
    pending = session.info.pop(_KEY, None)
    if pending:
        for item in pending:
            if synchronous:
                _dispatch(*item)
            else:
                _q.put(("event", item))


@sa_event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_KEY, None)


def _url(path: str) -> str:
    return (settings.public_url or f"http://localhost:{settings.port}") + path


def build_payload(s: Session, ev: str, object_type: str, object_id: int, user_id: int | None, details: dict) -> tuple[dict, bool]:
    """(payload, is_private): what a receiver gets for one event."""
    actor = s.get(User, user_id) if user_id else None
    p: dict[str, Any] = {
        "event": ev, "timestamp": utcnow().isoformat() + "Z", "benchtop": __version__,
        "actor": {"username": actor.username, "name": actor.full_name or actor.username} if actor else None,
    }
    private = False
    if object_type == "experiment":
        exp = s.get(Experiment, object_id)
        if exp is None:
            return p, True
        nb = s.get(Notebook, exp.notebook_id) if exp.notebook_id else None
        owner = s.get(User, exp.owner_id)
        # 0.36.6: anything not every account can read counts as private: a group-only notebook's records, and records
        # in no notebook (only their owner and people they're shared with can open those). Before, only private
        # notebooks were held back, so group-only titles reached a lab-wide Slack channel. Templates are open to all.
        private = not exp.is_template and not (nb and (nb.visibility or "everyone") == "everyone")
        p["experiment"] = {
            "id": exp.id, "eid": exp.eid, "title": exp.title, "status": exp.status.value,
            "author": owner.full_name or owner.username if owner else None,
            "notebook": nb.name if nb else None, "url": _url(f"/experiments/{exp.id}"),
        }
        if ev == "comment.created":
            p["comment_id"] = details.get("comment_id")
        if details.get("comment") and ev in ("experiment.returned", "experiment.voided"):
            p["reason"] = str(details["comment"])[:500]
    elif object_type == "material":
        from .models import Material
        m = s.get(Material, object_id)
        if m:
            p["material"] = {"id": m.id, "mid": m.mid, "name": m.name, "url": _url(f"/materials/{m.id}")}
    return p, private


def summary(p: dict) -> str:
    who = (p.get("actor") or {}).get("name") or "Benchtop"
    verb = VERBS.get(p["event"], p["event"])
    if "experiment" in p:
        e = p["experiment"]
        s = f"{who} {verb} {e['eid']}: {e['title']}"
        if p.get("reason"):
            s += f" (“{p['reason']}”)"
        return s
    if "material" in p:
        return f"{who} {verb} {p['material']['mid']}: {p['material']['name']}"
    return f"{who}: {p['event']}"


def body_for(hook: Webhook, p: dict) -> dict:
    link = (p.get("experiment") or p.get("material") or {}).get("url")
    if hook.format == "slack":  # Slack incoming webhooks (also Mattermost, Rocket.Chat, Discord's /slack endpoint)
        return {"text": summary(p) + (f" <{link}|Open>" if link else "")}
    if hook.format == "teams":  # Microsoft Teams workflow / incoming webhook: simple text card
        return {"text": summary(p) + (f" [Open]({link})" if link else "")}
    return p


def sign_body(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _dispatch(ev: str, object_type: str, object_id: int, user_id: int | None, details: dict) -> None:
    from .db import engine
    with Session(engine) as s:
        hooks = [h for h in s.exec(select(Webhook).where(Webhook.active == True)).all() if ev in (h.events or [])]  # noqa: E712
        if not hooks:
            return
        payload, private = build_payload(s, ev, object_type, object_id, user_id, details)
    for h in hooks:
        if private and not h.include_private:
            continue
        if synchronous:
            deliver(h.id, payload)
        elif not _submit(h.id, (h.id, h.created_at, payload, 1, None)):
            _record_dropped(h.id, payload["event"])


# ------------------------------------------------------------------ where a webhook may send (0.36.6)

class Refused(ValueError):
    """The address is this computer, the private network or another place webhooks don't go."""


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host stands for when it's written as one, in any form the system accepts: 127.0.0.1, [::1],
    2130706433, 0x7f.1, 0177.0.0.1 or 127.1 (all the same address). None for a name."""
    h = host.strip("[]").split("%")[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(h))
    except (OSError, ValueError):
        return None


def _unwrap(ip):
    """IPv6 addresses that carry an IPv4 one (::ffff:127.0.0.1, 6to4, NAT64) are judged by the IPv4 address."""
    if ip.version == 6:
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip.sixtofour:
            return ip.sixtofour
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if int(ip) >> 32 == 0 and int(ip) > 1:  # ::127.0.0.1, the old "IPv4-compatible" form
            return ipaddress.IPv4Address(int(ip))
    return ip


def _allowed(host: str, ip=None) -> bool:
    for a in settings.webhook_allow:
        try:
            net = ipaddress.ip_network(a.strip("[]"), strict=False)
        except ValueError:
            if host.lower().rstrip(".") == a.rstrip("."):
                return True
            continue
        if ip is not None and (ip in net or _unwrap(ip) in net):
            return True
    return False


def _public(ip) -> bool:
    ip = _unwrap(ip)
    return ip.is_global and not ip.is_multicast


def _refusal(host: str, ip) -> str:
    where = "this computer" if _unwrap(ip).is_loopback or _unwrap(ip).is_unspecified else "a private or reserved network"
    shown = host if host.strip("[]") == str(ip) else f"{host} ({ip})"
    return (f"{shown} is on {where}, and webhooks only call public addresses. If it's your lab's own receiver, the "
            f"person who runs this server can allow it with BENCHTOP_WEBHOOK_ALLOW={host.strip('[]')} (see docs/API.md).")


def check_address(url: str) -> None:
    """What can be judged without looking the name up: used when a webhook is saved. Raises Refused."""
    host = urlsplit(url.strip()).hostname or ""
    ip = _ip(host)
    if ip is None:
        if (host.rstrip(".") == "localhost" or host.rstrip(".").endswith(".localhost")) and not _allowed(host):
            raise Refused(f"{host} is this computer, and webhooks only call public addresses. If it's your lab's own "
                          f"receiver, the person who runs this server can allow it with BENCHTOP_WEBHOOK_ALLOW={host} "
                          "(see docs/API.md).")
        return
    if not _public(ip) and not _allowed(host, ip):
        raise Refused(_refusal(host, ip))


def resolve(host: str, port: int) -> list[str]:
    """Every address the host stands for now, each checked; raises Refused if any isn't allowed (a name pointing at
    both a public and a private address is refused too), socket.gaierror if there's no such name."""
    out = []
    for info in socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _public(ip) and not _allowed(host, ip):
            raise Refused(_refusal(host, ip))
        if str(ip) not in out:
            out.append(str(ip))
    return out


def _proxied(url: str) -> bool:
    import urllib.request
    host = urlsplit(url).hostname or ""
    return bool(urllib.request.getproxies().get(urlsplit(url).scheme)) and not urllib.request.proxy_bypass(host)


def _post(url: str, raw: bytes, headers: dict[str, str]) -> int:
    """One POST, to an address checked just before (so a name can't be pointed elsewhere between the check and the
    call). Only the status of the reply is read, never its body."""
    u = urlsplit(url)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    target, extensions, hdrs = url, {}, dict(headers)
    try:
        addrs = resolve(host, port)
    except socket.gaierror:
        if not _proxied(url):  # without a proxy there's nowhere to send it; through one, the proxy looks the name up
            raise
        addrs = []
    if addrs:
        ip = addrs[0]
        netloc = f"[{ip}]" if ":" in ip else ip
        target = u._replace(netloc=f"{netloc}:{port}").geturl()
        hdrs["Host"] = u.netloc.rpartition("@")[2]
        if u.scheme == "https":
            extensions["sni_hostname"] = host  # the certificate is still checked against the name
    timeout = httpx.Timeout(READ_SECONDS, connect=CONNECT_SECONDS)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("POST", target, content=raw, headers=hdrs, extensions=extensions) as r:
            return r.status_code  # closing here drops the rest of the reply unread


def _plain(exc: BaseException, host: str) -> str:
    if isinstance(exc, Refused):
        return str(exc)
    if isinstance(exc, socket.gaierror):
        return f"{host} wasn't found. Check the address."
    if isinstance(exc, httpx.ConnectTimeout):
        return f"No answer from {host} within {CONNECT_SECONDS:.0f} seconds. Is it running, and can this server reach it?"
    if isinstance(exc, httpx.ConnectError):
        why = str(exc).rpartition("] ")[2] or str(exc)
        return f"Couldn't connect to {host} ({why[:120]}). Is it running, and is the address right?"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return f"{host} took the request but didn't answer within {READ_SECONDS:.0f} seconds."
    if isinstance(exc, httpx.RemoteProtocolError):
        return f"{host} answered with something that isn't a proper web reply."
    return f"{type(exc).__name__}: {exc}"[:300]


def _attempt(url: str, raw: bytes, headers: dict[str, str]) -> tuple[int | None, str, bool]:
    """(status code, error, worth trying again) of one try. The try runs on its own thread and is given up after TRY_SECONDS, whatever
    the receiver does (a reply sent a byte at a time would otherwise hold on to it for hours)."""
    if sender:
        return sender(url, raw, headers), "", True
    host = urlsplit(url).hostname or url
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["code"] = _post(url, raw, headers)
        except BaseException as exc:  # reported below
            box["exc"] = exc
    t = threading.Thread(target=run, name="benchtop-webhook-post", daemon=True)
    t.start()
    t.join(TRY_SECONDS)
    if t.is_alive():
        return None, f"{host} didn't finish answering within {TRY_SECONDS:.0f} seconds.", True
    if "exc" in box:
        return None, _plain(box["exc"], host), not isinstance(box["exc"], Refused)
    return box["code"], "", True


def _error_for(code: int) -> str:
    if 300 <= code < 400:
        return (f"HTTP {code}: the receiver sends Benchtop on to another address. Redirects aren't followed: "
                "put the final address in the webhook.")
    return f"HTTP {code}"


# ------------------------------------------------------------------ queues: one per webhook

class _Queue:
    """Where committed events wait to be turned into deliveries (kept as an object with put() for the tests)."""
    def put(self, item: tuple[str, tuple]) -> None:
        _submit("events", ("event", item[1]))


_q = _Queue()
_cv = threading.Condition()
_lanes: dict[Any, deque] = {}   # webhook id (or "events") -> jobs waiting, oldest first
_busy: set = set()              # lanes with a job under way
_later: list = []               # (when, n, lane, job): retries waiting for their time
_seq = itertools.count()
_workers: list[threading.Thread] = []


def _submit(lane: Any, job: tuple, delay: float = 0) -> bool:
    with _cv:
        if delay:
            heapq.heappush(_later, (time.monotonic() + delay, next(_seq), lane, job))
        else:
            q = _lanes.setdefault(lane, deque())
            if lane != "events" and len(q) >= MAX_WAITING:
                return False
            q.append(job)
        _cv.notify()
        _workers[:] = [w for w in _workers if w.is_alive()]
        if len(_workers) < WORKERS and sum(len(q) for q in _lanes.values()) + len(_later) > 0:
            w = threading.Thread(target=_run, name="benchtop-webhooks", daemon=True)
            _workers.append(w)
            w.start()
    return True


def _take() -> tuple[Any, tuple]:
    with _cv:
        while True:
            now = time.monotonic()
            while _later and _later[0][0] <= now:
                _, _, lane, job = heapq.heappop(_later)
                _lanes.setdefault(lane, deque()).append(job)
            for lane, q in _lanes.items():
                if q and lane not in _busy:
                    _busy.add(lane)
                    return lane, q.popleft()
            _cv.wait(timeout=min(60.0, _later[0][0] - now) if _later else 60.0)


def _done(lane: Any) -> None:
    with _cv:
        _busy.discard(lane)
        if not _lanes.get(lane):
            _lanes.pop(lane, None)
        _cv.notify_all()


def _run() -> None:
    while True:
        lane, job = _take()
        try:
            if job[0] == "event":
                _dispatch(*job[1])   # find the webhooks that want it and build the payload
            else:
                hook_id, made, payload, attempt, did = job
                d, again = _try(hook_id, payload, attempt, did, last=attempt > len(RETRY_AFTER), made=made)
                if again and d is not None:
                    _submit(hook_id, (hook_id, made, payload, attempt + 1, d.id), delay=RETRY_AFTER[attempt - 1])
        except Exception:  # never let one bad hook stop the others
            log.exception("Webhook delivery crashed")
        finally:
            _done(lane)


def pending() -> int:
    """Deliveries waiting or under way (for tests)."""
    with _cv:
        return sum(len(q) for q in _lanes.values()) + len(_later) + len(_busy)


# ------------------------------------------------------------------ one try, recorded

def _keep_last(s: Session, hook_id: int, n: int = 50) -> None:
    old = s.exec(select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook_id)
                 .order_by(WebhookDelivery.id.desc()).offset(n)).all()
    for o in old:
        s.delete(o)


def _record_dropped(hook_id: int, event: str) -> None:
    from .db import engine
    with Session(engine) as s:
        s.add(WebhookDelivery(webhook_id=hook_id, event=event, ok=False, attempts=0,
                              error=f"Not sent: {MAX_WAITING} deliveries were already waiting for this receiver."))
        _keep_last(s, hook_id)
        s.commit()


def _try(hook_id: int, payload: dict, attempt: int, delivery_id: int | None, last: bool,
         made: Any = None) -> tuple[WebhookDelivery | None, bool]:
    """Try once and record it (a retry updates the same row). Returns (the delivery, whether to try again)."""
    from .db import engine
    with Session(engine) as s:
        hook = s.get(Webhook, hook_id)
        if hook is None or (made is not None and hook.created_at != made) or (attempt > 1 and not hook.active):
            # deleted or paused since; or deleted and its number given to a new webhook, which mustn't get the old one's
            return None, False
        raw = json.dumps(body_for(hook, payload), separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": f"Benchtop/{__version__}",
                   "X-Benchtop-Event": payload["event"], "X-Benchtop-Delivery": str(int(time.time() * 1000))}
        if hook.secret:
            headers["X-Benchtop-Signature"] = sign_body(hook.secret, raw)
        url, made = hook.url, hook.created_at
    t0 = time.perf_counter()
    code, error, retry = _attempt(url, raw, headers)
    ms = int((time.perf_counter() - t0) * 1000)
    ok = code is not None and 200 <= code < 300
    if code is not None and not ok:
        error = _error_for(code)
    # success, or the receiver turned it down (other than "busy"), or an address that's not allowed: trying again won't help
    again = retry and not ok and not last and not (code is not None and 300 <= code < 500 and code != 429)
    with Session(engine) as s:
        hook = s.get(Webhook, hook_id)
        if hook is None or hook.created_at != made:  # deleted while this try was under way
            return None, False
        d = s.get(WebhookDelivery, delivery_id) if delivery_id else None
        if d is None:
            d = WebhookDelivery(webhook_id=hook_id, event=payload["event"])
        d.attempts, d.status_code, d.ok, d.duration_ms = attempt, code, ok, ms
        d.error = "" if ok else error + (f" Trying again in {_wait(RETRY_AFTER[attempt - 1])}." if again and not synchronous else "")
        s.add(d)
        _keep_last(s, hook_id)
        s.commit()
        s.refresh(d)
        return d, again


def _wait(seconds: int) -> str:
    return f"{seconds} seconds" if seconds < 90 else f"{seconds // 60} minutes"


def deliver(hook_id: int, payload: dict, retries: int = 3) -> WebhookDelivery:
    """Every try at once, without waiting between them: the tests, and Send test (one try, in its own request)."""
    d: WebhookDelivery | None = None
    for attempt in range(1, retries + 1):
        d, again = _try(hook_id, payload, attempt, d.id if d else None, last=attempt == retries)
        if d is None:
            raise ValueError("No such webhook")
        if not again:
            break
    return d


def failing(rows: list[WebhookDelivery]) -> dict[str, Any] | None:
    """For the Admin page: how long a webhook has been failing, from its recent deliveries (newest first).
    None when the latest delivery arrived (or there are none)."""
    if not rows or rows[0].ok:
        return None
    streak = next((i for i, d in enumerate(rows) if d.ok), len(rows))
    last_ok = rows[streak] if streak < len(rows) else None
    return {"count": streak, "more": last_ok is None and len(rows) >= 50, "since": rows[streak - 1].created_at.isoformat() + "Z",
            "last_ok_at": last_ok.created_at.isoformat() + "Z" if last_ok else None, "error": rows[0].error}
