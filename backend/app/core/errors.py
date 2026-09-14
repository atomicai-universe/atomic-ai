"""Central error envelope and exception handlers.

Every error the API returns is shaped as a single, safe-to-expose envelope::

    { "error": { "code": <machine code>, "message": <human text>, "fields"?: {...} } }

produced by the handlers installed via :func:`install_exception_handlers`. The
envelope never contains stack traces, internal file paths, or secret values
(Req 17.4). Any unhandled exception fails closed as a generic HTTP 500 whose
body reveals no internals (fail-closed rule from the design's Error Handling).

The same secret scrubber that protects audit metadata (``app.core.scrubbing``)
is applied to any structured metadata (e.g. validation ``fields``) included in
a response, so a field name or value that looks like a secret is redacted.

Requirements: 17.4 (errors exclude stack traces, paths, secrets), 17.5
(oversized body -> 413 envelope), 21.2 (auth rate limit -> 429 envelope).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.scrubbing import scrub

logger = logging.getLogger("atomic_ai.errors")

# Secure-default response headers applied to every response, including error
# envelopes. Kept here (and mirrored by the security-headers middleware) so an
# error generated above the middleware stack still carries them (Req 21.4).
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Permitted-Cross-Domain-Policies": "none",
}

# Maps an HTTP status code to a stable, machine-readable error code. Kept
# intentionally generic so responses never encode internal details.
_STATUS_CODE_NAMES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_CONTENT_TOO_LARGE: "payload_too_large",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_502_BAD_GATEWAY: "upstream_error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}

# Generic, safe human messages per status. Handlers may override the message
# for a specific error, but these guarantee we never fall back to framework
# defaults that could leak internals.
_STATUS_MESSAGES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "The request was invalid.",
    status.HTTP_401_UNAUTHORIZED: "Authentication is required or has failed.",
    status.HTTP_403_FORBIDDEN: "You do not have permission to perform this action.",
    status.HTTP_404_NOT_FOUND: "The requested resource was not found.",
    status.HTTP_409_CONFLICT: "The request conflicts with the current state.",
    status.HTTP_413_CONTENT_TOO_LARGE: "The request body is too large.",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "The request payload failed validation.",
    status.HTTP_429_TOO_MANY_REQUESTS: "Too many requests. Please try again later.",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "An internal error occurred.",
    status.HTTP_502_BAD_GATEWAY: "An upstream dependency failed.",
    status.HTTP_503_SERVICE_UNAVAILABLE: "The service is temporarily unavailable.",
}


class APIError(Exception):
    """An application error that maps directly to the response envelope.

    Services and routers raise this (rather than framework exceptions) when
    they want to control the ``code``/``message``/``fields`` of the response
    while still being routed through the central, scrubbing handler.
    """

    def __init__(
        self,
        status_code: int,
        code: str | None = None,
        message: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code or _STATUS_CODE_NAMES.get(status_code, "error")
        self.message = message or _STATUS_MESSAGES.get(
            status_code, "An error occurred."
        )
        self.fields = fields
        super().__init__(self.message)


def build_error_response(
    status_code: int,
    code: str | None = None,
    message: str | None = None,
    fields: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a scrubbed ``{ "error": { ... } }`` JSON response.

    The ``fields`` payload (when present) is passed through the secret scrubber
    so any sensitive key/value is redacted before it leaves the process.
    """
    resolved_code = code or _STATUS_CODE_NAMES.get(status_code, "error")
    resolved_message = message or _STATUS_MESSAGES.get(
        status_code, "An error occurred."
    )
    error_body: dict[str, Any] = {"code": resolved_code, "message": resolved_message}
    if fields is not None:
        error_body["fields"] = scrub(fields)
    # Merge in the secure-default headers. Any explicitly provided header wins,
    # but security headers are added so error responses that bypass the ASGI
    # security-headers middleware (e.g. framework-generated errors) still carry
    # them. Duplicates are avoided by using a single merged dict (Req 21.4).
    merged_headers: dict[str, str] = dict(SECURITY_HEADERS)
    if headers:
        merged_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        content={"error": error_body},
        headers=merged_headers,
    )


def _validation_fields(exc: RequestValidationError) -> dict[str, str]:
    """Summarize a validation error as ``{field_path: message}``.

    Only the field location and a short message are included; the raw input
    value is deliberately omitted so a rejected payload never echoes back
    possibly sensitive submitted data.
    """
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        # Drop the leading "body"/"query"/"path" segment for readability.
        parts = [str(p) for p in loc if p not in ("body", "query", "path")]
        key = ".".join(parts) if parts else "__request__"
        fields[key] = str(err.get("msg", "invalid"))
    return fields


async def _api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
    return build_error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        fields=exc.fields,
    )


async def _http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render framework HTTPExceptions through the envelope.

    A string ``detail`` becomes the message only for client errors (4xx). For
    5xx we ignore any provided detail and use the generic message so internal
    context cannot leak.
    """
    headers = getattr(exc, "headers", None)
    message: str | None = None
    if exc.status_code < 500 and isinstance(exc.detail, str):
        message = exc.detail
    return build_error_response(
        status_code=exc.status_code,
        message=message,
        headers=headers,
    )


async def _request_validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return build_error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        fields=_validation_fields(exc),
    )


async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Fail closed: any unhandled exception becomes a generic 500.

    The exception is logged server-side (with traceback) for operators, but the
    response body contains only the generic envelope so no stack trace, file
    path, or secret is exposed to the client (Req 17.4).
    """
    logger.error(
        "Unhandled exception while processing %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return build_error_response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


def install_exception_handlers(app: FastAPI) -> None:
    """Register the central exception handlers on the application.

    Ordering note: the broad ``Exception`` handler guarantees fail-closed
    behavior for anything not matched by a more specific handler.
    """
    app.add_exception_handler(APIError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
