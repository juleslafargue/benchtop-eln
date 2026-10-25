"""Everything the dashboard widgets show, in one request."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter
from sqlmodel import select

from .. import safety
from ..models import Attachment, ChemSafety, Experiment, ExperimentStatus, Lot, LotTransaction, Material, Role, Signature, utcnow
from ..security import DB, CurrentUser
from ..services import user_brief, experiment_summary
from .reactions import _collect

router = APIRouter(prefix="/api", tags=["dashboard"])

SPECTRUM_RE = re.compile(r"\.(jdx|dx|jcamp|jcm|csv|tsv|xy|zip)$", re.I)
LOW_STOCK_FRACTION = 0.2
EXPIRY_DAYS = 30
STALE_DAYS = 14


@router.get("/dashboard")
def dashboard(user: CurrentUser, session: DB):
    from ..access import visible_experiment_filter
    vis = lambda stmt: visible_experiment_filter(stmt, session, user)  # noqa: E731
    exps = session.exec(vis(select(Experiment).where(Experiment.is_template == False)  # noqa: E712
                        .order_by(Experiment.updated_at.desc()))).all()
    mine = [e for e in exps if e.owner_id == user.id]
    summ = lambda e: experiment_summary(session, e)  # noqa: E731

    # returned to me: open, mine, and the latest signature event is a rejection
    returned = []
    open_mine = {e.id: e for e in mine if e.status == ExperimentStatus.open}
    last_sig: dict[int, Signature] = {}
    if open_mine:  # one query for all of them, not one per draft
        for sig in session.exec(select(Signature).where(Signature.experiment_id.in_(list(open_mine))).order_by(Signature.id)):
            last_sig[sig.experiment_id] = sig
    for e in mine:
        last = last_sig.get(e.id)
        if e.id in open_mine and last and last.meaning == "rejected":
            returned.append({**summ(e), "reason": last.comment, "returned_by": last.signer_name})

    pinned_ids = [i for i in ((user.preferences or {}).get("pinned") or []) if isinstance(i, int)]
    by_id = {e.id: e for e in exps}
    pinned = [summ(by_id[i]) for i in pinned_ids if i in by_id]

    # lab stats (everyone's reactions)
    rx = _collect(session, user)
    now = utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_start = (month_start - timedelta(days=1)).replace(day=1)
    def created(e: Experiment) -> Any:
        return e.created_at
    this_month = sum(1 for e in exps if created(e) >= month_start)
    last_month = sum(1 for e in exps if prev_start <= created(e) < month_start)
    witnessed_month = sum(1 for e in exps if e.status == ExperimentStatus.witnessed and e.updated_at >= month_start)
    yields = [{"experiment_id": r["experiment_id"], "eid": r["eid"], "title": r["title"], "date": r["date"],
               "yield_pct": r["yield_pct"], "product": r["product"]} for r in rx if r["yield_pct"] is not None]

    spectra = []
    for a, e in session.exec(vis(select(Attachment, Experiment).join(Experiment, Experiment.id == Attachment.experiment_id)
                             .order_by(Attachment.created_at.desc()))).all():
        if SPECTRUM_RE.search(a.filename):
            spectra.append({"id": a.id, "filename": a.filename, "experiment_id": e.id, "eid": e.eid, "title": e.title,
                            "created_at": a.created_at.isoformat() + "Z"})
        if len(spectra) >= 10:
            break

    # inventory alerts
    low, expiring = [], []
    for lot, m in session.exec(select(Lot, Material).join(Material, Material.id == Lot.material_id)
                               .where(Lot.status == "available")).all():
        first = session.exec(select(LotTransaction).where(LotTransaction.lot_id == lot.id).order_by(LotTransaction.id)).first()
        initial = first.balance_after if first and first.delta > 0 else None
        row = {"lot_id": lot.id, "barcode": lot.barcode, "material_id": m.id, "material": m.name,
               "amount": lot.amount, "unit": lot.unit, "location": lot.location}
        if initial and lot.amount < LOW_STOCK_FRACTION * initial:
            low.append({**row, "fraction": round(lot.amount / initial, 3)})
        if lot.expires_at and lot.expires_at <= now + timedelta(days=EXPIRY_DAYS):
            expiring.append({**row, "expires_at": lot.expires_at.isoformat() + "Z", "expired": lot.expires_at <= now})
    # safety classifications that changed since they were last looked at
    by_key = {safety.material_key(m): m for m in session.exec(select(Material)).all()}
    safety_changes = []
    for cs in session.exec(select(ChemSafety).where(ChemSafety.changed_at != None)).all():  # noqa: E711
        if cs.acknowledged_at and cs.acknowledged_at >= cs.changed_at:
            continue
        m = by_key.get(cs.key)
        safety_changes.append({"material_id": m.id if m else None, "material": m.name if m else cs.title,
                               "changed_at": cs.changed_at.isoformat() + "Z", "change": cs.change})
    low.sort(key=lambda r: r["fraction"])
    expiring.sort(key=lambda r: r["expires_at"])

    returned_ids = {r["id"] for r in returned}
    # witness requests: what I've been asked to witness comes first; my own records show who they wait on
    from ..models import WitnessRequest
    signed_ids = [e.id for e in exps if e.status == ExperimentStatus.signed]
    reqs: dict[int, WitnessRequest] = {}
    if signed_ids:
        for r in session.exec(select(WitnessRequest).where(WitnessRequest.status == "open", WitnessRequest.experiment_id.in_(signed_ids))):
            reqs[r.experiment_id] = r

    def with_request(e: Experiment) -> dict[str, Any]:
        r = reqs.get(e.id)
        extra = {"assigned_to_me": bool(r and r.witness_id == user.id), "witness": user_brief(session, r.witness_id) if r else None,
                 "asked_at": r.created_at.isoformat() + "Z" if r else None, "overdue": bool(r and r.due_at and r.due_at < now)}
        return {**summ(e), **extra}
    others = [e for e in exps if e.status == ExperimentStatus.signed and e.owner_id != user.id]
    others.sort(key=lambda e: (not (e.id in reqs and reqs[e.id].witness_id == user.id), not (e.id in reqs and reqs[e.id].due_at and reqs[e.id].due_at < now)))
    awaiting = [with_request(e) for e in others[:10]]
    waiting = [with_request(e) for e in [e for e in mine if e.status == ExperimentStatus.signed][:10]]
    return {
        "lists": {
            "recent": [summ(e) for e in mine[:8]],
            "open": [summ(e) for e in [e for e in mine if e.status == ExperimentStatus.open][:10]],
            "awaiting": awaiting,
            "waiting": waiting,
            "returned": returned[:10],
            "pinned": pinned,
            # drafts nobody has touched for two weeks (returned ones are already listed above)
            "stale": [summ(e) for e in [e for e in mine if e.status == ExperimentStatus.open and e.updated_at < now - timedelta(days=STALE_DAYS)
                      and e.id not in returned_ids][:10]],
        },
        "stale_days": STALE_DAYS,
        "stats": {"this_month": this_month, "last_month": last_month, "witnessed_this_month": witnessed_month,
                  "yields": yields[:200]},
        "spectra": spectra,
        # admins see backup problems where they'll notice them
        "system": {"backup_warnings": _backup_warnings() if user.role == Role.admin else [],
                   "check_warnings": _check_warnings() if user.role == Role.admin else [],
                   "disk_warnings": _disk_warnings() if user.role == Role.admin else [],
                   "update": _update() if user.role == Role.admin else None},
        "inventory": {"low": low[:10], "expiring": expiring[:10], "safety_changes": safety_changes[:10], "low_fraction": LOW_STOCK_FRACTION, "expiry_days": EXPIRY_DAYS},
    }


def _update() -> dict[str, Any] | None:
    """A newer Benchtop, for the admin (the check itself runs daily in the background)."""
    from .. import updates
    try:
        st = updates.status()
    except Exception:  # noqa: BLE001 - a dashboard must load whatever is in the saved check
        return None
    return st if st["available"] else None


def _check_warnings() -> list[str]:
    """What the daily data check found (0.24); nothing when all is well."""
    from .. import integrity
    try:
        return integrity.health()["warnings"]
    except Exception:  # noqa: BLE001 - a dashboard must load whatever is in the saved result
        return []


def _disk_warnings() -> list[str]:
    """A disk Benchtop uses running low on space (0.25)."""
    from .. import diskspace
    try:
        return diskspace.warnings()
    except Exception:  # noqa: BLE001 - a dashboard must load whatever the disks say
        return []


def _backup_warnings() -> list[str]:
    from .. import backup
    try:
        backup._sqlite_path()
    except backup.BackupError:
        return []  # Postgres servers back up with their own tools
    try:
        return backup.health()["warnings"]
    except Exception:  # noqa: BLE001 - a dashboard must load even if the backup folder is odd
        return []
