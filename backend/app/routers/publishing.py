"""Publishing signed records on Zenodo (0.28): each person's Zenodo token, the admin's settings, and the two steps
(prepare a draft, then publish it). The work is in app/publishing.py; the calls to Zenodo in app/zenodo.py."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import audit, publishing, sitesettings
from ..security import DB, Admin, CurrentUser, Writer
from ..services import read_experiment

router = APIRouter(prefix="/api", tags=["publishing"])


def _run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except publishing.PublishError as e:
        raise HTTPException(e.status, e.message) from None


# ---------------------------------------------------------------- your Zenodo account (Account settings)

class TokenIn(BaseModel):
    token: str


@router.get("/auth/zenodo")
def my_zenodo(user: CurrentUser, session: DB):
    return publishing.account_out(session, user)


@router.put("/auth/zenodo")
def connect_zenodo(body: TokenIn, user: Writer, session: DB):
    """Keep your Zenodo personal access token (checked with Zenodo first). It is never sent back to the browser."""
    return _run(publishing.connect, session, user, body.token)


@router.delete("/auth/zenodo")
def disconnect_zenodo(user: CurrentUser, session: DB):
    return publishing.disconnect(session, user)


# ---------------------------------------------------------------- Admin → Integrations

class ZenodoSettingsIn(BaseModel):
    enabled: bool | None = None
    site: str | None = None  # zenodo | sandbox
    community: str | None = None


@router.get("/admin/zenodo")
def get_zenodo_settings(_: Admin):
    return publishing.site_out()


@router.put("/admin/zenodo")
def set_zenodo_settings(body: ZenodoSettingsIn, user: Admin, session: DB):
    changes: dict[str, Any] = {}
    fixed = sitesettings.public("zenodo")["fixed"]
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if body.site is not None:
        if body.site not in publishing.SITES:
            raise HTTPException(422, "Choose zenodo.org or sandbox.zenodo.org")
        changes["url"] = publishing.SITES[body.site]
    if body.community is not None:
        c = body.community.strip()
        c = re.sub(r"^https?://(sandbox\.)?zenodo\.org/communities/", "", c).strip("/")
        if c and not re.fullmatch(r"[a-z0-9_-]{1,100}", c):
            raise HTTPException(422, "A Zenodo community's identifier is the end of its address, in small letters, digits, "
                                     "hyphens and underscores (zenodo.org/communities/my-lab → my-lab)")
        changes["community"] = c
    if (clash := [k for k in changes if k in fixed]):
        raise HTTPException(409, f"Set by this server's environment ({', '.join(clash)}), so it can't be changed here")
    sitesettings.update("zenodo", changes)
    audit.record(session, user, "settings_zenodo", "system", None, **changes)
    session.commit()
    return publishing.site_out()


# ---------------------------------------------------------------- a record

@router.get("/experiments/{exp_id}/zenodo")
def publication_status(exp_id: int, user: CurrentUser, session: DB):
    exp = read_experiment(session, user, exp_id)
    return publishing.status(session, user, exp)


@router.get("/experiments/{exp_id}/zenodo/preview")
def publication_preview(exp_id: int, user: CurrentUser, session: DB, comments: bool | None = None):
    """What would go to Zenodo: the details (prefilled from the record) and every file that becomes public."""
    exp = read_experiment(session, user, exp_id)
    return publishing.preview(session, user, exp, comments)


@router.post("/experiments/{exp_id}/zenodo/prepare")
def prepare_publication(exp_id: int, body: dict[str, Any], user: Writer, session: DB):
    """Step 1: a draft on Zenodo with a reserved DOI, and the PDF and .eln uploaded. Can still be discarded."""
    exp = read_experiment(session, user, exp_id)
    return _run(publishing.prepare, session, user, exp, body)


class PublishIn(BaseModel):
    confirm: bool = False


@router.post("/experiments/{exp_id}/zenodo/publish")
def publish_publication(exp_id: int, body: PublishIn, user: Writer, session: DB):
    """Step 2, which can't be undone: the draft becomes public and its DOI is registered."""
    exp = read_experiment(session, user, exp_id)
    return _run(publishing.publish, session, user, exp, body.confirm)


@router.delete("/experiments/{exp_id}/zenodo")
def discard_publication(exp_id: int, user: Writer, session: DB):
    exp = read_experiment(session, user, exp_id)
    return _run(publishing.discard, session, user, exp)
