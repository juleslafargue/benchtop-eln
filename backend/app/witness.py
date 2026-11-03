"""Witness requests: the author picks who should witness a signed record; that person is notified (in the app
and by email), reminded while it waits, and the author hears back when it's witnessed or returned.

A request doesn't stop anyone else with access from witnessing: it tells one person and tracks it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, select

from . import mailer, sitesettings
from .models import Experiment, ExperimentStatus, Notification, Role, User, WitnessRequest, utcnow
from .services import user_brief

MANUAL_REMIND_GAP = timedelta(hours=1)


def _days(n: float) -> str:
    n = max(0, n)
    if n < 1:
        h = round(n * 24)
        return f"{h} hour{'s' if h != 1 else ''}"
    d = round(n)
    return f"{d} day{'s' if d != 1 else ''}"


def _link(exp: Experiment) -> str:
    return f"{mailer.base_url()}/experiments/{exp.id}"


def candidates(session: Session, exp: Experiment) -> list[dict[str, Any]]:
    """Who can be asked: active, allowed to sign (not read-only), not the author, and able to open the record."""
    from .access import access_for
    out = []
    for u in session.exec(select(User).where(User.active == True).order_by(User.full_name, User.username)):  # noqa: E712
        if u.id == exp.owner_id or u.role == Role.viewer:
            continue
        if access_for(session, u).can_read(exp):
            out.append({**user_brief(session, u.id), "has_email": bool((u.email or "").strip())})
    return out


def open_request(session: Session, exp_id: int) -> WitnessRequest | None:
    return session.exec(select(WitnessRequest).where(WitnessRequest.experiment_id == exp_id, WitnessRequest.status == "open")
                        .order_by(WitnessRequest.id.desc())).first()


def summary(session: Session, exp: Experiment) -> dict[str, Any] | None:
    r = open_request(session, exp.id) if exp.status == ExperimentStatus.signed else None
    if r is None:
        return None
    now = utcnow()
    return {"id": r.id, "witness": user_brief(session, r.witness_id), "requested_by": user_brief(session, r.requested_by),
            "asked_at": r.created_at.isoformat() + "Z", "due_at": r.due_at.isoformat() + "Z" if r.due_at else None,
            "overdue": bool(r.due_at and r.due_at < now), "reminded_at": r.reminded_at.isoformat() + "Z" if r.reminded_at else None,
            "can_remind_now": not r.reminded_at or now - r.reminded_at >= MANUAL_REMIND_GAP,
            "email": mailer.configured() and mailer.wants(session.get(User, r.witness_id), "witness")}


def _notify(session: Session, user_id: int, kind: str, actor: User | None, exp: Experiment, text: str) -> None:
    session.add(Notification(user_id=user_id, kind=kind, actor_id=actor.id if actor else None, experiment_id=exp.id, text=text))


def validate(session: Session, exp: Experiment, witness_id: int) -> User:
    w = session.get(User, witness_id)
    if w is None or not any(c["id"] == witness_id for c in candidates(session, exp)):
        raise HTTPException(422, "That person can't witness this record (they need access to it and a role that can sign)")
    return w


def lock(session: Session, exp: Experiment) -> None:
    """Take the database's write lock, then re-read the record, so two clicks (or two people) asking or
    reminding at the same moment are handled one after the other instead of both seeing "no open request"."""
    from .audit import _take_write_lock
    _take_write_lock(session)
    session.refresh(exp)
    if exp.status != ExperimentStatus.signed:
        raise HTTPException(409, f"{exp.eid} isn't waiting for a witness any more")


def request(session: Session, exp: Experiment, author: User, witness_id: int) -> WitnessRequest:
    """Ask a colleague to witness. Replaces any open request for this record."""
    w = validate(session, exp, witness_id)
    lock(session, exp)
    now = utcnow()
    for old in session.exec(select(WitnessRequest).where(WitnessRequest.experiment_id == exp.id, WitnessRequest.status == "open")):
        old.status, old.closed_at, old.outcome = "cancelled", now, "reassigned"
        session.add(old)
        if old.witness_id != witness_id:
            _notify(session, old.witness_id, "witness_cancelled", author, exp, f"no longer needs your witness signature on {exp.eid}")
    cfg = sitesettings.get("witness")
    r = WitnessRequest(experiment_id=exp.id, requested_by=author.id, witness_id=w.id, created_at=now,
                       due_at=now + timedelta(days=max(1, cfg["overdue_after_days"])))
    session.add(r)
    _notify(session, w.id, "witness_request", author, exp, f"asked you to witness {exp.eid}: {exp.title}")
    if mailer.wants(w, "witness"):
        mailer.queue_email(session, w.email, "witness_request", eid=exp.eid, title=exp.title, link=_link(exp),
                           author=author.full_name or author.username, due=r.due_at.strftime("%a %d %b"))
    return r


def close(session: Session, exp: Experiment, outcome: str, actor: User, reason: str = "") -> None:
    """The record left the "awaiting witness" state: close requests and tell the right people."""
    now = utcnow()
    asked = []
    for r in session.exec(select(WitnessRequest).where(WitnessRequest.experiment_id == exp.id, WitnessRequest.status == "open")):
        r.status, r.closed_at, r.outcome = ("done" if outcome in ("witnessed", "returned") else "cancelled"), now, outcome
        session.add(r)
        asked.append(r.witness_id)
    author = session.get(User, exp.owner_id)
    who = actor.full_name or actor.username
    if outcome in ("witnessed", "returned") and author and author.id != actor.id:
        kind = "witnessed" if outcome == "witnessed" else "returned"
        _notify(session, author.id, kind, actor, exp, f"witnessed {exp.eid}" if kind == "witnessed" else f"returned {exp.eid} to you: “{reason}”")
        if mailer.wants(author, "results"):
            mailer.queue_email(session, author.email, kind, eid=exp.eid, title=exp.title, link=_link(exp), witness=who, reason=reason)
    for uid in asked:  # the person asked, if someone else acted or the author pulled it back
        if uid != actor.id:
            what = {"witnessed": f"was witnessed by {who}", "returned": f"was returned by {who}",
                    "withdrawn": "was withdrawn by its author", "voided": "was voided"}.get(outcome, "no longer needs you")
            _notify(session, uid, "witness_cancelled", actor, exp, f"{exp.eid} {what}; your witness signature isn't needed")


def can_witness(u: User | None) -> bool:
    return bool(u and u.active and u.role != Role.viewer)


def cancel_for(session: Session, witness: User, actor: User | None) -> list[str]:
    """The person asked can't witness any more (made read-only, or their account disabled): their open requests are
    cancelled, so the records don't wait on them and others see them as nobody asked again (0.30.1: they stayed on
    the dashboard of someone who couldn't act on them). Each author is told; one audit event lists the records.
    Only the requests change: the signed records, and their signatures, are untouched."""
    now = utcnow()
    why = "their account is disabled" if not witness.active else "their account is read-only"
    eids: list[str] = []
    for r in session.exec(select(WitnessRequest).where(WitnessRequest.witness_id == witness.id, WitnessRequest.status == "open")).all():
        r.status, r.closed_at, r.outcome = "cancelled", now, "witness_unavailable"
        session.add(r)
        exp = session.get(Experiment, r.experiment_id)
        if exp:
            eids.append(exp.eid)
            _notify(session, r.requested_by, "witness_cancelled", witness, exp,
                    f"can't witness {exp.eid} any more ({why}): ask someone else")
    if eids:
        from . import audit
        audit.record(session, actor, "witness_requests_cancelled", "user", witness.id, eids=eids, reason=why)
    return eids


def remind(session: Session, r: WitnessRequest, manual_by: User | None = None) -> None:
    exp = session.get(Experiment, r.experiment_id)
    w = session.get(User, r.witness_id)
    author = session.get(User, exp.owner_id) if exp else None
    if not exp or not w:
        return
    now = utcnow()
    waiting = _days((now - r.created_at).total_seconds() / 86400)
    overdue = bool(r.due_at and r.due_at < now)
    _notify(session, w.id, "witness_reminder", manual_by or author, exp,
            f"{exp.eid} has been waiting for your witness signature for {waiting}{' (overdue)' if overdue else ''}")
    if mailer.wants(w, "witness"):
        mailer.queue_email(session, w.email, "witness_reminder", eid=exp.eid, title=exp.title, link=_link(exp),
                           author=(author.full_name or author.username) if author else "", waiting=waiting, overdue=overdue)
    r.reminded_at = now
    session.add(r)


def send_due_reminders() -> int:
    """Housekeeping (hourly): remind witnesses every N days while a request waits."""
    from .db import engine
    gap = timedelta(days=max(1, sitesettings.get("witness")["remind_after_days"]))
    now = utcnow()
    n = 0
    with Session(engine) as s:
        # requests to someone who can no longer witness (changed before 0.30.1, which cancels them at once)
        stuck = {r.witness_id for r in s.exec(select(WitnessRequest).where(WitnessRequest.status == "open")).all()}
        for uid in stuck:
            w = s.get(User, uid)
            if w is not None and not can_witness(w):
                cancel_for(s, w, None)
        for r in s.exec(select(WitnessRequest).where(WitnessRequest.status == "open")).all():
            exp = s.get(Experiment, r.experiment_id)
            if not exp or exp.status != ExperimentStatus.signed:
                r.status, r.closed_at, r.outcome = "cancelled", now, "stale"
                s.add(r)
                continue
            if now - (r.reminded_at or r.created_at) >= gap:
                remind(s, r)
                n += 1
        s.commit()
    return n
