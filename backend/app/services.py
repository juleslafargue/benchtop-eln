"""Shared domain logic: access checks, serialization, content hashing for signatures."""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import HTTPException, status
from sqlmodel import Session, select

from .audit import canonical
from .models import (
    Attachment, Entry, Experiment, ExperimentStatus, Notebook, Role, Signature, User,
)


def get_or_404(session: Session, model, obj_id: int):
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model.__name__} {obj_id} not found")
    return obj


def require_editable(exp: Experiment, user: User, claim: bool = True) -> None:
    """The record may be edited by this person; with `claim`, also take it for this edit (see _claim_open).
    claim=False only checks, without holding the database: for work done before the edit (storing an upload)."""
    if exp.status != ExperimentStatus.open:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{exp.eid} is {exp.status.value}; signed records are locked",
        )
    if exp.owner_id != user.id and user.role != Role.admin and not exp.is_template:
        from sqlalchemy.orm import object_session

        from .access import access_for
        sess = object_session(exp)
        if sess is None or not access_for(sess, user).can_edit(exp):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the experiment owner, or someone it was shared with for editing, can edit it")
    if claim:
        _claim_open(exp)


def _claim_open(exp: Experiment) -> None:
    """Take the record for this edit, atomically, only if it is still a draft.

    The status check above and the write that follows are separate steps; without this, a save that
    arrives while someone signs could land after the signature, and two saves of the same version
    could both succeed. The no-op UPDATE makes the database order edits to one experiment, and fails
    if the record was signed in the meantime."""
    from sqlalchemy import update
    from sqlalchemy.orm import object_session
    sess = object_session(exp)
    if sess is None or exp.id is None:
        return
    res = sess.execute(update(Experiment).where(Experiment.id == exp.id, Experiment.status == ExperimentStatus.open)
                       .values(status=ExperimentStatus.open).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        sess.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"{exp.eid} was just signed; signed records are locked")


def read_experiment(session: Session, user: User, exp_id: int) -> Experiment:
    """The experiment, if this user may read it (404 otherwise, so hidden records stay hidden)."""
    from .access import require_read
    exp = get_or_404(session, Experiment, exp_id)
    require_read(session, user, exp)
    return exp


def user_brief(session: Session, uid: int | None) -> dict[str, Any] | None:
    if uid is None:
        return None
    u = session.get(User, uid)
    return {"id": u.id, "username": u.username, "full_name": u.full_name} if u else None


def entry_out(e: Entry) -> dict[str, Any]:
    return {
        "id": e.id, "experiment_id": e.experiment_id, "kind": e.kind.value, "title": e.title,
        "position": e.position, "content": e.content, "version": e.version,
        "updated_by": e.updated_by, "updated_at": e.updated_at.isoformat() + "Z",
    }


def live_entries(session: Session, exp_id: int) -> list[Entry]:
    return list(session.exec(
        select(Entry).where(Entry.experiment_id == exp_id, Entry.deleted == False)  # noqa: E712
        .order_by(Entry.position, Entry.id)
    ))


def experiment_summary(session: Session, exp: Experiment) -> dict[str, Any]:
    nb = session.get(Notebook, exp.notebook_id) if exp.notebook_id else None
    return {
        "id": exp.id, "eid": exp.eid, "title": exp.title, "description": exp.description,
        "status": exp.status.value, "notebook_id": exp.notebook_id,
        "notebook_name": nb.name if nb else None, "is_template": exp.is_template,
        "template_id": exp.template_id, "tags": exp.tags or [],
        "owner": user_brief(session, exp.owner_id),
        "created_at": exp.created_at.isoformat() + "Z", "updated_at": exp.updated_at.isoformat() + "Z",
    }


def snapshot(session: Session, exp: Experiment) -> dict[str, Any]:
    """Deterministic representation of everything a signature attests to."""
    atts = session.exec(
        select(Attachment).where(Attachment.experiment_id == exp.id, Attachment.withdrawn_at == None)  # noqa: E711
        .order_by(Attachment.id)  # files sent back with "Not mine" (only possible on a draft) aren't part of the record
    )
    return {
        "eid": exp.eid,
        "title": exp.title,
        "description": exp.description,
        "owner_id": exp.owner_id,
        "entries": [
            {"id": e.id, "kind": e.kind.value, "title": e.title, "version": e.version,
             "content": e.content}
            for e in live_entries(session, exp.id)
        ],
        "attachments": [{"id": a.id, "filename": a.filename, "sha256": a.sha256} for a in atts],
    }


def content_hash(session: Session, exp: Experiment) -> str:
    return hashlib.sha256(canonical(snapshot(session, exp)).encode()).hexdigest()


def signatures_out(session: Session, exp_id: int) -> list[dict[str, Any]]:
    from . import timestamps
    from .models import SignatureTimestamp
    sigs = list(session.exec(select(Signature).where(Signature.experiment_id == exp_id).order_by(Signature.id)))
    stamps = {t.signature_id: t for t in session.exec(select(SignatureTimestamp).where(SignatureTimestamp.signature_id.in_([s.id for s in sigs])))} if sigs else {}
    return [
        {"id": s.id, "meaning": s.meaning, "comment": s.comment, "content_hash": s.content_hash,
         "signed_at": s.signed_at.isoformat() + "Z", "user": user_brief(session, s.user_id),
         # printed name as it was when signed; falls back to the current name for pre-0.4 signatures
         "signer_name": s.signer_name or (user_brief(session, s.user_id) or {}).get("full_name") or "",
         "signer_username": s.signer_username,
         "timestamp": timestamps.out(stamps.get(s.id), s.signed_at)}  # trusted timestamp (0.14), if any
        for s in sigs
    ]
