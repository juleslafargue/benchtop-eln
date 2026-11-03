"""Sample codes: each person's prefix, and the codes handed to experiments (see app/samples.py)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from .. import audit, samples
from ..access import access_for
from ..models import Experiment, SampleCode, User, utcnow
from ..security import DB, CurrentUser, Writer
from ..services import get_or_404, read_experiment, require_editable

router = APIRouter(prefix="/api", tags=["samples"])


class PrefixIn(BaseModel):
    prefix: Optional[str] = None


@router.put("/auth/me/sample-prefix")
def set_prefix(body: PrefixIn, user: CurrentUser, session: DB):
    """Choose (or clear) the start of your sample codes. Codes already handed out keep theirs."""
    from .auth import user_out
    audit._take_write_lock(session)  # check-then-save as one step: two people can't claim the same initials at once
    if body.prefix in (None, ""):
        new = None
    else:
        try:
            new = samples.clean_prefix(body.prefix)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        other = session.exec(select(User).where(User.sample_prefix == new, User.id != user.id)).first()
        theirs = session.exec(select(SampleCode).where(SampleCode.prefix == new, SampleCode.user_id != user.id)).first()
        if other or theirs:
            who = other or session.get(User, theirs.user_id)
            raise HTTPException(409, f"{new} is already {who.full_name or who.username}'s. Add another letter, like a middle initial.")
    if new != user.sample_prefix:
        audit.record(session, user, "sample_prefix", "user", user.id, before=user.sample_prefix, after=new)
        user.sample_prefix = new
        session.add(user)
        session.commit()
    return user_out(user)


@router.get("/samples/lookup")
def lookup_sample(code: str, user: CurrentUser, session: DB):
    """The experiment a sample code was handed to, for the ⌘K box (which only has the latest records' codes).
    A code on a record this person can't open answers exactly as a code never handed out: 404."""
    from ..services import experiment_summary
    found = samples.lookup(session, code[:40])
    exp = session.get(Experiment, found.code.experiment_id) if found and found.code else None
    if exp is None or not access_for(session, user).can_read(exp):
        raise HTTPException(404, "No experiment you can open has that sample code")
    return {"code": found.text, "experiment": experiment_summary(session, exp)}


class SampleIn(BaseModel):
    number: Optional[int] = None  # a number already written on a vial; the next free one if left out


@router.post("/experiments/{exp_id}/samples", status_code=201)
def new_sample(exp_id: int, body: SampleIn, user: Writer, session: DB):
    exp = read_experiment(session, user, exp_id)
    if exp.is_template:
        raise HTTPException(422, "Templates don't have samples")
    require_editable(exp, user)  # a draft this person can edit
    if not user.sample_prefix:
        raise HTTPException(409, "Choose the start of your sample codes (your initials) in Account first")
    audit._take_write_lock(session)  # two clicks at once get two different numbers
    p = user.sample_prefix
    sc = None
    if body.number is not None:
        if not 1 <= body.number <= 999_999:
            raise HTTPException(422, "Sample numbers go from 1 to 999999")
        sc = session.exec(select(SampleCode).where(SampleCode.prefix == p, SampleCode.number == body.number)).first()
        if sc and sc.retired_at is None:
            other = session.get(Experiment, sc.experiment_id)
            where = other.eid if other and access_for(session, user).can_read(other) else "another experiment"
            raise HTTPException(409, f"{samples.fmt(p, body.number)} already belongs to {where}")
        n = body.number
    else:
        n = samples.next_number(session, p)
    if sc is not None:  # a retired code, given again (to the right experiment this time)
        sc.experiment_id, sc.user_id, sc.retired_at = exp.id, user.id, None
    else:
        sc = SampleCode(prefix=p, number=n, user_id=user.id, experiment_id=exp.id)
    session.add(sc)
    session.flush()
    audit.record(session, user, "create", "sample_code", sc.id, experiment=exp.eid, code=samples.fmt(p, n))
    session.commit()
    return samples.out(session, sc)


@router.delete("/samples/{sid}")
def remove_sample(sid: int, user: Writer, session: DB):
    """Take a code off a draft (handed out by mistake, or to the wrong one). The number stays taken, so it's
    never handed out again; files carrying it go to its owner's Inbox, and typing the number on the right
    experiment gives it to that one instead."""
    sc = get_or_404(session, SampleCode, sid)
    exp = read_experiment(session, user, sc.experiment_id)
    if sc.retired_at is not None:
        raise HTTPException(404, f"Sample code {sid} not found")
    require_editable(exp, user)
    sc.retired_at = utcnow()
    session.add(sc)
    audit.record(session, user, "retire", "sample_code", sid, experiment=exp.eid, code=samples.fmt(sc.prefix, sc.number))
    session.commit()
    return {"ok": True}
