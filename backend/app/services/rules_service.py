"""Automation rules service (task 10.1).

Implements the ``Rules_Service`` operations from the design (see design.md
"Rules_Service"):

- **Persistence** — :func:`create_rule`, :func:`update_rule`,
  :func:`list_rules`, and :func:`delete_rule` persist and manage a
  :class:`~app.db.models.Rule` with its ``workspace_id``,
  ``created_by_user_id``, ``category``, ``provider_name`` (nullable),
  ``rule_prompt``, ``is_workspace_wide``, and ``is_active`` attributes
  (Req 8.1). The caller owns the surrounding transaction: these functions
  ``flush`` (so generated ids/defaults are populated) but never ``commit``.
  Not-found conditions raise :class:`~app.core.errors.APIError` (404).

- **Applicable-rule resolver** — :func:`applicable_rules` is a *pure*,
  DB-free function that, given an iterable of Rule-like objects and a
  :class:`RuleContext` describing the current execution's ``category`` and
  ``provider_name``, returns the rules that apply to that execution
  (Req 8.2, 8.3, 8.4). Keeping it pure lets task 10.2 (Property 21)
  property-test it without a database.

Resolver semantics (precise):

- A rule with ``is_active == False`` **never** applies — it is excluded
  regardless of any other attribute (Req 8.3).
- A rule with ``is_workspace_wide == True`` applies to **every** execution in
  the workspace, regardless of the execution's category/provider (Req 8.2).
  This holds even when such a rule also carries a ``category``/
  ``provider_name`` — workspace-wide always wins, so the rule is included.
- Otherwise (an active, non-workspace-wide rule), the rule applies only when
  its ``category`` equals the execution's ``category`` **and** either the rule
  is not scoped to a provider (``provider_name is None``) or its
  ``provider_name`` equals the execution's ``provider_name`` (Req 8.4). A
  provider-scoped rule therefore applies only to that provider; a rule with
  ``provider_name is None`` applies to the whole matching category.

The :class:`RuleApplies` protocol captures exactly the attributes the resolver
reads (``is_active``, ``is_workspace_wide``, ``category``, ``provider_name``),
so the resolver works against ORM :class:`~app.db.models.Rule` rows and against
lightweight in-memory fakes used by the property test alike.

Requirements: 8.1, 8.2, 8.3, 8.4.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.db.models import Rule

# ---------------------------------------------------------------------------
# Pure applicable-rule resolver (DB-free)
# ---------------------------------------------------------------------------


@runtime_checkable
class RuleApplies(Protocol):
    """Structural protocol for the attributes the resolver reads.

    Any object exposing these four attributes can be resolved, so the pure
    resolver works uniformly against ORM :class:`~app.db.models.Rule` rows and
    against in-memory fakes (e.g. from the Property 21 test).
    """

    is_active: bool
    is_workspace_wide: bool
    category: str
    provider_name: str | None


@dataclass(frozen=True)
class RuleContext:
    """The execution context a rule set is resolved against (pure).

    Attributes:
        category: The category of the current agent execution.
        provider_name: The provider of the current execution, or ``None`` when
            the execution is not scoped to a specific provider.
    """

    category: str
    provider_name: str | None = None


def rule_matches(rule: RuleApplies, ctx: RuleContext) -> bool:
    """Return whether ``rule`` applies to execution context ``ctx`` (pure).

    Encodes the full resolver semantics for a single rule (see module docstring):

    - Inactive rules never apply (Req 8.3).
    - Workspace-wide rules always apply (Req 8.2), even if they also carry a
      category/provider.
    - Otherwise the rule's ``category`` must equal ``ctx.category`` and, when the
      rule is scoped to a provider, its ``provider_name`` must equal
      ``ctx.provider_name`` (Req 8.4).
    """
    if not rule.is_active:
        return False
    if rule.is_workspace_wide:
        return True
    if rule.category != ctx.category:
        return False
    return rule.provider_name is None or rule.provider_name == ctx.provider_name


def applicable_rules(
    rules: Iterable[RuleApplies], ctx: RuleContext
) -> list[RuleApplies]:
    """Return the rules from ``rules`` that apply to context ``ctx`` (pure).

    Preserves the input order and includes exactly the active rules that are
    workspace-wide or match the context's category (and provider, when the rule
    is provider-scoped). No inactive rule, and no non-matching non-workspace-wide
    rule, is ever included (Req 8.2, 8.3, 8.4).
    """
    return [rule for rule in rules if rule_matches(rule, ctx)]


# ---------------------------------------------------------------------------
# Persistence operations (async, DB-backed)
# ---------------------------------------------------------------------------


async def create_rule(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    category: str,
    rule_prompt: str,
    provider_name: str | None = None,
    is_workspace_wide: bool = False,
    is_active: bool = True,
) -> Rule:
    """Persist a new :class:`~app.db.models.Rule` (Req 8.1).

    Persists all rule attributes bound to the target workspace and creating
    user. The caller owns the surrounding transaction: this flushes so the
    generated ``id``/``created_at`` are populated but does not ``commit``.

    Args:
        session: The active async session/transaction.
        workspace_id: The owning workspace.
        created_by_user_id: The user creating the rule.
        category: The rule's category (plain string).
        rule_prompt: The rule's instruction text.
        provider_name: The provider the rule is scoped to, or ``None`` for the
            whole category.
        is_workspace_wide: Whether the rule applies to every execution.
        is_active: Whether the rule is active.

    Returns:
        The persisted :class:`~app.db.models.Rule`.
    """
    rule = Rule(
        workspace_id=workspace_id,
        created_by_user_id=created_by_user_id,
        category=category,
        provider_name=provider_name,
        rule_prompt=rule_prompt,
        is_workspace_wide=is_workspace_wide,
        is_active=is_active,
    )
    session.add(rule)
    await session.flush()
    return rule


async def list_rules(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
) -> list[Rule]:
    """Return all rules for a workspace, oldest first (Req 8.1).

    The query is filtered by ``workspace_id`` so a caller only ever sees rules
    of the target workspace.
    """
    result = await session.scalars(
        select(Rule)
        .where(Rule.workspace_id == workspace_id)
        .order_by(Rule.created_at, Rule.id)
    )
    return list(result.all())


async def update_rule(
    session: AsyncSession,
    *,
    rule_id: uuid.UUID,
    workspace_id: uuid.UUID,
    category: str | None = None,
    provider_name: str | None = None,
    rule_prompt: str | None = None,
    is_workspace_wide: bool | None = None,
    is_active: bool | None = None,
    update_provider_name: bool = False,
) -> Rule:
    """Update mutable attributes of a rule within a workspace (Req 8.1).

    Only the fields passed as non-``None`` are updated. Because ``None`` is a
    meaningful value for ``provider_name`` (clearing the provider scope), pass
    ``update_provider_name=True`` to apply ``provider_name`` (including setting
    it to ``None``); otherwise ``provider_name`` is left unchanged.

    The lookup is scoped by ``workspace_id`` so a rule from another workspace is
    never mutated (tenant isolation); a missing rule raises 404. The caller owns
    the transaction (flush, no commit).

    Raises:
        APIError: 404 if no matching rule exists in the workspace.
    """
    rule = await session.scalar(
        select(Rule).where(
            Rule.id == rule_id,
            Rule.workspace_id == workspace_id,
        )
    )
    if rule is None:
        raise APIError(status_code=404, code="not_found", message="Rule not found.")

    if category is not None:
        rule.category = category
    if update_provider_name:
        rule.provider_name = provider_name
    if rule_prompt is not None:
        rule.rule_prompt = rule_prompt
    if is_workspace_wide is not None:
        rule.is_workspace_wide = is_workspace_wide
    if is_active is not None:
        rule.is_active = is_active

    await session.flush()
    return rule


async def delete_rule(
    session: AsyncSession,
    *,
    rule_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Delete a rule within a workspace (Req 8.1).

    The lookup is scoped by ``workspace_id`` so a caller can only delete rules of
    the target workspace; a missing rule raises 404. The caller owns the
    transaction (flush, no commit).

    Raises:
        APIError: 404 if no matching rule exists in the workspace.
    """
    rule = await session.scalar(
        select(Rule).where(
            Rule.id == rule_id,
            Rule.workspace_id == workspace_id,
        )
    )
    if rule is None:
        raise APIError(status_code=404, code="not_found", message="Rule not found.")

    await session.delete(rule)
    await session.flush()


__all__ = [
    "RuleApplies",
    "RuleContext",
    "rule_matches",
    "applicable_rules",
    "create_rule",
    "list_rules",
    "update_rule",
    "delete_rule",
]
