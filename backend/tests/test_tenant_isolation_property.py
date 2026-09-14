"""Property-based tests for tenant isolation at the query/job scoping layer.

Implements **Property 29: Tenant isolation of queries and jobs** from the design.

**Validates: Requirements 16.1, 16.2, 16.3**

- Req 16.1: every tenant-scoped read passes through the single choke point
  :func:`app.core.tenancy.apply_tenant_scope`, which fails closed when there is
  no active workspace to scope to.
- Req 16.2: a query scoped to one workspace binds exactly that workspace id and
  can never reference another tenant's id, so cross-tenant rows are unreachable.
- Req 16.3: a queued job carries its originating ``workspace_id`` and re-applies
  the same tenant scope, yielding a statement bound to that same workspace.

These properties operate on the *compiled* statement's bound parameters. The
scoping helper builds ``WHERE model.workspace_id == active_workspace_id`` using
the ORM column and the context value, so the workspace id is bound as a query
parameter. Inspecting ``stmt.compile().params`` lets us assert exactly which
workspace value the query can see, without a database.
"""

from __future__ import annotations

import uuid

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.core.tenancy import RequestContext, TenantScopeError, apply_tenant_scope
from app.db.models import (
    AgentSession,
    ApprovalRequest,
    Integration,
    Rule,
    WorkspaceMember,
)

# The tenant-scoped models the choke point is expected to constrain. Each
# exposes a ``workspace_id`` column.
_SCOPED_MODELS = [Integration, Rule, AgentSession, ApprovalRequest, WorkspaceMember]

# The design mandates a minimum of 100 examples for property tests; these
# generators are cheap so we run 200.
_PBT = settings(max_examples=200)


def _bound_workspace_ids(stmt) -> list[uuid.UUID]:
    """Return the workspace-id values bound into ``stmt``'s compiled params.

    The scoping helper binds the active workspace id as a query parameter; this
    collects every UUID present in the compiled parameter map so tests can
    assert which tenant value(s) the statement can reference.
    """
    params = stmt.compile().params
    return [v for v in params.values() if isinstance(v, uuid.UUID)]


# --- Property 29: a scoped statement binds exactly the active workspace -----

@_PBT
@given(active_workspace_id=st.uuids(), model=st.sampled_from(_SCOPED_MODELS))
def test_scope_binds_exactly_active_workspace(active_workspace_id, model) -> None:
    """apply_tenant_scope binds the active workspace id and no other workspace
    value, for any tenant-scoped model (Req 16.1, 16.2)."""
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=active_workspace_id)
    stmt = apply_tenant_scope(select(model), model, ctx)

    bound = _bound_workspace_ids(stmt)
    assert active_workspace_id in bound
    # No workspace value other than the active one is bound into the query.
    assert all(v == active_workspace_id for v in bound)


# --- Property 29: a statement scoped to w1 never references w2 --------------

@_PBT
@given(
    workspaces=st.lists(st.uuids(), min_size=2, max_size=2, unique=True),
    model=st.sampled_from(_SCOPED_MODELS),
)
def test_scope_to_one_workspace_never_references_another(workspaces, model) -> None:
    """For a distinct pair (w1 != w2), a statement scoped to w1 binds w1 and
    never w2 — one tenant's query cannot reference another tenant's id
    (Req 16.2)."""
    w1, w2 = workspaces
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=w1)
    stmt = apply_tenant_scope(select(model), model, ctx)

    bound = _bound_workspace_ids(stmt)
    assert w1 in bound
    assert w2 not in bound


# --- Property 29: fail-closed when there is no active workspace -------------

@_PBT
@given(model=st.sampled_from(_SCOPED_MODELS))
def test_scope_without_active_workspace_always_raises(model) -> None:
    """When active_workspace_id is None, apply_tenant_scope always raises
    TenantScopeError for any model (fail-closed, Req 16.1)."""
    ctx = RequestContext(user_id=uuid.uuid4(), active_workspace_id=None)
    try:
        apply_tenant_scope(select(model), model, ctx)
    except TenantScopeError:
        pass
    else:
        raise AssertionError("scoping with no active workspace was not refused")


# --- Property 29: a queued job re-applies the same tenant scope -------------

@_PBT
@given(job_workspace_id=st.uuids(), model=st.sampled_from(_SCOPED_MODELS))
def test_queued_job_reapplies_originating_workspace_scope(
    job_workspace_id, model
) -> None:
    """A job payload carries its originating workspace_id; re-applying the scope
    with a context built from that id yields a statement bound to the same
    workspace (Req 16.3)."""
    # Model the queued job payload as carrying its originating workspace id.
    job = {"workspace_id": job_workspace_id}

    ctx = RequestContext(
        user_id=uuid.uuid4(), active_workspace_id=job["workspace_id"]
    )
    stmt = apply_tenant_scope(select(model), model, ctx)

    bound = _bound_workspace_ids(stmt)
    assert job_workspace_id in bound
    assert all(v == job_workspace_id for v in bound)
