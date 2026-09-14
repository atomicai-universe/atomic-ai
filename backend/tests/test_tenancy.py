"""Unit tests for request context, tenant scoping, and request-schema conventions.

Covers task 4.8:
- Req 16.1/16.2: ``apply_tenant_scope`` constrains a query to the active
  workspace and fails closed (``TenantScopeError``) when no workspace is active.
- Req 17.3: the workspace id is bound as a query parameter (the helper compares
  an ORM column to a value; it never interpolates SQL text).
- Req 4.x plumbing: ``RequestContext`` structurally satisfies
  ``rbac.MembershipContext`` and resolves roles via ``member_role``.
- Req 17.1/17.2: ``BaseRequest`` rejects unknown fields.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.rbac import MembershipContext
from app.core.tenancy import (
    RequestContext,
    TenantScopeError,
    apply_tenant_scope,
    scope_to_workspace,
)
from app.db.models import Integration, MemberRole
from app.schemas.base import BaseRequest


# --- apply_tenant_scope: Req 16.1, 16.2, 17.3 --------------------------------


def test_apply_tenant_scope_filters_on_active_workspace_id() -> None:
    """The returned statement filters integrations by the active workspace id."""
    workspace_id = uuid.uuid4()
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=workspace_id)

    stmt = apply_tenant_scope(select(Integration), Integration, ctx)

    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # The WHERE clause references the tenant column and binds the exact workspace.
    # The UUID column renders as its hyphenless hex under literal binds.
    assert "integrations.workspace_id" in compiled
    assert workspace_id.hex in compiled or str(workspace_id) in compiled


def test_apply_tenant_scope_binds_value_as_parameter_not_sql_text() -> None:
    """The workspace id is a bound parameter, never interpolated SQL text (Req 17.3)."""
    workspace_id = uuid.uuid4()
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=workspace_id)

    stmt = apply_tenant_scope(select(Integration), Integration, ctx)

    # Without literal_binds the compiled SQL shows a placeholder, and the value
    # lives in the compiled params map — proof it is bound, not concatenated.
    compiled = stmt.compile()
    assert "integrations.workspace_id" in str(compiled)
    assert workspace_id in set(compiled.params.values())
    assert str(workspace_id) not in str(compiled)


def test_apply_tenant_scope_does_not_mutate_input_select() -> None:
    """Select builders are immutable: the returned statement differs from the input."""
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=uuid.uuid4())
    original = select(Integration)
    original_sql = str(original)

    scoped = apply_tenant_scope(original, Integration, ctx)

    assert scoped is not original
    # The original has no WHERE clause; the scoped one does.
    assert str(original) == original_sql
    assert original.whereclause is None
    assert scoped.whereclause is not None


def test_apply_tenant_scope_raises_without_active_workspace() -> None:
    """No active workspace => fail closed with TenantScopeError (Req 16.1, 16.2)."""
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=None)

    with pytest.raises(TenantScopeError):
        apply_tenant_scope(select(Integration), Integration, ctx)


def test_scope_to_workspace_alias_matches_apply_tenant_scope() -> None:
    """The ``scope_to_workspace`` alias produces the same scoped statement."""
    workspace_id = uuid.uuid4()
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=workspace_id)

    via_alias = scope_to_workspace(select(Integration), Integration, ctx)
    via_direct = apply_tenant_scope(select(Integration), Integration, ctx)

    assert str(via_alias) == str(via_direct)


# --- RequestContext satisfies rbac.MembershipContext -------------------------


def test_request_context_satisfies_membership_context_protocol() -> None:
    """RequestContext is usable anywhere a MembershipContext is required."""
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=uuid.uuid4())
    assert isinstance(ctx, MembershipContext)


def test_member_role_returns_role_for_known_workspace_and_none_otherwise() -> None:
    """member_role resolves a present workspace and reports absent ones as None."""
    member_ws = uuid.uuid4()
    absent_ws = uuid.uuid4()
    ctx = RequestContext(
        user_id=uuid.uuid4(),
        active_workspace_id=member_ws,
        roles={member_ws: MemberRole.ADMIN},
    )

    assert ctx.member_role(member_ws) is MemberRole.ADMIN
    assert ctx.member_role(absent_ws) is None


# --- BaseRequest rejects unknown fields: Req 17.1, 17.2 ----------------------


class _SampleRequest(BaseRequest):
    name: str


def test_base_request_accepts_declared_fields() -> None:
    """A payload with exactly the declared fields validates."""
    assert _SampleRequest(name="ok").name == "ok"


def test_base_request_rejects_unknown_fields() -> None:
    """An extra, undeclared field is rejected (feeds the 422 field envelope)."""
    with pytest.raises(ValidationError):
        _SampleRequest(name="ok", surprise="nope")
