"""Verified provider API path hints keep the agent from guessing REST paths."""

from app.services import provider_api


def test_gmail_hint_has_correct_verified_paths():
    hint = provider_api.api_hint("gmail")
    assert hint  # present
    # The exact Gmail paths that a 404 earlier proved the agent must not guess.
    assert "/gmail/v1/users/me/messages" in hint
    assert "/gmail/v1/users/me/messages/send" in hint
    assert "/gmail/v1/users/me/drafts" in hint


def test_unknown_provider_hint_is_empty():
    assert provider_api.api_hint("does_not_exist") == ""


def test_hint_providers_are_all_known_providers():
    # Every hinted provider must have a real API profile (no orphan hints).
    for name in provider_api.API_HINTS:
        assert name in provider_api.PROVIDER_API, f"{name} hint has no ApiProfile"


def test_api_hints_cover_every_provider():
    # Every provider with an API profile must have a non-empty guidance hint so
    # the agent never has to guess REST paths or build fragmentary actions.
    missing = set(provider_api.PROVIDER_API) - set(provider_api.API_HINTS)
    assert not missing, f"providers missing an API hint: {sorted(missing)}"
    for name in provider_api.PROVIDER_API:
        hint = provider_api.api_hint(name)
        assert hint and hint.strip(), f"{name} has an empty API hint"


def test_graphql_providers_mention_graphql_single_endpoint():
    # monday_* and linear are GraphQL — the hint must steer to a single POST.
    for name in ("linear", "monday_dev", "monday_crm", "monday_service",
                 "monday_workdocs", "monday_workspaces"):
        hint = provider_api.api_hint(name).lower()
        assert "graphql" in hint, f"{name} hint should mention GraphQL"


def test_gmail_hint_insists_on_complete_reply():
    # Guard against regressing to empty-body sends: RFC 2822 full-reply guidance.
    hint = provider_api.api_hint("gmail")
    assert "RFC 2822" in hint
    assert "In-Reply-To" in hint
