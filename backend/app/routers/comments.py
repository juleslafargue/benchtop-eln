"""Comments on experiments and sections, @mentions, and each person's notifications.

Comments are discussion about a record, not part of it: they can be added to signed and
witnessed experiments, they don't change the signed content hash, and every add/edit/delete/
resolve is written to the audit trail.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import audit
from ..access import access_for
from ..models import Comment, Entry, Experiment, Notification, Role, User, utcnow
from ..security import DB, CurrentUser
from ..services import get_or_404, read_experiment, user_brief

router = APIRouter(prefix="/api", tags=["comments & notifications"])

MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_.\-]{2,40})")
MAX_LEN = 5000


def mentions(session, body: str) -> list[User]:
    """The people @mentioned. "Thanks @hweiss." means hweiss, but a username may itself end in "." or "-" ("jo."):
    the longest username that the text starts with is the one meant (0.30.1: the last character was always cut, so
    "jo." was never told)."""
    typed = {m.group(1).lower() for m in MENTION.finditer(body)}
    if not typed:
        return []
    users = {u.username.lower(): u for u in session.exec(select(User).where(User.active == True))}  # noqa: E712
    found: dict[int, User] = {}
    for t in typed:
        while t and t not in users and t[-1] in ".-":
            t = t[:-1]
        if t in users:
            found[users[t].id] = users[t]
    return [found[k] for k in sorted(found)]


def comment_out(session, c: Comment, me: User) -> dict[str, Any]:
    gone = c.deleted
    return {
        "id": c.id, "experiment_id": c.experiment_id, "entry_id": c.entry_id, "parent_id": c.parent_id,
        "user": user_brief(session, c.user_id), "body": "" if gone else c.body, "deleted": gone,
        "created_at": c.created_at.isoformat() + "Z", "edited_at": c.edited_at.isoformat() + "Z" if c.edited_at else None,
        "resolved_at": c.resolved_at.isoformat() + "Z" if c.resolved_at else None,
        "resolved_by": user_brief(session, c.resolved_by) if c.resolved_by else None,
        "mine": c.user_id == me.id,
    }


def _notify(session, uid: int, kind: str, actor: User, exp: Experiment, c: Comment, text: str, done: set[int]) -> None:
    if uid == actor.id or uid in done:
        return
    done.add(uid)
    session.add(Notification(user_id=uid, kind=kind, actor_id=actor.id, experiment_id=exp.id, comment_id=c.id, text=text))


def _excerpt(body: str, n: int = 90) -> str:
    t = re.sub(r"\s+", " ", body).strip()
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


@router.get("/experiments/{exp_id}/comments")
def list_comments(exp_id: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    cs = session.exec(select(Comment).where(Comment.experiment_id == exp.id).order_by(Comment.id)).all()
    has_replies = {c.parent_id for c in cs if c.parent_id and not c.deleted}
    # a deleted comment is only shown (as a placeholder) when it still has replies under it
    return [comment_out(session, c, user) for c in cs if not c.deleted or c.id in has_replies]


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_LEN)
    entry_id: int | None = None
    parent_id: int | None = None


@router.post("/experiments/{exp_id}/comments", status_code=201)
def add_comment(exp_id: int, body: CommentIn, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    text = body.body.strip()
    if not text:
        raise HTTPException(422, "Write something first")
    entry_id = body.entry_id
    parent = None
    if body.parent_id:
        parent = get_or_404(session, Comment, body.parent_id)
        if parent.experiment_id != exp.id:
            raise HTTPException(422, "That comment belongs to another experiment")
        while parent.parent_id:  # one level of threading: replies attach to the thread's first comment
            parent = get_or_404(session, Comment, parent.parent_id)
        entry_id = parent.entry_id
    if entry_id is not None:
        en = get_or_404(session, Entry, entry_id)
        if en.experiment_id != exp.id:
            raise HTTPException(422, "That section belongs to another experiment")
    c = Comment(experiment_id=exp.id, entry_id=entry_id, parent_id=parent.id if parent else None, user_id=user.id, body=text)
    session.add(c)
    session.flush()
    done: set[int] = set()
    unseen = []
    for u in mentions(session, text):
        if access_for(session, u).can_read(exp):
            _notify(session, u.id, "mention", user, exp, c, f"mentioned you on {exp.eid}: “{_excerpt(text)}”", done)
        elif u.id != user.id:
            unseen.append(user_brief(session, u.id))
    if parent:
        _notify(session, parent.user_id, "reply", user, exp, c, f"replied on {exp.eid}: “{_excerpt(text)}”", done)
        for other in session.exec(select(Comment).where(Comment.parent_id == parent.id, Comment.deleted == False)):  # noqa: E712
            _notify(session, other.user_id, "reply", user, exp, c, f"replied on {exp.eid}: “{_excerpt(text)}”", done)
    _notify(session, exp.owner_id, "comment", user, exp, c, f"commented on your {exp.eid}: “{_excerpt(text)}”", done)
    audit.record(session, user, "comment", "experiment", exp.id, eid=exp.eid, comment_id=c.id, entry_id=entry_id,
                 parent_id=c.parent_id, body=text)
    session.commit()
    out = comment_out(session, c, user)
    out["notified"] = len(done)
    out["unseen_mentions"] = unseen  # @mentioned people who can't open this experiment (the UI offers to share)
    return out


class CommentPatch(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=MAX_LEN)
    resolved: bool | None = None


@router.patch("/comments/{cid}")
def edit_comment(cid: int, body: CommentPatch, user: CurrentUser, session: DB):
    c = get_or_404(session, Comment, cid)
    exp = read_experiment(session, user, c.experiment_id)
    if c.deleted:
        raise HTTPException(409, "That comment was deleted")
    if body.body is not None:
        if c.user_id != user.id:
            raise HTTPException(403, "You can only edit your own comments")
        before = c.body
        c.body = body.body.strip()
        c.edited_at = utcnow()
        audit.record(session, user, "comment_edit", "experiment", exp.id, eid=exp.eid, comment_id=c.id, before=before, after=c.body)
    if body.resolved is not None:
        if c.parent_id:
            raise HTTPException(422, "Resolve the thread, not a reply")
        if user.id not in (c.user_id, exp.owner_id) and user.role != Role.admin and not access_for(session, user).can_edit(exp):
            raise HTTPException(403, "Only the experiment's owner or the comment's author can resolve it")
        c.resolved_at, c.resolved_by = (utcnow(), user.id) if body.resolved else (None, None)
        audit.record(session, user, "comment_resolve" if body.resolved else "comment_reopen", "experiment", exp.id,
                     eid=exp.eid, comment_id=c.id)
    session.add(c)
    session.commit()
    return comment_out(session, c, user)


@router.delete("/comments/{cid}")
def delete_comment(cid: int, user: CurrentUser, session: DB):
    c = get_or_404(session, Comment, cid)
    exp = read_experiment(session, user, c.experiment_id)
    if c.user_id != user.id and user.role != Role.admin:
        raise HTTPException(403, "You can only delete your own comments")
    c.deleted = True
    session.add(c)
    audit.record(session, user, "comment_delete", "experiment", exp.id, eid=exp.eid, comment_id=c.id, body=c.body)
    session.commit()
    return {"ok": True}


# ------------------------------------------------------------------ notifications

def notification_out(session, n: Notification) -> dict[str, Any]:
    exp = session.get(Experiment, n.experiment_id) if n.experiment_id else None
    c = session.get(Comment, n.comment_id) if n.comment_id else None
    return {"id": n.id, "kind": n.kind, "text": n.text, "actor": user_brief(session, n.actor_id) if n.actor_id else None,
            "experiment": {"id": exp.id, "eid": exp.eid, "title": exp.title} if exp else None,
            "comment_id": n.comment_id, "entry_id": c.entry_id if c else None,
            "created_at": n.created_at.isoformat() + "Z", "read": n.read_at is not None}


@router.get("/notifications")
def list_notifications(user: CurrentUser, session: DB, limit: int = 30, unread: bool = False):
    q = select(Notification).where(Notification.user_id == user.id)
    if unread:
        q = q.where(Notification.read_at == None)  # noqa: E711
    items = session.exec(q.order_by(Notification.id.desc()).limit(max(1, min(limit, 200)))).all()
    acc = access_for(session, user)
    out = []
    for n in items:
        exp = session.get(Experiment, n.experiment_id) if n.experiment_id else None
        if exp is not None and not acc.can_read(exp):  # access was taken away since
            continue
        out.append(notification_out(session, n))
    count = len(session.exec(select(Notification.id).where(Notification.user_id == user.id, Notification.read_at == None)).all())  # noqa: E711
    return {"items": out, "unread": count}


class ReadIn(BaseModel):
    ids: list[int] = []
    all: bool = False
    experiment_id: int | None = None


@router.post("/notifications/read")
def mark_read(body: ReadIn, user: CurrentUser, session: DB):
    q = select(Notification).where(Notification.user_id == user.id, Notification.read_at == None)  # noqa: E711
    if not body.all:
        if body.experiment_id is not None:
            q = q.where(Notification.experiment_id == body.experiment_id)
        elif body.ids:
            q = q.where(Notification.id.in_(body.ids))
        else:
            return {"marked": 0}
    now = utcnow()
    n = 0
    for x in session.exec(q):
        x.read_at = now
        session.add(x)
        n += 1
    session.commit()
    return {"marked": n}
