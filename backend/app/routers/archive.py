"""The yearly archive (0.34): one archival PDF of a year's signed records (see app/archive.py for how it runs).

Anyone signed in can make one, of the records they can open; it only reads. A job is seen, fed and downloaded only by
the person who started it."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Path as PathParam, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import archive
from ..security import DB, CurrentUser

router = APIRouter(prefix="/api/archive", tags=["yearly archive"])

Year = Annotated[int, Query(ge=1970, le=2200)]
OptId = Annotated[int | None, Query(ge=1, le=2**31 - 1)]
JobId = Annotated[str, PathParam(pattern="^[0-9a-f]{16}$")]
AttId = Annotated[int, PathParam(ge=1, le=2**31 - 1)]


class JobIn(BaseModel):
    year: int = Field(ge=1970, le=2200)
    notebook_id: int | None = Field(None, ge=1, le=2**31 - 1)
    author_id: int | None = Field(None, ge=1, le=2**31 - 1)
    page_size: str = Field("letter", pattern="^(letter|a4)$")


class SkipIn(BaseModel):
    reason: str = Field("", max_length=300)


def _fail(e: archive.ArchiveError):
    raise HTTPException(e.status, str(e)) from None


@router.get("/options")
def archive_options(user: CurrentUser, session: DB):
    """The years with signed records you can open, the notebooks and authors they come from, and your latest job."""
    job = archive.current_job(user)
    return {**archive.options(session, user), "job": archive.job_out(job) if job else None}


@router.get("/preview")
def archive_preview(user: CurrentUser, session: DB, year: Year, notebook_id: OptId = None, author_id: OptId = None):
    """How many records an archive with these choices holds, before building it."""
    return archive.preview(session, user, year, notebook_id, author_id)


@router.post("/jobs", status_code=201)
def start_archive(body: JobIn, user: CurrentUser, session: DB):
    """Start an archive: the answer lists the records and the spectrum files for the browser to draw. Starting one
    replaces your earlier one."""
    try:
        return archive.job_out(archive.start_job(session, user, body.year, body.notebook_id, body.author_id, body.page_size))
    except archive.ArchiveError as e:
        _fail(e)


@router.get("/jobs/{job_id}")
def get_archive(job_id: JobId, user: CurrentUser):
    try:
        return archive.job_out(archive.get_job(user, job_id))
    except archive.ArchiveError as e:
        _fail(e)


@router.put("/jobs/{job_id}/spectra/{att_id}")
def upload_spectrum(job_id: JobId, att_id: AttId, user: CurrentUser, file: UploadFile = File(...)):
    """One spectrum, drawn by the browser, as a PNG. Used only in this archive: it's never added to a record."""
    data = file.file.read(archive.MAX_PNG_BYTES + 1)
    try:
        return archive.job_out(archive.add_picture(user, job_id, att_id, data), with_spectra=False)
    except archive.ArchiveError as e:
        _fail(e)


@router.post("/jobs/{job_id}/spectra/{att_id}/skip")
def skip_spectrum(job_id: JobId, att_id: AttId, body: SkipIn, user: CurrentUser):
    """A file the browser couldn't draw: the archive lists it by name and fingerprint instead."""
    try:
        return archive.job_out(archive.skip_picture(user, job_id, att_id, body.reason), with_spectra=False)
    except archive.ArchiveError as e:
        _fail(e)


@router.post("/jobs/{job_id}/build", status_code=202)
def build_archive(job_id: JobId, user: CurrentUser):
    """Lay out the PDF in the background; GET the job to follow it."""
    try:
        return archive.job_out(archive.request_build(user, job_id), with_spectra=False)
    except archive.ArchiveError as e:
        _fail(e)


@router.get("/jobs/{job_id}/download")
def download_archive(job_id: JobId, user: CurrentUser):
    try:
        path = archive.pdf_path(user, job_id)
        name = archive.file_name(archive.get_job(user, job_id))
    except archive.ArchiveError as e:
        _fail(e)
    return FileResponse(path, media_type="application/pdf", filename=name)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_archive(job_id: JobId, user: CurrentUser):
    try:
        archive.delete_job(user, job_id)
    except archive.ArchiveError as e:
        _fail(e)
