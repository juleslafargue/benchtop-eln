"""Research groups, membership, and sharing single experiments with people."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import audit
from ..access import access_for
from ..models import Experiment, ExperimentShare, GroupMember, LabGroup, Notebook, Notification, Role, User
from ..security import DB, Admin, CurrentUser
from ..patching import cleaned
from ..services import get_or_404, read_experiment, user_brief

router = APIRouter(prefix="/api", tags=["groups & sharing"])


def group_out(session, g: LabGroup, me: User) -> dict[str, Any]:
    members = session.exec(select(GroupMember).where(GroupMember.group_id == g.id)).all()
    nbs = session.exec(select(Notebook).where(Notebook.group_id == g.id, Notebook.archived == False)).all()  # noqa: E712
    mine = next((m.role for m in members if m.user_id == me.id), None)
    return {
        "id": g.id, "name": g.name, "description": g.description, "created_at": g.created_at.isoformat() + "Z",
        "members": sorted(({**(user_brief(session, m.user_id) or {}), "role": m.role} for m in members),
                          key=lambda m: (m["role"] != "lead", (m.get("full_name") or m.get("username") or "").lower())),
        "notebook_count": len(nbs), "my_role": mine,
        "can_manage": me.role == Role.admin or mine == "lead",
    }


def _manage(session, user: User, g: LabGroup) -> None:
    if user.role != Role.admin and access_for(session, user).groups.get(g.id) != "lead":
        raise HTTPException(403, "Only an admin or a lead of this group can change it")


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


@router.get("/groups")
def list_groups(user: CurrentUser, session: DB):
    return [group_out(session, g, user) for g in session.exec(select(LabGroup).order_by(LabGroup.name))]


@router.post("/groups", status_code=201)
def create_group(body: GroupIn, user: Admin, session: DB):
    name = body.name.strip()
    if session.exec(select(LabGroup).where(LabGroup.name == name)).first():
        raise HTTPException(409, f"There is already a group called {name}")
    g = LabGroup(name=name, description=body.description.strip())
    session.add(g)
    session.flush()
    audit.record(session, user, "create", "group", g.id, name=g.name)
    session.commit()
    return group_out(session, g, user)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None


@router.patch("/groups/{gid}")
def update_group(gid: int, body: GroupPatch, user: CurrentUser, session: DB):
    g = get_or_404(session, LabGroup, gid)
    _manage(session, user, g)
    changes = cleaned(g, body.model_dump(exclude_unset=True))
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        clash = session.exec(select(LabGroup).where(LabGroup.name == changes["name"], LabGroup.id != gid)).first()
        if clash:
            raise HTTPException(409, f"There is already a group called {changes['name']}")
    for k, v in changes.items():
        setattr(g, k, v)
    session.add(g)
    audit.record(session, user, "update", "group", g.id, changes=changes)
    session.commit()
    return group_out(session, g, user)


@router.delete("/groups/{gid}")
def delete_group(gid: int, user: Admin, session: DB):
    """The group goes; its notebooks stay with their owners. Group-only notebooks become private,
    never more visible than they were."""
    g = get_or_404(session, LabGroup, gid)
    moved = []
    for nb in session.exec(select(Notebook).where(Notebook.group_id == gid)):
        if (nb.visibility or "everyone") == "group":
            nb.visibility = "private"
        nb.group_id = None
        session.add(nb)
        moved.append(nb.id)
    for m in session.exec(select(GroupMember).where(GroupMember.group_id == gid)):
        session.delete(m)
    session.delete(g)
    audit.record(session, user, "delete", "group", gid, name=g.name, notebooks_made_private=moved)
    session.commit()
    return {"ok": True, "notebooks": moved}


class MemberIn(BaseModel):
    role: str = "member"


@router.put("/groups/{gid}/members/{uid}")
def set_member(gid: int, uid: int, body: MemberIn, user: CurrentUser, session: DB):
    g = get_or_404(session, LabGroup, gid)
    _manage(session, user, g)
    target = get_or_404(session, User, uid)
    if body.role not in ("member", "lead"):
        raise HTTPException(422, "role must be member or lead")
    m = session.exec(select(GroupMember).where(GroupMember.group_id == gid, GroupMember.user_id == uid)).first()
    if m is None:
        m = GroupMember(group_id=gid, user_id=uid, role=body.role)
        if target.id != user.id:
            session.add(Notification(user_id=target.id, kind="group", actor_id=user.id,
                                     text=f"added you to {g.name}" + (" as a lead" if body.role == "lead" else "")))
    else:
        m.role = body.role
    session.add(m)
    audit.record(session, user, "group_member", "group", gid, member=target.username, role=body.role)
    session.commit()
    return group_out(session, g, user)


@router.delete("/groups/{gid}/members/{uid}")
def remove_member(gid: int, uid: int, user: CurrentUser, session: DB):
    g = get_or_404(session, LabGroup, gid)
    if uid != user.id:  # anyone may leave a group; removing others needs a lead or admin
        _manage(session, user, g)
    m = session.exec(select(GroupMember).where(GroupMember.group_id == gid, GroupMember.user_id == uid)).first()
    if m is None:
        raise HTTPException(404, "Not a member")
    leads = session.exec(select(GroupMember).where(GroupMember.group_id == gid, GroupMember.role == "lead")).all()
    if m.role == "lead" and len(leads) == 1 and user.role != Role.admin:
        raise HTTPException(409, "A group needs at least one lead; make someone else a lead first")
    session.delete(m)
    target = session.get(User, uid)
    audit.record(session, user, "group_member_remove", "group", gid, member=target.username if target else uid)
    session.commit()
    return group_out(session, g, user)


# ------------------------------------------------------------------ sharing one experiment

def _can_share(session, user: User, exp: Experiment) -> bool:
    acc = access_for(session, user)
    nb = session.get(Notebook, exp.notebook_id) if exp.notebook_id else None
    return user.role == Role.admin or exp.owner_id == user.id or (nb is not None and acc.leads(nb.group_id))


def shares_out(session, exp: Experiment, user: User) -> dict[str, Any]:
    nb = session.get(Notebook, exp.notebook_id) if exp.notebook_id else None
    g = session.get(LabGroup, nb.group_id) if nb and nb.group_id else None
    shares = session.exec(select(ExperimentShare).where(ExperimentShare.experiment_id == exp.id)).all()
    return {
        "audience": {"visibility": (nb.visibility or "everyone") if nb else ("everyone" if exp.is_template else "private"),
                     "group": {"id": g.id, "name": g.name} if g else None,
                     "notebook": {"id": nb.id, "name": nb.name} if nb else None,
                     "owner": user_brief(session, exp.owner_id)},
        "shares": [{"user": user_brief(session, s.user_id), "permission": s.permission,
                    "created_at": s.created_at.isoformat() + "Z"} for s in shares],
        "can_share": _can_share(session, user, exp),
    }


@router.get("/experiments/{exp_id}/shares")
def get_shares(exp_id: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    return shares_out(session, exp, user)


class ShareIn(BaseModel):
    permission: str = "view"


@router.put("/experiments/{exp_id}/shares/{uid}")
def share(exp_id: int, uid: int, body: ShareIn, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    if not _can_share(session, user, exp):
        raise HTTPException(403, "Only the owner (or a lead of the notebook's group) can share this experiment")
    target = get_or_404(session, User, uid)
    if body.permission not in ("view", "edit"):
        raise HTTPException(422, "permission must be view or edit")
    if target.id == exp.owner_id:
        raise HTTPException(422, "That's the owner")
    if body.permission == "edit" and target.role == Role.viewer:
        raise HTTPException(422, f"{target.full_name or target.username} has a read-only account, so can only be given view access")
    s = session.exec(select(ExperimentShare).where(ExperimentShare.experiment_id == exp.id, ExperimentShare.user_id == uid)).first()
    new = s is None
    if new:
        s = ExperimentShare(experiment_id=exp.id, user_id=uid, permission=body.permission, created_by=user.id)
    else:
        s.permission = body.permission
    session.add(s)
    if new:
        session.add(Notification(user_id=uid, kind="share", actor_id=user.id, experiment_id=exp.id,
                                 text=f"shared {exp.eid} {exp.title} with you" + (" to edit" if body.permission == "edit" else "")))
    audit.record(session, user, "share", "experiment", exp.id, eid=exp.eid, with_user=target.username, permission=body.permission)
    session.commit()
    return shares_out(session, exp, user)


@router.delete("/experiments/{exp_id}/shares/{uid}")
def unshare(exp_id: int, uid: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    s = session.exec(select(ExperimentShare).where(ExperimentShare.experiment_id == exp.id, ExperimentShare.user_id == uid)).first()
    if s is None:
        raise HTTPException(404, "Not shared with that person")
    if not (_can_share(session, user, exp) or uid == user.id):  # you can always drop something shared with you
        raise HTTPException(403, "Only the owner can change sharing")
    target = session.get(User, uid)
    session.delete(s)
    audit.record(session, user, "unshare", "experiment", exp.id, eid=exp.eid, with_user=target.username if target else uid)
    session.commit()
    return shares_out(session, exp, user)
