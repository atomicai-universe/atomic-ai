"""Integrations router — connect / sharing toggle / disconnect (task 9.5).

This module wires the encrypted :mod:`app.services.integration_vault` primitives
(tasks 9.1/9.3) into authenticated HTTP endpoints under ``/api/v1/integrations``.
Every route requires a valid Session_Token via :func:`app.api.deps.require_session`
and is authorized against the caller's workspace :class:`~app.db.models.MemberRole`.

Endpoints:

- ``POST /api/v1/integrations`` (connect) — Owner/Admin only
  (:attr:`~app.core.rbac.Capability.MANAGE_INTEGRATIONS`, Req 7.1, 7.6). Encrypts
  and persists the credential via :func:`app.services.integration_vault.store`
  and writes an ``integration.connected`` audit row.
- ``PATCH /api/v1/integrations/{id}/sharing`` (toggle) — authorized by the pure
  :func:`app.services.integration_vault.can_toggle_sharing` rule (creator, or
  non-creating Owner/Admin) enforced inside
  :func:`app.services.integration_vault.set_sharing` (Req 7.6). Writes an
  ``integration.sharing_toggled`` audit row.
- ``DELETE /api/v1/integrations/{id}`` (disconnect) — the creator, or an
  Owner/Admin holding ``MANAGE_INTEGRATIONS``, may disconnect (Req 7.6). Deletes
  the encrypted credential via :func:`app.services.integration_vault.disconnect`
  and writes an ``integration.disconnected`` audit row.

Secret hygiene (Req 6.4). Decrypted OAuth tokens never enter this layer, and the
ciphertext columns (``encrypted_access_token`` / ``encrypted_refresh_token``) are
never returned or logged. Responses use the deliberately-narrow
:class:`_IntegrationView` (id, workspace, category, provider, sharing flag,
status) and audit metadata is additionally passed through
:func:`app.core.scrubbing.scrub` as defense-in-depth.

RBAC wiring note. The pure decision layer :func:`app.core.rbac.can` is consulted
directly here (against the role resolved from ``ctx.member_role(workspace_id)``)
rather than through the :func:`app.core.rbac.require` dependency factory:
``require`` authorizes against the caller's *active* workspace, whereas these
routes take the target ``workspace_id`` from the request body, so the role is
resolved for that workspace explicitly. Resolving the role from the already-built
:class:`~app.core.tenancy.RequestContext` and raising
:class:`~app.core.errors.APIError` (404 for a non-member so the workspace's
existence is not disclosed, 403 for insufficient role) keeps the authorization
decision in one place and matches the HTTP semantics documented on
:func:`app.core.rbac.require`.

Audit trail (Req 15.1). Every state-changing operation appends a
:class:`~app.db.models.SystemAuditLog` row and the router commits the operation
and its audit entry together, so an audit row is present exactly when the change
is durable. Auth is not workspace-scoped, but integrations are, so the audit row
carries the integration's ``workspace_id`` and the acting ``user_id``.

Requirements: 6.4, 7.1, 7.6, 15.1.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_session
from app.core.errors import APIError
from app.core.rbac import Capability, can
from app.core.scrubbing import scrub
from app.core.tenancy import RequestContext
from app.db.models import (
    Integration,
    IntegrationCategory,
    MemberRole,
    SystemAuditLog,
)
from app.db.session import get_session
from app.schemas.base import BaseRequest
from app.services import integration_oauth
from app.services import integration_vault
from app.services import provider_credentials
from app.services import webhook_subscriptions
from app.services import webhook_registration
from app.services import oauth_refresh

logger = logging.getLogger("atomic_ai.integrations")

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

# Audit action names for integration lifecycle events (Req 15.1).
AUDIT_ACTION_CONNECTED = "integration.connected"
AUDIT_ACTION_SHARING_TOGGLED = "integration.sharing_toggled"
AUDIT_ACTION_DISCONNECTED = "integration.disconnected"


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ConnectIntegrationRequest(BaseRequest):
    """Body for connecting (storing) an integration credential.

    Providers use different credential shapes, so ``credentials`` is a flexible
    map of the SECRET fields that provider needs (e.g. ``{"bot_token": "xoxb-…"}``
    for Slack, ``{"client_id":…, "client_secret":…, "refresh_token":…}`` for
    Gmail, ``{"access_key_id":…, "secret_access_key":…}`` for AWS). ``config``
    carries NON-secret operational values (region, host, base_url, tenant id …).
    Every value in ``credentials`` is encrypted at rest by the vault and never
    echoed back (Req 6.1/6.2/6.4).

    ``access_token`` / ``refresh_token`` remain accepted for backward
    compatibility and are merged into the credential map by the vault.
    """

    workspace_id: uuid.UUID
    category: IntegrationCategory
    provider_name: str
    credentials: dict[str, str] | None = None
    config: dict[str, str] | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    is_shared_with_workspace: bool = False


class ToggleSharingRequest(BaseRequest):
    """Body for toggling an integration's workspace-sharing scope (Req 7.6)."""

    workspace_id: uuid.UUID
    shared: bool


class DisconnectIntegrationRequest(BaseRequest):
    """Body for disconnecting (deleting) an integration credential (Req 7.7)."""

    workspace_id: uuid.UUID


class _IntegrationView(BaseModel):
    """Safe, token-free projection of an :class:`~app.db.models.Integration`.

    Deliberately omits ``encrypted_access_token`` / ``encrypted_refresh_token``
    (and of course any plaintext token) so no credential material can leave the
    process through a response body (Req 6.4).
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    category: IntegrationCategory
    provider_name: str
    is_shared_with_workspace: bool
    status: str
    webhook: dict | None = None
    # True once the per-integration OAuth authorize flow has completed and a
    # refresh token is stored (read from a non-secret config marker; never
    # decrypts credentials). Lets the UI show an "Authorized" state.
    authorized: bool = False


def _to_view(integration: Integration) -> _IntegrationView:
    """Project an ORM integration onto the token-free response view (Req 6.4)."""
    _cfg = integration.config or {}
    return _IntegrationView(
        id=integration.id,
        workspace_id=integration.workspace_id,
        category=integration.category,
        provider_name=integration.provider_name,
        is_shared_with_workspace=integration.is_shared_with_workspace,
        status=str(integration.status),
        webhook=webhook_subscriptions.webhook_status_view(
            integration.provider_name, _cfg
        ),
        authorized=bool(_cfg.get(integration_oauth.OAUTH_AUTHORIZED_KEY, False)),
    )


# ---------------------------------------------------------------------------
# Authorization + audit helpers
# ---------------------------------------------------------------------------


def _require_member_role(ctx: RequestContext, workspace_id: uuid.UUID) -> MemberRole:
    """Return the caller's role in ``workspace_id`` or raise 404 for a non-member.

    Mirrors the membership semantics of :func:`app.core.rbac.require`: a caller
    who is not a member gets **404** so the workspace's existence is not
    disclosed (Req 4.3, 16.2). Resolving the role from the already-built
    :class:`RequestContext` keeps the guard's decision close to the workspace it resolves.
    """
    role = ctx.member_role(workspace_id)
    if role is None:
        raise APIError(
            status_code=404,
            code="not_found",
            message="The requested resource was not found.",
        )
    return role


def _require_capability(role: MemberRole, capability: Capability) -> None:
    """Raise 403 if ``role`` lacks ``capability`` (Req 7.6).

    Uses the pure :func:`app.core.rbac.can` decision so the role-capability map
    stays the single source of truth.
    """
    if not can(role, capability):
        raise APIError(
            status_code=403,
            code="forbidden",
            message="You do not have permission to perform this action.",
        )


def _write_audit(
    session: AsyncSession,
    *,
    action: str,
    ctx: RequestContext,
    workspace_id: uuid.UUID,
    metadata: dict | None = None,
) -> None:
    """Append an integration Audit_Log row with scrubbed metadata (Req 15.1, 6.4).

    The row records the workspace, the acting ``user_id``, and the ``action``.
    Any ``metadata`` is passed through :func:`app.core.scrubbing.scrub` so a
    token/credential-shaped value can never be persisted — though callers never
    place token material in the metadata to begin with.
    """
    session.add(
        SystemAuditLog(
            workspace_id=workspace_id,
            user_id=ctx.user_id,
            action=action,
            log_metadata=scrub(metadata) if metadata is not None else None,
        )
    )


async def _load_owned_integration(
    session: AsyncSession, integration_id: uuid.UUID, workspace_id: uuid.UUID
) -> Integration:
    """Load an integration, enforcing it belongs to ``workspace_id`` (Req 16.2).

    Raises **404** when the integration does not exist or belongs to a different
    workspace, so a caller can never read or act on another tenant's integration
    (tenant isolation).
    """
    integration = await session.get(Integration, integration_id)
    if integration is None or integration.workspace_id != workspace_id:
        raise APIError(
            status_code=404,
            code="not_found",
            message="Integration not found.",
        )
    return integration


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", status_code=200)
async def list_integrations(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List a workspace's integrations (token-free) so the UI can hydrate.

    Requires membership of ``workspace_id`` (404 for non-members, so the
    workspace's existence is not disclosed). Returns the deliberately-narrow
    :class:`_IntegrationView` for each row — never any credential material
    (Req 6.4). This is what lets the integrations page survive a refresh: rows
    persisted by connect are read back here.
    """
    _require_member_role(ctx, workspace_id)
    rows = await integration_vault.list_for_workspace(
        session, workspace_id=workspace_id
    )
    return {"integrations": [_to_view(r).model_dump(mode="json") for r in rows]}


@router.get("/spec", status_code=200)
async def get_credential_spec(
    ctx: RequestContext = Depends(require_session),
) -> dict:
    """Return the per-provider credential field specification (BUILD.md).

    Lets the frontend render exactly the fields each provider needs (which are
    secret vs. config, which are required). Requires a valid session but is not
    workspace-scoped. Contains no secrets — only field definitions.

    Also appends any webhook-target config fields a provider needs to
    auto-register its push webhook (e.g. Mailchimp list_id, GitHub owner/repo),
    marked optional, so the connect form can prompt for them.
    """
    spec = provider_credentials.spec_json()
    for provider, fields in spec.items():
        existing = {f["name"] for f in fields}
        for target in webhook_registration.target_fields_for(provider):
            if target["name"] not in existing:
                fields.append(target)
    return {"providers": spec}


@router.post("", status_code=201)
async def connect_integration(
    body: ConnectIntegrationRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> _IntegrationView:
    """Connect (store) an encrypted integration credential (Req 7.1, 7.6, 6.4, 15.1).

    Requires the caller to hold ``MANAGE_INTEGRATIONS`` (Owner/Admin) in the
    target workspace. Encrypts and persists the credential via
    :func:`integration_vault.store`, writes an ``integration.connected`` audit
    row (no token in metadata), and commits both together.

    The response is the token-free :class:`_IntegrationView`; neither the
    plaintext tokens nor the stored ciphertext are ever returned (Req 6.4).
    """
    role = _require_member_role(ctx, body.workspace_id)
    _require_capability(role, Capability.MANAGE_INTEGRATIONS)

    # Merge any legacy access/refresh token into the flexible credential map,
    # then validate the provider's REQUIRED secret fields are all present.
    credentials: dict[str, str] = dict(body.credentials or {})
    if body.access_token is not None:
        credentials.setdefault("access_token", body.access_token)
    if body.refresh_token is not None:
        credentials.setdefault("refresh_token", body.refresh_token)

    required = provider_credentials.required_field_names(body.provider_name)
    secret_required = required & provider_credentials.secret_field_names(
        body.provider_name
    )
    config = dict(body.config or {})

    # Webhook-target config fields (e.g. Mailchimp list_id, GitHub owner/repo)
    # are required for the provider's webhook to be registered, so enforce them
    # at connect too. These are non-secret and supplied in `config`.
    target_fields = webhook_registration.target_fields_for(body.provider_name)
    required_targets = {t["name"] for t in target_fields if t.get("required")}

    # A required secret field must be supplied in `credentials`; a required
    # non-secret/config field (including webhook targets) in `config`.
    missing = [
        name
        for name in required
        if (name in secret_required and not credentials.get(name))
        or (name not in secret_required and not config.get(name))
    ]
    missing += [name for name in required_targets if not config.get(name)]
    if missing:
        raise APIError(
            status_code=422,
            code="invalid_request",
            message=(
                "Missing required credential field(s) for "
                f"{body.provider_name}: {', '.join(sorted(missing))}."
            ),
        )

    integration = await integration_vault.store(
        session,
        workspace_id=body.workspace_id,
        created_by_user_id=ctx.user_id,
        category=body.category,
        provider_name=body.provider_name,
        credentials=credentials,
        config=config or None,
        is_shared_with_workspace=body.is_shared_with_workspace,
    )

    # Register the provider's instant-trigger (Gmail users.watch / webhook) and
    # persist the per-integration webhook secret. Best-effort: never block a
    # successful connection on subscription registration.
    try:
        # For Gmail/Graph, mint an access token so users.watch can be called now.
        sub_creds = await oauth_refresh.ensure_access_token(
            body.provider_name, credentials, config
        )
        sub = await webhook_subscriptions.register_subscription(
            provider=body.provider_name,
            integration_id=str(integration.id),
            credentials=sub_creds,
            config=integration.config or {},
        )
        integration.config = sub["config"]
        await session.flush()
    except Exception:  # noqa: BLE001 - subscription is best-effort
        logger.exception("subscription registration failed for %s", integration.id)

    _write_audit(
        session,
        action=AUDIT_ACTION_CONNECTED,
        ctx=ctx,
        workspace_id=body.workspace_id,
        metadata={
            "integration_id": str(integration.id),
            "category": str(integration.category),
            "provider_name": integration.provider_name,
            "is_shared_with_workspace": integration.is_shared_with_workspace,
        },
    )
    await session.commit()
    logger.info(
        "integration.connected id=%s workspace_id=%s user_id=%s",
        integration.id,
        body.workspace_id,
        ctx.user_id,
    )
    return _to_view(integration)


@router.patch("/{integration_id}/sharing")
async def toggle_sharing(
    integration_id: uuid.UUID,
    body: ToggleSharingRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> _IntegrationView:
    """Toggle an integration's workspace-sharing scope (Req 7.6, 15.1).

    Authorization is the pure creator-or-Owner/Admin rule enforced inside
    :func:`integration_vault.set_sharing` (via ``can_toggle_sharing``), so a
    non-creating Member/Viewer is rejected with 403 there. The integration must
    belong to the caller's workspace (404 otherwise). Writes an
    ``integration.sharing_toggled`` audit row and commits both together.
    """
    role = _require_member_role(ctx, body.workspace_id)
    # Enforce tenant ownership before mutating (404 hides foreign integrations).
    await _load_owned_integration(session, integration_id, body.workspace_id)

    integration = await integration_vault.set_sharing(
        session,
        integration_id=integration_id,
        user_id=ctx.user_id,
        role=role,
        shared=body.shared,
    )

    _write_audit(
        session,
        action=AUDIT_ACTION_SHARING_TOGGLED,
        ctx=ctx,
        workspace_id=body.workspace_id,
        metadata={
            "integration_id": str(integration.id),
            "is_shared_with_workspace": integration.is_shared_with_workspace,
        },
    )
    await session.commit()
    logger.info(
        "integration.sharing_toggled id=%s shared=%s user_id=%s",
        integration.id,
        integration.is_shared_with_workspace,
        ctx.user_id,
    )
    return _to_view(integration)


@router.delete("/{integration_id}", status_code=200)
async def disconnect_integration(
    integration_id: uuid.UUID,
    body: DisconnectIntegrationRequest,
    ctx: RequestContext = Depends(require_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Disconnect (delete) an integration credential (Req 7.6, 7.7, 15.1).

    Authorized for the integration's **creator**, or a non-creating Owner/Admin
    holding ``MANAGE_INTEGRATIONS`` (Req 7.6); a non-creating Member/Viewer is
    rejected with 403. The integration must belong to the caller's workspace
    (404 otherwise). Deletes the encrypted credential via
    :func:`integration_vault.disconnect`, writes an ``integration.disconnected``
    audit row, and commits both together.
    """
    role = _require_member_role(ctx, body.workspace_id)
    integration = await _load_owned_integration(
        session, integration_id, body.workspace_id
    )

    is_creator = integration.created_by_user_id == ctx.user_id
    if not is_creator:
        # A non-creator may disconnect only with the manage capability
        # (Owner/Admin); Member/Viewer is rejected (Req 7.6).
        _require_capability(role, Capability.MANAGE_INTEGRATIONS)

    await integration_vault.disconnect(session, integration_id=integration_id)

    _write_audit(
        session,
        action=AUDIT_ACTION_DISCONNECTED,
        ctx=ctx,
        workspace_id=body.workspace_id,
        metadata={
            "integration_id": str(integration_id),
            "category": str(integration.category),
            "provider_name": integration.provider_name,
        },
    )
    await session.commit()
    logger.info(
        "integration.disconnected id=%s workspace_id=%s user_id=%s",
        integration_id,
        body.workspace_id,
        ctx.user_id,
    )
    return {"status": "disconnected"}


__all__ = [
    "router",
    "AUDIT_ACTION_CONNECTED",
    "AUDIT_ACTION_SHARING_TOGGLED",
    "AUDIT_ACTION_DISCONNECTED",
]
