"""The .eln format: export records (one, a notebook, or everything you can see) and import them from other ELNs."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from starlette.background import BackgroundTask

from .. import audit, eln
from ..access import access_for
from ..models import Experiment, Notebook
from ..security import DB, CurrentUser, Writer
from ..services import get_or_404, read_experiment

router = APIRouter(prefix="/api", tags=["eln"])


def _send(session, user, exps: list[Experiment], name: str, what: str, obj_type: str, obj_id: int | None, description: str = ""):
    if not exps:
        raise HTTPException(404, "There's nothing here you can export yet")
    tmp = eln.scratch_dir() / f"export-{os.getpid()}-{datetime.now():%H%M%S%f}.eln"
    try:
        res = eln.export(session, user, exps, tmp, name, description)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    audit.record(session, user, "export_eln", obj_type, obj_id, what=what, experiments=[e.eid for e in exps][:500],
                 count=len(exps), size=res["size"])
    session.commit()
    return FileResponse(tmp, media_type="application/vnd.eln+zip", filename=res["name"],
                        background=BackgroundTask(lambda: Path(tmp).unlink(missing_ok=True)))


@router.get("/experiments/{exp_id}/export.eln")
def export_experiment(exp_id: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    return _send(session, user, [exp], f"{exp.eid} - {exp.title}", "experiment", "experiment", exp.id)


@router.get("/notebooks/{nb_id}/export.eln")
def export_notebook(nb_id: int, user: CurrentUser, session: DB):
    nb = get_or_404(session, Notebook, nb_id)
    acc = access_for(session, user)
    if not acc.can_read_notebook(nb):
        raise HTTPException(404, f"Notebook {nb_id} not found")
    exps = list(session.exec(acc.filter(select(Experiment).where(Experiment.notebook_id == nb.id,
                                                                  Experiment.is_template == False))  # noqa: E712
                             .order_by(Experiment.id)))
    return _send(session, user, exps, f"{nb.name} - {datetime.now():%Y-%m-%d}", "notebook", "notebook", nb.id,
                 f"Notebook \"{nb.name}\" exported from Benchtop ELN")


@router.get("/export.eln")
def export_everything(user: CurrentUser, session: DB):
    """Everything this person can see (their own records, their groups', shared ones): for taking it with them."""
    acc = access_for(session, user)
    exps = list(session.exec(acc.filter(select(Experiment).where(Experiment.is_template == False))  # noqa: E712
                             .order_by(Experiment.notebook_id, Experiment.id)))
    who = user.full_name or user.username
    return _send(session, user, exps, f"Benchtop - {who} - {datetime.now():%Y-%m-%d}", "everything", "user", user.id,
                 f"Everything {who} could see in Benchtop ELN")


# ---------------------------------------------------------------------------------------------------- import


@router.post("/eln/preview")
def import_preview(user: Writer, session: DB, file: UploadFile = File(...)):
    """Step 1: upload a .eln file; see what's in it before anything is added to a notebook."""
    from .. import eln_import
    try:
        token = eln_import.stage(file.file, file.filename or "export.eln", user)
        return eln_import.preview(session, user, token)
    except eln_import.ElnImportError as e:
        if "token" in locals():
            eln_import.discard(token, user)
        raise HTTPException(422, str(e)) from None
    except OSError as e:
        if "token" in locals():
            eln_import.discard(token, user)
        from .. import diskspace
        if diskspace.is_disk_full(e):
            raise HTTPException(507, diskspace.message(e, file=True)) from None
        raise HTTPException(500, f"Couldn't read the file: {e}") from None


class ImportIn(BaseModel):
    notebook_id: int
    items: list[str] = Field(..., min_length=1, max_length=100_000)


@router.post("/eln/import/{token}")
def import_run(token: str, body: ImportIn, user: Writer, session: DB):
    """Step 2: bring the ticked experiments into a notebook (one you can add experiments to)."""
    from .. import eln_import
    nb = get_or_404(session, Notebook, body.notebook_id)
    if not access_for(session, user).can_read_notebook(nb):
        raise HTTPException(404, f"Notebook {body.notebook_id} not found")
    if nb.archived:
        raise HTTPException(409, f"\"{nb.name}\" is archived; choose another notebook")
    try:
        return eln_import.import_items(session, user, token, nb, body.items)
    except eln_import.ElnImportError as e:
        raise HTTPException(422, str(e)) from None


@router.delete("/eln/import/{token}", status_code=204)
def import_discard(token: str, user: CurrentUser):
    from .. import eln_import
    eln_import.discard(token, user)
