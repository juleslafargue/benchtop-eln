"""Edits (PATCH requests) that send an empty value (null) for something that can't be empty.

Before 0.24.1 such a value went straight to the database, which refused it, and the person saw "Internal Server
Error" (found by fuzzing the API: tests/fuzz_api.py). Now a text field or list that has a default is cleared (a null
description becomes ""), and anything else (a title, a name, yes/no settings) is refused with a plain message.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic_core import PydanticUndefined


def cleaned(obj: Any, changes: dict[str, Any]) -> dict[str, Any]:
    """The changes, with a null for a field that can't be empty either cleared or refused (422)."""
    model = type(obj)
    cols = model.__table__.c
    out = dict(changes)
    for key, value in changes.items():
        if value is not None or key not in cols or cols[key].nullable:
            continue
        field = model.model_fields.get(key)
        default = PydanticUndefined
        if field is not None:
            default = field.default_factory() if field.default_factory is not None else field.default
        if isinstance(default, (str, list)):
            out[key] = default  # clearing a description or the tags
        else:
            raise HTTPException(422, f"{key.replace('_', ' ').capitalize()} can't be empty")
    return out
