"""Supporting Information documents (0.33): one per paper, kept by the person writing it and seen only by them.

A document lists experiments (with the compound number and name used in the paper); reading it back gives, for each,
the write-up, the product and its spectra, and what a journal will ask for that the record doesn't have yet. The
browser builds the Word file from that. Nothing here changes an experiment."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import select

from .. import audit, si
from ..access import access_for
from ..models import Experiment, SiDocument, utcnow
from ..security import DB, CurrentUser, Writer

router = APIRouter(prefix="/api", tags=["supporting information"])

MAX_ITEMS = 300
Id = Annotated[int, Field(ge=1, le=2**31 - 1)]


class ItemIn(BaseModel):
    experiment_id: Id
    number: str = Field("", max_length=20)
    name: str = Field("", max_length=300)
    spectra: list[Id] | None = Field(None, max_length=60)  # attachment ids to draw; None: every NMR file


class DocIn(BaseModel):
    title: str = Field("", max_length=300)


class DocUpdate(BaseModel):
    title: str | None = Field(None, max_length=300)
    general: str | None = Field(None, max_length=50_000)
    items: list[ItemIn] | None = Field(None, max_length=MAX_ITEMS)
    page_size: str | None = Field(None, pattern="^(letter|a4)$")
    expected_version: int | None = None


def _mine(session, user, doc_id: int) -> SiDocument:
    doc = session.get(SiDocument, doc_id) if 0 < doc_id < 2**31 else None
    if doc is None or doc.owner_id != user.id:  # 404 either way: someone else's SI isn't confirmed to exist
        raise HTTPException(404, "Supporting information document not found")
    return doc


@router.get("/si-documents")
def list_documents(user: CurrentUser, session: DB):
    docs = session.exec(select(SiDocument).where(SiDocument.owner_id == user.id)
                        .order_by(SiDocument.updated_at.desc(), SiDocument.id.desc())).all()
    return [{"id": d.id, "title": d.title, "compounds": len(d.items or []), "page_size": d.page_size,
             "created_at": d.created_at.isoformat() + "Z", "updated_at": d.updated_at.isoformat() + "Z"} for d in docs]


@router.post("/si-documents", status_code=201)
def create_document(body: DocIn, user: Writer, session: DB):
    doc = SiDocument(owner_id=user.id, title=body.title.strip())
    session.add(doc)
    session.flush()
    audit.record(session, user, "create", "si_document", doc.id, title=doc.title)
    session.commit()
    session.refresh(doc)
    return si.document_out(session, user, doc)


@router.get("/si-documents/{doc_id}")
def get_document(doc_id: int, user: CurrentUser, session: DB):
    return si.document_out(session, user, _mine(session, user, doc_id))


@router.put("/si-documents/{doc_id}")
def update_document(doc_id: int, body: DocUpdate, user: Writer, session: DB):
    doc = _mine(session, user, doc_id)
    items = None
    if body.items is not None:
        before = {it.get("experiment_id") for it in doc.items or []}
        acc = access_for(session, user)
        for it in body.items:
            if it.experiment_id in before:
                continue  # already in it: kept even if it's since become hidden (it's left out of the document)
            exp = session.get(Experiment, it.experiment_id)
            if exp is None or not acc.can_read(exp):
                raise HTTPException(404, f"Experiment {it.experiment_id} not found")
        items = [{"experiment_id": it.experiment_id, "number": it.number.strip(), "name": it.name.strip(),
                  "spectra": it.spectra} for it in body.items]
    # one save at a time: a save made from an older version is refused, so two tabs can't undo each other's work
    expected = body.expected_version if body.expected_version is not None else doc.version
    r = session.exec(update(SiDocument).where(SiDocument.id == doc.id, SiDocument.version == expected)
                     .values(version=SiDocument.version + 1))
    if r.rowcount != 1:
        session.rollback()
        session.refresh(doc)
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"This document was changed elsewhere (now version {doc.version}); reload before saving")
    session.refresh(doc)
    if body.title is not None:
        doc.title = body.title.strip()
    if body.general is not None:
        doc.general = body.general
    if items is not None:
        doc.items = items
    if body.page_size is not None:
        doc.page_size = body.page_size
    doc.updated_at = utcnow()
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return si.document_out(session, user, doc)


@router.delete("/si-documents/{doc_id}")
def delete_document(doc_id: int, user: Writer, session: DB):
    doc = _mine(session, user, doc_id)
    audit.record(session, user, "delete", "si_document", doc.id, title=doc.title, compounds=len(doc.items or []))
    session.delete(doc)
    session.commit()
    return {"ok": True, "id": doc_id}
