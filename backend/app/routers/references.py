"""DOI lookup for References sections (the section's content itself is saved like any other)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import doi
from ..models import Role
from ..security import Writer

router = APIRouter(prefix="/api", tags=["references"])


@router.get("/doi")
def lookup_doi(q: str, user: Writer):
    """A paper's title, authors, journal, year, volume, issue and pages, from doi.org."""
    try:
        return doi.lookup(q)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except doi.NotFound as e:
        raise HTTPException(404, str(e)) from None
    except doi.DoiError as e:
        if not doi.enabled():  # said plainly; an admin, who can turn it on, also gets the setting's name
            raise HTTPException(503, "Looking up DOIs is turned off on this server; type the details in by hand"
                                + (" (BENCHTOP_DOI=off)" if user.role == Role.admin else "")) from None
        raise HTTPException(502, str(e)) from None
