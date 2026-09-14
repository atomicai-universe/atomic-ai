"""Shared request-schema conventions for the API layer.

Every router that accepts a JSON body subclasses :class:`BaseRequest` (rather
than a bare :class:`pydantic.BaseModel`) so the whole surface shares one
validation policy:

- ``extra="forbid"`` — an unknown/misspelled field is a hard error, not a
  silently-ignored key. Pydantic raises a :class:`~pydantic.ValidationError`
  which FastAPI surfaces as a :class:`fastapi.exceptions.RequestValidationError`;
  the central handler in :mod:`app.core.errors` converts that into the ``422``
  ``{"error": {"code": "validation_error", "fields": {...}}}`` envelope naming
  each invalid field (Req 17.1, 17.2).
- ``str_strip_whitespace=True`` — surrounding whitespace is trimmed so
  " value " and "value" validate identically.
- ``from_attributes=True`` — models may also be built from ORM objects when a
  request schema is reused for internal construction.

Note on parameter binding (Req 17.3): request *values* validated here are later
handed to the SQLAlchemy ORM, which binds them as query parameters — they are
never interpolated into SQL text. The tenant-scoping helper in
:mod:`app.core.tenancy` likewise builds its ``WHERE`` clause with a bound
parameter (``Model.workspace_id == ctx.active_workspace_id``), so user-supplied
identifiers never reach the database as raw string fragments.

Requirements: 17.1 (validate against a defined schema), 17.2 (name invalid
fields), 17.3 (parameterized queries — no string interpolation).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BaseRequest(BaseModel):
    """Base class for every inbound API request body.

    Subclasses inherit a strict, shared configuration. Rejecting unknown fields
    by default (``extra="forbid"``) means a client that sends a typo'd or
    unexpected key gets a precise ``422`` naming that field instead of having it
    silently dropped — closing off a class of "worked in tests, ignored in prod"
    bugs and making input validation total.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        from_attributes=True,
    )


__all__ = ["BaseRequest"]
