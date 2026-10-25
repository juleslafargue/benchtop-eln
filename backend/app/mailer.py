"""Outgoing email: witness requests, reminders and outcomes.

Configured in Admin → Email (or BENCHTOP_SMTP_* variables). Messages are queued in the database and sent by a
background thread, so a slow mail server never holds up a request. If the server can't be reached (or says "try
later") the message waits and is retried with growing gaps for about a day, including after Benchtop restarts;
a message the server rejects outright (bad address) fails at once. Everything shows in the admin's log.
Without an email server everything still works in the app (notifications), just without the emails.
"""
from __future__ import annotations

import html
import json
import logging
import queue
import smtplib
import ssl
import threading
import time
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Callable

from sqlmodel import Session, select

from . import sitesettings
from .config import settings
from .models import EmailLog, User, utcnow

log = logging.getLogger("benchtop.mail")
synchronous = False                              # tests: send inline
transport: Callable[[EmailMessage, dict], None] | None = None  # tests: capture instead of SMTP
_q: "queue.Queue[int]" = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()
KEEP = 200
# gaps between attempts while the mail server can't be reached: about a day in all, then the message fails
BACKOFF = [timedelta(minutes=m) for m in (1, 5, 15, 60, 180, 360, 720)]
SWEEP_SECONDS = 30
_down_until = 0.0  # after a connection problem, new messages wait for the next sweep instead of each trying
_unreachable = False  # the last failure was not reaching the server at all (offline, server down)


def configured() -> bool:
    if settings.demo:  # never email the demo lab's fictional people
        return False
    cfg = sitesettings.get("email")
    return bool(cfg["host"] and cfg["from_address"])


def base_url() -> str:
    """The address people open Benchtop at, for links in emails."""
    cfg = sitesettings.get("email")
    return (cfg["base_url"] or settings.public_url or settings.lan_url or f"http://localhost:{settings.port}").rstrip("/")


def wants(user: User | None, topic: str) -> bool:
    """Does this person get email for this topic? ("witness" requests/reminders, "results" of their records)."""
    if not user or not user.active or not (user.email or "").strip():
        return False
    prefs = ((user.preferences or {}).get("email_notify") or {})
    return prefs.get(topic, True) is not False


# ------------------------------------------------------------------ messages

def _layout(title: str, lines: list[str], link: str | None, button: str = "Open in Benchtop") -> str:
    body = "".join(f'<p style="margin:0 0 12px">{line}</p>' for line in lines)
    btn = (f'<p style="margin:20px 0"><a href="{html.escape(link)}" style="background:#b8336a;color:#fff;text-decoration:none;'
           f'padding:9px 16px;border-radius:7px;font-weight:600;display:inline-block">{html.escape(button)}</a></p>') if link else ""
    return (f'<div style="font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#17212c;max-width:560px">'
            f'<p style="margin:0 0 16px;font-weight:700;font-size:17px">{html.escape(title)}</p>{body}{btn}'
            f'<p style="margin:24px 0 0;color:#6f7d8b;font-size:12.5px">Sent by Benchtop. You can change which emails you get in '
            f'Account settings.</p></div>')


def render(kind: str, **c: Any) -> tuple[str, str, str]:
    """(subject, plain text, html) for one kind of email."""
    e = lambda v: html.escape(str(v))  # noqa: E731
    rec = f"{c.get('eid', '')} {c.get('title', '')}".strip()
    link = c.get("link")
    if kind == "witness_request":
        subject = f"Please witness {c['eid']}: {c['title']}"
        lines = [f"{e(c['author'])} signed <b>{e(rec)}</b> and asked you to witness it.",
                 f"Please review it by {e(c['due'])}." if c.get("due") else ""]
    elif kind == "witness_reminder":
        subject = f"Reminder: {c['eid']} is waiting for your witness signature"
        lines = [f"<b>{e(rec)}</b> by {e(c['author'])} has been waiting for your witness signature for {e(c['waiting'])}.",
                 "It's overdue." if c.get("overdue") else ""]
    elif kind == "witnessed":
        subject = f"{c['eid']} was witnessed"
        lines = [f"{e(c['witness'])} witnessed <b>{e(rec)}</b>. The record is now final."]
    elif kind == "returned":
        subject = f"{c['eid']} was returned to you"
        lines = [f"{e(c['witness'])} returned <b>{e(rec)}</b> to you for changes.", f"Reason: “{e(c.get('reason', ''))}”"]
    elif kind == "data_check":
        subject = "Benchtop's daily data check found a problem"
        lines = ["Benchtop checks its data every day. Today's check found:"] + [e(p) for p in c.get("problems", [])] + [
            "Admin → Backups shows the details. You'll get one email per new problem, not one a day."]
        link = c.get("link")
    elif kind == "disk_space":
        subject = "Benchtop's disk is nearly full" if c.get("full") else "Benchtop's disk is running low on space"
        lines = [e(p) for p in c.get("problems", [])] + [
            "Admin → Backups shows each disk's free space. You'll get one more email if it gets nearly full, not one a day."
            if not c.get("full") else "Admin → Backups shows each disk's free space."]
        link = c.get("link")
    elif kind == "test":
        subject = "Benchtop test email"
        lines = ["This is a test from Benchtop's email settings. If you can read this, email is working."]
        link = base_url()
    else:
        raise ValueError(kind)
    lines = [ln for ln in lines if ln]
    subject = " ".join(subject.split())[:250]  # a line break in a record's title can't go in an email header
    plain = "\n\n".join(html.unescape(ln.replace("<b>", "").replace("</b>", "")) for ln in lines)
    if link:
        plain += f"\n\nOpen it: {link}"
    plain += "\n\n—\nSent by Benchtop. You can change which emails you get in Account settings."
    return subject, plain, _layout(subject, lines, link)


# ------------------------------------------------------------------ sending

def queue_email(session: Session, to: str, kind: str, **ctx: Any) -> None:
    """Queue an email; it goes out after the current transaction commits (nothing is sent if it rolls back)."""
    if not configured() or not to:
        return
    subject, _, _ = render(kind, **ctx)
    row = EmailLog(to=to, subject=subject, kind=kind, context=json.dumps(ctx, default=str), attempts=0)
    session.add(row)
    session.flush()
    session.info.setdefault("benchtop_mail", []).append(row.id)


def _build(to: str, kind: str, ctx: dict) -> EmailMessage:
    cfg = sitesettings.get("email")
    subject, plain, rich = render(kind, **ctx)
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = formataddr((cfg["from_name"] or "Benchtop", cfg["from_address"]))
    msg["Message-ID"] = make_msgid(domain=(cfg["from_address"].split("@")[-1] or "benchtop.local"))
    msg.set_content(plain)
    msg.add_alternative(rich, subtype="html")
    return msg


def tls_context() -> ssl.SSLContext:
    """Trusted certificates for the encrypted connection. Python from python.org on a Mac has none of its own
    until "Install Certificates.command" is run, so also load the certifi bundle (installed with httpx)."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall back to the system's certificates
        pass
    return ctx


def _smtp_send(msg: EmailMessage, cfg: dict) -> None:
    if transport is not None:
        transport(msg, cfg)
        return
    ctx = tls_context()
    if cfg["security"] == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg["host"], cfg["port"] or 465, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"] or 587, timeout=20)
    with server:
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        server.send_message(msg)


def _explain(exc: Exception) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ("Couldn't confirm the email server's identity (its security certificate didn't check out). "
                "Check the server name; on a Mac with Python from python.org, running \"Install Certificates.command\" "
                "in Applications → Python also fixes this")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The email server refused the username or password (for Gmail or Outlook, use an app password)"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        (addr, (code, msg)), = list(exc.recipients.items())[:1] or [("", (0, b""))]
        return f"The email server refused the address {addr}: {code} {_text(msg)}".strip()
    if isinstance(exc, smtplib.SMTPResponseException):
        later = " (it asked to try again later)" if 400 <= exc.smtp_code < 500 else ""
        return f"The email server answered {exc.smtp_code} {_text(exc.smtp_error)}{later}".strip()
    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)) or \
            (isinstance(exc, OSError) and not isinstance(exc, smtplib.SMTPException)):
        return f"Couldn't reach the email server ({exc.__class__.__name__}: {exc})"
    return str(exc) or exc.__class__.__name__


def _text(v: Any) -> str:
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


def _permanent(exc: Exception) -> bool:
    """The server rejected this message for good (bad address, refused content): retrying won't help."""
    if isinstance(exc, ValueError):  # the message itself can't be built
        return True
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return all(code >= 500 for code, _ in exc.recipients.values())
    if isinstance(exc, smtplib.SMTPAuthenticationError):  # fixable in Admin → Email; keep the message until then
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code >= 500 and exc.smtp_code not in (502, 503, 504)
    return False


def deliver(log_id: int) -> bool:
    """One attempt at one queued message. Returns False if the mail server couldn't take it right now."""
    from .db import engine
    global _unreachable
    with Session(engine) as s:
        row = s.get(EmailLog, log_id)
        if row is None or row.status != "queued":
            return True
        if not configured():
            return False
        try:
            ctx = json.loads(row.context or "{}")
            _smtp_send(_build(row.to, row.kind, ctx), sitesettings.get("email"))
            row.status, row.sent_at, row.error, row.next_try = "sent", utcnow(), "", None
            ok = True
        except Exception as exc:  # noqa: BLE001 - recorded for the admin
            _unreachable = _connection_problem(exc)
            row.attempts = (row.attempts or 0) + 1
            row.error = _explain(exc)
            if _permanent(exc) or not row.context or row.attempts > len(BACKOFF):
                row.status, row.next_try = "failed", None
            else:
                row.next_try = utcnow() + BACKOFF[row.attempts - 1]
            log.warning("Email to %s %s: %s", row.to, "failed" if row.status == "failed" else "not sent yet", row.error)
            ok = row.status == "failed"  # a permanent failure says nothing about the server being down
        s.add(row)
        old = s.exec(select(EmailLog).where(EmailLog.status != "queued").order_by(EmailLog.id.desc()).offset(KEEP)).all()
        for o in old:
            s.delete(o)
        s.commit()
        return ok


def _connection_problem(exc: Exception) -> bool:
    return isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)) or \
        (isinstance(exc, OSError) and not isinstance(exc, smtplib.SMTPException))


def _probe() -> bool:
    """Can we reach the mail server now? (Connects and says hello; doesn't log in or send.)"""
    if transport is not None:
        return True
    cfg = sitesettings.get("email")
    try:
        if cfg["security"] == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(cfg["host"], cfg["port"] or 465, timeout=10, context=tls_context())
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"] or 587, timeout=10)
        with server:
            server.noop()
        return True
    except Exception:  # noqa: BLE001
        return False


def sweep() -> int:
    """Send queued messages that are due (retries, and anything left over from before a restart).

    Stops at the first one the server can't take, so an outage costs one attempt per sweep, not one per
    message. Once mail goes through again (or, after a connection problem, the server answers again), the
    messages waiting out a retry gap are sent straight away instead of hours later."""
    from .db import engine
    global _down_until, _unreachable
    if not configured():
        return 0
    now = utcnow()
    with Session(engine) as s:
        for old in s.exec(select(EmailLog).where(EmailLog.status == "queued", EmailLog.context == None,  # noqa: E711
                                                 EmailLog.created_at < now - timedelta(minutes=10))):
            old.status, old.error = "failed", "Benchtop restarted before it could send this"  # queued by an older version
            s.add(old)
        s.commit()
        queued = s.exec(select(EmailLog.id, EmailLog.next_try).where(EmailLog.status == "queued", EmailLog.context.is_not(None))
                        .order_by(EmailLog.id)).all()
    due = [i for i, nt in queued if nt is None or nt <= now]
    waiting = [i for i, nt in queued if nt is not None and nt > now]
    sent = 0
    for log_id in due:
        if not deliver(log_id):
            _down_until = time.monotonic() + SWEEP_SECONDS
            return sent
        sent += 1
    _down_until = 0.0
    if not waiting or not (sent or (_unreachable and _probe())):
        return sent
    _unreachable = False
    for log_id in waiting:
        if not deliver(log_id):
            _down_until = time.monotonic() + SWEEP_SECONDS
            break
        sent += 1
    return sent


def _run() -> None:
    global _down_until
    last_sweep = 0.0
    while True:
        try:
            log_id = _q.get(timeout=SWEEP_SECONDS)
        except queue.Empty:
            log_id = None
        try:
            if log_id is not None and time.monotonic() >= _down_until and not deliver(log_id):
                _down_until = time.monotonic() + SWEEP_SECONDS
            if time.monotonic() - last_sweep >= SWEEP_SECONDS:
                last_sweep = time.monotonic()
                sweep()
        except Exception:  # noqa: BLE001
            log.exception("Email worker hit a problem")


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="benchtop-mail", daemon=True)
            _worker.start()


def start() -> None:
    """At startup and hourly: make sure messages left queued (e.g. from before a restart) get sent."""
    if configured() and not synchronous:
        _ensure_worker()


def flush_after_commit(session: Session) -> None:
    for log_id in session.info.pop("benchtop_mail", None) or []:
        if synchronous:
            deliver(log_id)
        else:
            _q.put(log_id)
            _ensure_worker()


def drop_on_rollback(session: Session) -> None:
    session.info.pop("benchtop_mail", None)


def send_test(to: str) -> str:
    """Send a test email now; returns "" or what went wrong."""
    if not configured():
        return "Fill in the email server and the From address first"
    try:
        _smtp_send(_build(to, "test", {}), sitesettings.get("email"))
        return ""
    except Exception as exc:  # noqa: BLE001
        return _explain(exc)


def _install_hooks() -> None:
    from sqlalchemy import event
    event.listen(Session, "after_commit", flush_after_commit)
    event.listen(Session, "after_soft_rollback", lambda s, prev: drop_on_rollback(s))


_install_hooks()
