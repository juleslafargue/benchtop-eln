"""Paging for list endpoints (0.31): `limit` and `offset` query parameters, and the number of matching rows in an
X-Total-Count response header, so a script always knows whether it has everything. The JSON body stays a plain list.
Documented in docs/API.md ("Paging")."""
from __future__ import annotations

from typing import Any, Sequence, TypeVar

from fastapi import Query, Response
from sqlalchemy import func
from sqlmodel import Session, select

MAX_LIMIT = 1000
TOTAL = "X-Total-Count"

T = TypeVar("T")


def limit_param(default: int | None):
    """`limit` for a list: 1 to MAX_LIMIT. A default of None means every match unless the caller asks for fewer."""
    return Query(default, ge=1, le=MAX_LIMIT, description=f"At most this many rows (1 to {MAX_LIMIT})")


def offset_param():
    return Query(0, ge=0, description="Skip this many rows first")


def count(session: Session, stmt: Any) -> int:
    """How many rows a select would return, ignoring its order, limit and offset."""
    sub = stmt.order_by(None).limit(None).offset(None).subquery()
    return session.exec(select(func.count()).select_from(sub)).one()


def set_total(response: Response, total: int) -> None:
    response.headers[TOTAL] = str(total)


def window(rows: Sequence[T], limit: int | None, offset: int) -> list[T]:
    """One page of a list already built in Python."""
    return list(rows[offset:] if limit is None else rows[offset:offset + limit])
