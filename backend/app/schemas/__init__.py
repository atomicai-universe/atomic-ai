"""Request/response schema package.

Every inbound API payload is modeled as a Pydantic v2 schema derived from
:class:`app.schemas.base.BaseRequest`, so the payload is validated against a
defined schema before any handler processes it (Req 17.1). Rejected payloads
are turned into the ``{"error": {..., "fields": {...}}}`` envelope by
``app.core.errors`` naming the invalid fields (Req 17.2).
"""

from __future__ import annotations

from app.schemas.base import BaseRequest

__all__ = ["BaseRequest"]
