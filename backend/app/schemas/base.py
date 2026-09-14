"""Base conventions for API request schemas.

All inbound request bodies are validated against a defined Pydantic v2 schema
*before* a handler runs, so malformed or unexpected input is rejected at the
edge rather than reaching business logic (Req 17.1). :class:`BaseRequest`
supplies the shared configuration every request model inherits:

- ``extra="forbid"`` — unknown/unexpected fields are rejected rather than
  silently ignored. A payload that carries a field the schema does not declare
  raises a :class:`pydantic.ValidationError`, which
  ``app.core.errors._request_validation_handler`` converts into a 422 envelope
  whose ``fields`` map names each offending field (Req 17.2). This narrows the
  accepted input surface and resists mass-assignment / smuggled fields.

Request values that flow into the database are always bound as query parameters
through the ORM (Req 17.3); these schemas never build SQL text, so no
user-supplied value is interpolated into a query string.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BaseRequest(BaseModel):
    """Base class for all API request payload models.

    Subclass this for every request body so the strict-input conventions
    (Req 17.1, 17.2) are applied uniformly:

        >>> class CreateWidgetRequest(BaseRequest):
        ...     name: str

    Constructing a subclass with a field it does not declare raises
    :class:`pydantic.ValidationError` because ``extra`` is forbidden.
    """

    model_config = ConfigDict(extra="forbid")


class ExampleRequest(BaseRequest):
    """Minimal example demonstrating the request-schema convention.

    Present as living documentation of the pattern: a single declared field and
    strict rejection of anything else (inherited from :class:`BaseRequest`).
    Not wired to any route.
    """

    name: str


__all__ = ["BaseRequest", "ExampleRequest"]
