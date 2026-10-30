"""Instrument folders (admins), the Inbox, follow-up experiments and "Not mine"."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session, select

from .. import audit, instruments
from ..access import access_for
from ..models import (
    Attachment, Entry, EntryKind, EntryVersion, Experiment, ExperimentShare, ExperimentStatus, Instrument,
    InstrumentFile, LabGroup, Notebook, Role, User, utcnow,
)
from ..security import DB, Admin, CurrentUser, Writer
from ..services import get_or_404, read_experiment, require_editable, user_brief

router = APIRouter(prefix="/api", tags=["instruments"])


# ====================================================================== Admin → Instruments

def instrument_out(session: Session, i: Instrument) -> dict[str, Any]:
    from sqlalchemy import func
    counts: dict[str, int] = dict(session.exec(select(InstrumentFile.state, func.count()).where(
        InstrumentFile.instrument_id == i.id).group_by(InstrumentFile.state)).all())
    groups = [{"id": g.id, "name": g.name} for g in (session.get(LabGroup, gid) for gid in i.audience or []) if g]
    return {"id": i.id, "name": i.name, "kind": i.kind, "kind_label": instruments.KINDS.get(i.kind, i.kind),
            "folder": i.folder, "audience": groups, "settle_minutes": round(i.settle_seconds / 60, 2), "enabled": i.enabled,
            "import_since": i.import_since.isoformat() + "Z" if i.import_since else None,
            "last_scan_at": i.last_scan_at.isoformat() + "Z" if i.last_scan_at else None,
            "last_scan_ms": i.last_scan_ms, "last_error": i.last_error, "counts": counts,
            "created_at": i.created_at.isoformat() + "Z"}


class InstrumentIn(BaseModel):
    name: str
    kind: str = "files"
    folder: str
    audience: list[int] = []
    settle_minutes: float = 2
    enabled: bool = True
    import_since: Optional[datetime] = None


class InstrumentPatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    folder: Optional[str] = None
    audience: Optional[list[int]] = None
    settle_minutes: Optional[float] = None
    enabled: Optional[bool] = None
    import_since: Optional[datetime] = None
    clear_import_since: bool = False


def _naive(d: Optional[datetime]) -> Optional[datetime]:
    """Benchtop stores times in UTC without a zone; a time without one is taken as UTC already."""
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d is not None and d.tzinfo else d


def _check(session: Session, name: Optional[str], kind: Optional[str], audience: Optional[list[int]],
           settle: Optional[float]) -> None:
    if name is not None and not 1 <= len(name.strip()) <= 80:
        raise HTTPException(422, "Give the instrument a name (up to 80 characters), e.g. “NMR – Bruker 400”")
    if kind is not None and kind not in instruments.KINDS:
        raise HTTPException(422, f"Type must be one of: {', '.join(instruments.KINDS)}")
    if audience is not None and any(session.get(LabGroup, g) is None for g in audience):
        raise HTTPException(422, "One of those groups doesn't exist")
    if settle is not None and not 0.25 <= settle <= 24 * 60:
        raise HTTPException(422, "The quiet time is between 15 seconds and 24 hours (in minutes)")


def _folder(session: Session, raw: str, skip_id: Optional[int] = None) -> str:
    try:
        p = instruments.validate_folder(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    for other in session.exec(select(Instrument).where(Instrument.archived == False)):  # noqa: E712
        if other.id != skip_id and other.folder == str(p):
            raise HTTPException(409, f"“{other.name}” already watches that folder")
    return str(p)


@router.get("/instruments")
def list_instruments(_: Admin, session: DB):
    rows = session.exec(select(Instrument).where(Instrument.archived == False).order_by(Instrument.name))  # noqa: E712
    return {"instruments": [instrument_out(session, i) for i in rows], "kinds": instruments.KINDS,
            "scan_seconds": instruments.settings.instrument_scan_seconds, "max_mb": instruments.settings.instrument_max_mb}


@router.post("/instruments", status_code=201)
def add_instrument(body: InstrumentIn, user: Admin, session: DB):
    _check(session, body.name, body.kind, body.audience, body.settle_minutes)
    folder = _folder(session, body.folder)
    i = Instrument(name=body.name.strip(), kind=body.kind, folder=folder, audience=sorted(set(body.audience)),
                   settle_seconds=int(body.settle_minutes * 60), enabled=body.enabled,
                   import_since=_naive(body.import_since), created_by=user.id)
    session.add(i)
    session.flush()
    audit.record(session, user, "create", "instrument", i.id, name=i.name, kind=i.kind, folder=folder,
                 audience=i.audience, import_since=str(i.import_since) if i.import_since else None)
    session.commit()
    return instrument_out(session, i)


@router.patch("/instruments/{iid}")
def edit_instrument(iid: int, body: InstrumentPatch, user: Admin, session: DB):
    i = get_or_404(session, Instrument, iid)
    _check(session, body.name, body.kind, body.audience, body.settle_minutes)
    changes: dict[str, Any] = {}
    if body.folder is not None:
        f = _folder(session, body.folder, skip_id=i.id)
        if f != i.folder:  # a different folder starts afresh: what's already there is noted, not taken
            i.folder, i.baseline_at = f, None
            changes["folder"] = f
    if body.kind is not None and body.kind != i.kind:
        i.kind, i.baseline_at = body.kind, None
        changes["kind"] = body.kind
    for k in ("name", "enabled"):
        v = getattr(body, k)
        if v is not None:
            setattr(i, k, v.strip() if isinstance(v, str) else v)
            changes[k] = v
    if body.audience is not None:
        i.audience = sorted(set(body.audience))
        changes["audience"] = i.audience
    if body.settle_minutes is not None:
        i.settle_seconds = int(body.settle_minutes * 60)
        changes["settle_seconds"] = i.settle_seconds
    if body.import_since is not None or body.clear_import_since:
        i.import_since = None if body.clear_import_since else _naive(body.import_since)
        changes["import_since"] = str(i.import_since) if i.import_since else None
    session.add(i)
    audit.record(session, user, "update", "instrument", i.id, name=i.name, **changes)
    session.commit()
    return instrument_out(session, i)


@router.delete("/instruments/{iid}")
def remove_instrument(iid: int, user: Admin, session: DB):
    """Stop watching. Its history (and anything waiting in the Inbox) stays."""
    i = get_or_404(session, Instrument, iid)
    i.archived, i.enabled = True, False
    session.add(i)
    audit.record(session, user, "remove", "instrument", i.id, name=i.name, folder=i.folder)
    session.commit()
    return {"ok": True}


class PreviewIn(BaseModel):
    folder: str
    kind: str = "files"
    import_since: Optional[datetime] = None


@router.post("/instruments/check")
def check_folder(body: PreviewIn, _: Admin, session: DB):
    """What Benchtop would do with a folder, without doing any of it."""
    if body.kind not in instruments.KINDS:
        raise HTTPException(422, f"Type must be one of: {', '.join(instruments.KINDS)}")
    try:
        return instruments.preview(session, body.folder, body.kind, _naive(body.import_since))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/instruments/{iid}/old")
def old_data(iid: int, _: Admin, session: DB, q: str = "", offset: int = 0, limit: int = 100):
    """What was already in the folder when it was added, and what would happen to each if taken now."""
    inst = get_or_404(session, Instrument, iid)
    return instruments.old_items(session, inst, q, offset, limit)


class TakeIn(BaseModel):
    paths: list[str]


@router.post("/instruments/{iid}/take")
def take_old_data(iid: int, body: TakeIn, user: Admin, session: DB):
    """Bring in chosen datasets that were already there: they're handled like new arrivals, straight away."""
    inst = get_or_404(session, Instrument, iid)
    if inst.archived:
        raise HTTPException(404, "Not found")
    if not inst.enabled:
        raise HTTPException(409, f"{inst.name} is paused. Resume it first.")
    if len(body.paths) > instruments.MAX_TAKE:
        raise HTTPException(422, f"Choose at most {instruments.MAX_TAKE:,} at a time")
    res = instruments.take_old(session, inst, body.paths)
    if res["taken"]:
        audit.record(session, user, "take_old", "instrument", inst.id, name=inst.name, count=len(res["taken"]),
                     paths=res["taken"][:100])
        session.commit()
        instruments.scan_all()
    return res


@router.post("/instruments/scan")
async def scan_now(_: Admin):
    """Look at every folder now, rather than at the next minute."""
    res = await run_in_threadpool(instruments.scan_all)
    return {"scanned": len(res)}


# ====================================================================== the Inbox

def _visible(session: Session, user: User, row_id: int) -> tuple[InstrumentFile, Instrument]:
    row = session.get(InstrumentFile, row_id)
    inst = session.get(Instrument, row.instrument_id) if row else None
    if row is None or inst is None or row.state not in ("inbox", "ignored", "attached") \
            or not instruments.can_see(session, user, row, inst):
        raise HTTPException(404, "That file isn't in your Inbox")  # 404: don't confirm it exists
    if row.state == "attached":  # once in a record, it follows that record's sharing
        exp = session.get(Experiment, row.experiment_id) if row.experiment_id else None
        if exp is None or not access_for(session, user).can_read(exp):
            raise HTTPException(404, "That file isn't in your Inbox")
    return row, inst


def item_out(session: Session, user: User, row: InstrumentFile, inst: Instrument) -> dict[str, Any]:
    acc = access_for(session, user)
    matched = []
    for eid in row.matched_ids or []:
        e = session.get(Experiment, eid)
        if e and acc.can_read(e):
            matched.append({"id": e.id, "eid": e.eid, "title": e.title, "status": e.status.value,
                            "notebook_id": e.notebook_id})
    exp = session.get(Experiment, row.experiment_id) if row.experiment_id else None
    return {"id": row.id, "path": row.path, "revision": row.revision, "state": row.state, "note": row.note,
            "instrument": {"id": inst.id, "name": inst.name, "kind": inst.kind},
            "files": [{"n": n, "name": f["name"], "size": f["size"], "content_type": f["content_type"]}
                      for n, f in enumerate(row.files or [])],
            "size": row.size, "arrived_at": row.first_seen_at.isoformat() + "Z",
            "handled_at": row.handled_at.isoformat() + "Z" if row.handled_at else None,
            "handled_by": user_brief(session, row.handled_by) if row.handled_by else None,
            "matched": matched,
            "experiment": {"id": exp.id, "eid": exp.eid, "title": exp.title} if exp and acc.can_read(exp) else None,
            "follow_up": bool(len(matched) == 1 and matched[0]["status"] in ("signed", "witnessed")),
            "for_you": row.person_id == user.id}


@router.get("/inbox")
def inbox(user: CurrentUser, session: DB, state: str = "inbox", limit: int = 200):
    """Arrivals this person may see: waiting (inbox), ignored, or recently attached (attached)."""
    if state not in ("inbox", "ignored", "attached"):
        raise HTTPException(422, "state is inbox, ignored or attached")
    insts = {i.id: i for i in session.exec(select(Instrument))}
    order = InstrumentFile.handled_at.desc() if state == "attached" else InstrumentFile.first_seen_at.desc()
    out = []
    for row in session.exec(select(InstrumentFile).where(InstrumentFile.state == state).order_by(order, InstrumentFile.id.desc())):
        inst = insts.get(row.instrument_id)
        if inst and instruments.can_see(session, user, row, inst):
            if state == "attached":  # recent arrivals: only those in records this person can read
                exp = session.get(Experiment, row.experiment_id) if row.experiment_id else None
                if not exp or not access_for(session, user).can_read(exp):
                    continue
            out.append(item_out(session, user, row, inst))
            if len(out) >= min(limit, 500):
                break
    return out


@router.get("/inbox/count")
def inbox_count(user: CurrentUser, session: DB):
    insts = {i.id: i for i in session.exec(select(Instrument))}
    n = mine = 0
    for row in session.exec(select(InstrumentFile).where(InstrumentFile.state == "inbox")):
        inst = insts.get(row.instrument_id)
        if inst and instruments.can_see(session, user, row, inst):
            n += 1
            mine += row.person_id == user.id or any(
                (e := session.get(Experiment, i)) is not None and e.owner_id == user.id for i in row.matched_ids or [])
    return {"waiting": n, "yours": mine}


@router.get("/inbox/{row_id}/files/{n}")
def inbox_file(row_id: int, n: int, user: CurrentUser, session: DB, inline: bool = False):
    row, _ = _visible(session, user, row_id)
    if not 0 <= n < len(row.files or []):
        raise HTTPException(404, "No such file")
    f = row.files[n]
    path = instruments.blob_path(f["sha256"])
    if not path.exists():
        raise HTTPException(410, "That file is missing from Benchtop's storage")
    resp = FileResponse(path, media_type=f["content_type"], filename=f["name"],
                        content_disposition_type="inline" if inline else "attachment")
    if f["content_type"] != "application/pdf":
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'"
    return resp


def _claim(session: Session, row: InstrumentFile, states: tuple[str, ...]) -> None:
    """Take an Inbox item for this action, atomically: of two people clicking at once, one wins."""
    audit._take_write_lock(session)
    res = session.execute(update(InstrumentFile).where(InstrumentFile.id == row.id, InstrumentFile.state.in_(states))
                          .values(state="handling").execution_options(synchronize_session=False))
    if res.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "Someone else has just dealt with this file; reload the Inbox")
    session.refresh(row)


def _attach_from_inbox(session: Session, user: User, row: InstrumentFile, inst: Instrument, exp: Experiment, rule: str) -> None:
    if not row.files:
        raise HTTPException(409, "This arrival wasn't copied into Benchtop (see its note), so it can't be attached")
    try:
        instruments.attach(session, row, inst, exp, user, rule)
    except instruments.NotOpen:
        session.rollback()
        raise HTTPException(409, f"{exp.eid} isn't a draft any more, so files can't be added to it") from None
    row.handled_by = user.id
    session.add(row)
    if exp.owner_id != user.id:
        instruments.notify(session, exp.owner_id, f"{user.full_name or user.username} attached {row.path} from "
                           f"{inst.name} to {exp.eid}", exp.id)


class AssignIn(BaseModel):
    experiment_id: int


@router.post("/inbox/{row_id}/assign")
def assign(row_id: int, body: AssignIn, user: Writer, session: DB):
    row, inst = _visible(session, user, row_id)
    exp = read_experiment(session, user, body.experiment_id)
    if exp.is_template:
        raise HTTPException(422, "Files go into experiments, not templates")
    if exp.status != ExperimentStatus.open:
        raise HTTPException(409, f"{exp.eid} is {exp.status.value}, so files can't be added to it. Use a follow-up experiment.")
    if not access_for(session, user).can_edit(exp):
        raise HTTPException(403, f"You can't edit {exp.eid}; ask its owner to share it with you for editing")
    _claim(session, row, ("inbox", "ignored"))
    _attach_from_inbox(session, user, row, inst, exp, "assigned")
    session.commit()
    return item_out(session, user, row, inst)


@router.post("/inbox/{row_id}/ignore")
def ignore(row_id: int, user: Writer, session: DB):
    row, inst = _visible(session, user, row_id)
    _claim(session, row, ("inbox",))
    row.state, row.handled_by, row.handled_at = "ignored", user.id, utcnow()
    session.add(row)
    audit.record(session, user, "ignore", "instrument_file", row.id, instrument=inst.name, path=row.path)
    session.commit()
    return item_out(session, user, row, inst)


@router.post("/inbox/{row_id}/unignore")
def unignore(row_id: int, user: Writer, session: DB):
    row, inst = _visible(session, user, row_id)
    _claim(session, row, ("ignored",))
    row.state, row.handled_by, row.handled_at = "inbox", None, None
    session.add(row)
    audit.record(session, user, "unignore", "instrument_file", row.id, instrument=inst.name, path=row.path)
    session.commit()
    return item_out(session, user, row, inst)


@router.get("/inbox/targets")
def assign_targets(user: CurrentUser, session: DB, q: str = ""):
    """Drafts this person can add files to, most recently changed first."""
    acc = access_for(session, user)
    stmt = select(Experiment).where(Experiment.status == ExperimentStatus.open, Experiment.is_template == False)  # noqa: E712
    out = []
    ql = q.strip().lower()
    for e in session.exec(acc.filter(stmt).order_by(Experiment.updated_at.desc())):
        if not acc.can_edit(e):
            continue
        if ql and ql not in e.title.lower() and ql not in e.eid.lower():
            continue
        out.append({"id": e.id, "eid": e.eid, "title": e.title, "mine": e.owner_id == user.id,
                    "owner": user_brief(session, e.owner_id), "updated_at": e.updated_at.isoformat() + "Z"})
        if len(out) >= 30:
            break
    return out


# ====================================================================== follow-up experiments

class FollowUpIn(BaseModel):
    notebook_id: Optional[int] = None
    copy_shares: bool = True
    title: Optional[str] = None


def create_follow_up(session: Session, user: User, original: Experiment, body: FollowUpIn) -> Experiment:
    """A new draft, owned by this person, linked to a signed record. The link is stored on the new record only,
    so nothing the original's signature covers changes."""
    if original.is_template or original.status not in (ExperimentStatus.signed, ExperimentStatus.witnessed, ExperimentStatus.locked):
        raise HTTPException(409, "Follow-ups are for signed, witnessed or locked records; a draft can simply be edited")
    acc = access_for(session, user)
    nb_id = body.notebook_id
    if nb_id is None:
        nb = session.get(Notebook, original.notebook_id) if original.notebook_id else None
        if nb is None or not acc.can_read_notebook(nb) or nb.archived:
            raise HTTPException(400, f"You can't add experiments to {original.eid}'s notebook. Choose one of your notebooks.")
        nb_id = nb.id
    else:
        nb = session.get(Notebook, nb_id)
        if nb is None or not acc.can_read_notebook(nb):
            raise HTTPException(404, f"Notebook {nb_id} not found")
        if nb.archived:
            from .experiments import archived_message
            raise HTTPException(409, archived_message(nb))
    title = (body.title or "").strip()[:300] or f"Follow-up to {original.eid}: {original.title}"[:300]
    exp = Experiment(title=title, notebook_id=nb_id, owner_id=user.id, follows_id=original.id,
                     description=f"Results for {original.eid} that came after it was signed.")
    session.add(exp)
    session.flush()
    e = Entry(experiment_id=exp.id, kind=EntryKind.text, title="Notes", content={"html": ""}, position=0, updated_by=user.id)
    session.add(e)
    session.flush()
    session.add(EntryVersion(entry_id=e.id, version=1, title=e.title, content=e.content, created_by=user.id))
    audit.record(session, user, "create", "experiment", exp.id, eid=exp.eid, title=exp.title, follows=original.eid)
    if body.copy_shares:  # the people who could see the original can see what came after it
        people: dict[int, str] = {}
        if original.owner_id != user.id:
            people[original.owner_id] = "view"
        for s in session.exec(select(ExperimentShare).where(ExperimentShare.experiment_id == original.id)):
            if s.user_id != user.id:
                people[s.user_id] = s.permission
        for uid, perm in people.items():
            u = session.get(User, uid)
            if u is None or not u.active:
                continue
            if perm == "edit" and u.role == Role.viewer:
                perm = "view"
            session.add(ExperimentShare(experiment_id=exp.id, user_id=uid, permission=perm, created_by=user.id))
            audit.record(session, user, "share", "experiment", exp.id, eid=exp.eid, with_user=u.username, permission=perm)
    return exp


@router.post("/inbox/{row_id}/follow-up", status_code=201)
def inbox_follow_up(row_id: int, body: FollowUpIn, user: Writer, session: DB):
    row, inst = _visible(session, user, row_id)
    if len(row.matched_ids or []) != 1:
        raise HTTPException(409, "This file doesn't point to one signed experiment; use Assign instead")
    original = read_experiment(session, user, row.matched_ids[0])
    if row.state not in ("inbox", "ignored"):
        raise HTTPException(409, "Someone else has just dealt with this file; reload the Inbox")
    exp = create_follow_up(session, user, original, body)
    _claim(session, row, ("inbox", "ignored"))
    _attach_from_inbox(session, user, row, inst, exp, "follow-up")
    session.commit()
    return {"id": exp.id, "eid": exp.eid}


@router.post("/experiments/{exp_id}/follow-up", status_code=201)
def experiment_follow_up(exp_id: int, body: FollowUpIn, user: Writer, session: DB):
    original = read_experiment(session, user, exp_id)
    exp = create_follow_up(session, user, original, body)
    session.commit()
    return {"id": exp.id, "eid": exp.eid}


def follow_info(session: Session, user: User, exp: Experiment) -> dict[str, Any]:
    """The links shown on a record. Only what this person may read is named: a follow-up of a record they can't
    see says just that; follow-ups they can't see aren't mentioned at all."""
    acc = access_for(session, user)
    follows = None
    if exp.follows_id:
        o = session.get(Experiment, exp.follows_id)
        follows = {"id": o.id, "eid": o.eid, "title": o.title} if o and acc.can_read(o) else {"hidden": True}
    ups = [{"id": f.id, "eid": f.eid, "title": f.title, "status": f.status.value}
           for f in session.exec(select(Experiment).where(Experiment.follows_id == exp.id).order_by(Experiment.id))
           if acc.can_read(f)]
    return {"follows": follows, "follow_ups": ups}


# ====================================================================== "Not mine"

@router.post("/attachments/{att_id}/not-mine")
def not_mine(att_id: int, user: Writer, session: DB):
    """Send a file that arrived from an instrument back to the Inbox: it isn't this experiment's (a mistyped
    number, someone else's sample). Only on a draft; the file leaves the record, and the audit trail says who
    sent it back and when."""
    a = get_or_404(session, Attachment, att_id)
    exp = read_experiment(session, user, a.experiment_id)
    if a.instrument_file_id is None or a.withdrawn_at is not None:
        raise HTTPException(400, "Only files that arrived from an instrument folder can be sent back to the Inbox")
    require_editable(exp, user)  # the owner, an editor or an admin; and only while it's a draft
    row = session.get(InstrumentFile, a.instrument_file_id)
    inst = session.get(Instrument, row.instrument_id) if row else None
    if row is None or inst is None:
        raise HTTPException(404, "That file's arrival record is missing")
    audit._take_write_lock(session)
    gone = set(row.attachment_ids or [a.id])
    now = utcnow()
    for att in session.exec(select(Attachment).where(Attachment.id.in_(gone), Attachment.experiment_id == exp.id)):
        att.withdrawn_at, att.withdrawn_by = now, user.id
        session.add(att)
    for e in session.exec(select(Entry).where(Entry.experiment_id == exp.id, Entry.deleted == False)):  # noqa: E712
        c = dict(e.content or {})
        changed = False
        if e.kind == EntryKind.file and any(i in gone for i in c.get("attachment_ids", [])):
            c["attachment_ids"] = [i for i in c["attachment_ids"] if i not in gone]
            if isinstance(c.get("analyses"), dict):  # saved spectrum analyses are keyed by attachment id
                c["analyses"] = {k: v for k, v in c["analyses"].items() if not (str(k).isdigit() and int(k) in gone)}
            changed = True
        if e.kind == EntryKind.characterization:
            items = [dict(it) for it in c.get("items", [])]
            for it in items:
                if it.get("attachment_id") in gone:
                    it["attachment_id"] = None
                    changed = True
            c["items"] = items
        if changed:
            e.content = c
            e.version += 1
            e.updated_by, e.updated_at = user.id, now
            session.add_all([e, EntryVersion(entry_id=e.id, version=e.version, title=e.title, content=c, created_by=user.id)])
            audit.record(session, user, "update", "entry", e.id, experiment=exp.eid, version=e.version)
    exp.updated_at = now
    session.add(exp)
    who = user.full_name or user.username
    row.state, row.experiment_id, row.attachment_ids, row.matched_ids = "inbox", None, [], []
    row.note = f"Sent back from an experiment by {who}: it wasn't theirs. Assign it to the right one."
    row.handled_by, row.handled_at = user.id, now
    session.add(row)
    audit.record(session, user, "not_mine", "experiment", exp.id, experiment=exp.eid, instrument=inst.name,
                 path=row.path, attachments=sorted(gone))
    session.commit()
    return {"ok": True, "removed": sorted(gone)}
