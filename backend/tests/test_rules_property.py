"""Property-based tests for the applicable-rule resolver.

Exercises the pure resolver in :mod:`app.services.rules_service`
(:func:`rule_matches` / :func:`applicable_rules`), which selects the rules that
apply to a given :class:`~app.services.rules_service.RuleContext`.

Resolver semantics under test (see the module docstring in
``rules_service.py``):

- Inactive rules never apply (Req 8.3).
- Active workspace-wide rules always apply, regardless of context (Req 8.2).
- Otherwise an active rule applies iff its ``category`` equals the context's
  ``category`` and either it is not provider-scoped (``provider_name is None``)
  or its ``provider_name`` equals the context's ``provider_name`` (Req 8.4).

Property 21: Applicable-rule resolution.
**Validates: Requirements 8.2, 8.3, 8.4**

Each property runs at least 100 examples per the design's property-testing
budget (configured here at 200).
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.rules_service import (
    RuleContext,
    applicable_rules,
    rule_matches,
)


# ---------------------------------------------------------------------------
# Fake rule + strategies
# ---------------------------------------------------------------------------


@dataclass
class FakeRule:
    """In-memory stand-in exposing exactly the attributes the resolver reads.

    Structurally satisfies the ``RuleApplies`` protocol so the pure resolver can
    operate on it without a database.
    """

    is_active: bool
    is_workspace_wide: bool
    category: str
    provider_name: str | None


_categories = st.sampled_from(["email", "calendar", "crm"])
_providers = st.sampled_from(["gmail", "outlook", None])

_rules = st.builds(
    FakeRule,
    is_active=st.booleans(),
    is_workspace_wide=st.booleans(),
    category=_categories,
    provider_name=_providers,
)

_rule_lists = st.lists(_rules, max_size=12)

_contexts = st.builds(RuleContext, category=_categories, provider_name=_providers)


def _expected(rule: FakeRule, ctx: RuleContext) -> bool:
    """Independent re-implementation of the resolver predicate."""
    return rule.is_active and (
        rule.is_workspace_wide
        or (
            rule.category == ctx.category
            and (rule.provider_name is None or rule.provider_name == ctx.provider_name)
        )
    )


# ---------------------------------------------------------------------------
# Property 21: Applicable-rule resolution
# ---------------------------------------------------------------------------


@given(rules=_rule_lists, ctx=_contexts)
@settings(max_examples=200)
def test_applicable_rules_matches_independent_predicate(rules, ctx):
    """Output equals the independently-computed expected list, order preserved.

    **Validates: Requirements 8.2, 8.3, 8.4**
    """
    expected = [rule for rule in rules if _expected(rule, ctx)]
    assert applicable_rules(rules, ctx) == expected


@given(rules=_rule_lists, ctx=_contexts)
@settings(max_examples=200)
def test_every_returned_rule_is_active(rules, ctx):
    """No inactive rule is ever returned.

    **Validates: Requirements 8.3**
    """
    for rule in applicable_rules(rules, ctx):
        assert rule.is_active


@given(rules=_rule_lists, ctx=_contexts)
@settings(max_examples=200)
def test_active_workspace_wide_rules_always_included(rules, ctx):
    """Every active workspace-wide input rule appears in the output.

    **Validates: Requirements 8.2**
    """
    result = applicable_rules(rules, ctx)
    for rule in rules:
        if rule.is_active and rule.is_workspace_wide:
            assert rule in result


@given(rules=_rule_lists, ctx=_contexts)
@settings(max_examples=200)
def test_returned_non_workspace_wide_rules_match_context(rules, ctx):
    """Returned non-workspace-wide rules match the context category/provider.

    **Validates: Requirements 8.4**
    """
    for rule in applicable_rules(rules, ctx):
        if not rule.is_workspace_wide:
            assert rule.category == ctx.category
            assert rule.provider_name is None or rule.provider_name == ctx.provider_name


@given(rules=_rule_lists, ctx=_contexts)
@settings(max_examples=200)
def test_output_is_subsequence_of_input(rules, ctx):
    """Output is a subsequence of the input: order preserved, nothing invented.

    **Validates: Requirements 8.2, 8.3, 8.4**
    """
    result = applicable_rules(rules, ctx)
    # Walk the input once, matching output items in order.
    it = iter(rules)
    for produced in result:
        assert any(produced is candidate for candidate in it), (
            "output rule not found in remaining input (order violated or invented)"
        )
    # Also confirm consistency with the single-rule predicate.
    assert result == [rule for rule in rules if rule_matches(rule, ctx)]
