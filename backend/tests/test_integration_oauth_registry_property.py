"""Property-based tests for the OAuth_Provider_Registry (task 1.2).

These exercise the pure, side-effect-free registry in
``app.services.integration_oauth`` — the frozen ``OAuthProvider`` dataclass,
the ``OAUTH_PROVIDERS`` mapping, and the two helpers ``is_oauth_family`` and
``lookup``. No I/O, no database, no ORM row: the registry is data + classifier
only, so the invariants below are checked directly against ``OAUTH_PROVIDERS``
and the doc-verified provider families.

Feature: integration-oauth-flow, Property 18: Registry classification is exact
and entries are well-formed — for any listed OAuth-family slug ``is_oauth_family``
is true and ``lookup`` returns an entry with a non-empty ``authorize_url``, a
non-empty ``token_url``, and at least one scope; for any excluded provider slug
``is_oauth_family`` is false and the slug is absent from the registry.

Feature: integration-oauth-flow, Property 12: Refresh-token parameters are set
per provider family — for any Google-family or Zoho registry entry,
``extra_authorize_params`` contains ``access_type=offline`` and
``prompt=consent``; for any Microsoft-family entry, ``offline_access`` is present
in the scopes.

Validates: Requirements 5.1, 5.2, 9.1, 9.2, 9.3.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.errors import APIError
from app.services.integration_oauth import (
    OAUTH_PROVIDERS,
    OAuthProvider,
    is_oauth_family,
    lookup,
)

# --- Family definitions (Requirement 9.1 / design Data Models) ---------------
#
# The 21 OAuth-family slugs, grouped by the provider family that determines how
# a refresh token is forced (Property 12). Google-family + Zoho carry
# access_type=offline + prompt=consent; Microsoft/Entra-family carry the
# offline_access scope.

GOOGLE_FAMILY_SLUGS: tuple[str, ...] = (
    "gmail",
    "google_calendar",
    "google_drive",
    "google_docs",
    "google_sheets",
    "google_slides",
    "google_meet",
    "gcp",
)

ZOHO_SLUGS: tuple[str, ...] = ("zoho_crm",)

MICROSOFT_FAMILY_SLUGS: tuple[str, ...] = (
    "outlook",
    "outlook_calendar",
    "onedrive",
    "teams",
    "word",
    "excel",
    "powerpoint",
)

OTHER_OAUTH_SLUGS: tuple[str, ...] = (
    "salesforce",
    "quickbooks",
    "xero",
    "yahoo",
    "workday",
)

# Every OAuth-family slug enumerated in Requirement 9.1.
OAUTH_FAMILY_SLUGS: tuple[str, ...] = (
    *GOOGLE_FAMILY_SLUGS,
    *MICROSOFT_FAMILY_SLUGS,
    *OTHER_OAUTH_SLUGS,
    *ZOHO_SLUGS,
)

# Providers that are explicitly NOT OAuth-authorization-code (Requirement 9.2 /
# design "NOT OAuth-authorization-code (excluded)"): API-key, bot-token,
# key+secret / signed, basic-auth, and client-credentials-only providers.
EXCLUDED_SLUGS: tuple[str, ...] = (
    # API-key providers
    "stripe",
    "sendgrid",
    "notion",
    "linear",
    "cloudflare",
    # bot-token providers
    "slack",
    "discord",
    "telegram",
    "line",
    # key+secret / signed providers
    "aws",
    "twilio_sms",
    "netsuite",
    # basic-auth providers
    "jira",
    "confluence",
    "bitbucket",
    "imap_smtp",
    # client-credentials-only providers
    "zoom",
    "sap",
)


def test_registry_enumerates_exactly_the_required_oauth_family() -> None:
    """The registry keys are exactly the 21 slugs required by Requirement 9.1.

    Validates: Requirements 9.1.
    """
    assert set(OAUTH_PROVIDERS.keys()) == set(OAUTH_FAMILY_SLUGS)
    # No accidental duplicate slugs in the family fixture itself.
    assert len(OAUTH_FAMILY_SLUGS) == len(set(OAUTH_FAMILY_SLUGS))
    assert len(OAUTH_FAMILY_SLUGS) == 21


# --- Property 18: classification is exact and entries are well-formed --------


@settings(max_examples=200)
@given(slug=st.sampled_from(OAUTH_FAMILY_SLUGS))
def test_listed_slug_is_family_and_entry_is_well_formed(slug: str) -> None:
    """Property 18: every listed OAuth-family slug is classified and well-formed.

    Feature: integration-oauth-flow, Property 18: Registry classification is
    exact and entries are well-formed.

    Validates: Requirements 9.1, 9.2, 9.3.
    """
    assert is_oauth_family(slug) is True

    entry = lookup(slug)
    assert isinstance(entry, OAuthProvider)
    # The looked-up entry describes the requested slug.
    assert entry.slug == slug
    # Non-empty authorize URL, non-empty token URL, >= 1 scope (Req 9.3).
    assert entry.authorize_url
    assert entry.authorize_url.strip() != ""
    assert entry.token_url
    assert entry.token_url.strip() != ""
    assert len(entry.scopes) >= 1
    assert all(scope.strip() != "" for scope in entry.scopes)


@settings(max_examples=200)
@given(slug=st.sampled_from(EXCLUDED_SLUGS))
def test_excluded_slug_is_not_family_and_absent(slug: str) -> None:
    """Property 18: excluded providers are not OAuth-family and absent from registry.

    ``is_oauth_family`` is false, the slug is not a registry key, and ``lookup``
    raises the ``APIError`` that maps to HTTP 400 (Requirement 9.4 mapping).

    Feature: integration-oauth-flow, Property 18: Registry classification is
    exact and entries are well-formed.

    Validates: Requirements 9.2.
    """
    assert is_oauth_family(slug) is False
    assert slug not in OAUTH_PROVIDERS

    try:
        lookup(slug)
    except APIError as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover - a raised APIError is the only correct outcome
        raise AssertionError(f"lookup({slug!r}) should have raised APIError")


@settings(max_examples=300)
@given(
    slug=st.text(min_size=0, max_size=40).filter(
        lambda s: s not in OAUTH_PROVIDERS
    )
)
def test_arbitrary_unlisted_slug_is_not_family(slug: str) -> None:
    """Property 18: any slug absent from the registry is never OAuth-family.

    The classifier is exactly membership in ``OAUTH_PROVIDERS`` — nothing outside
    the registry is ever classified as OAuth-family, and ``lookup`` refuses it.

    Validates: Requirements 9.2.
    """
    assert is_oauth_family(slug) is False

    try:
        lookup(slug)
    except APIError as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover
        raise AssertionError(f"lookup({slug!r}) should have raised APIError")


def test_is_oauth_family_matches_registry_membership_over_all_known_slugs() -> None:
    """Property 18: classification == registry membership across family + excluded.

    Validates: Requirements 9.1, 9.2.
    """
    for slug in OAUTH_FAMILY_SLUGS:
        assert is_oauth_family(slug) is (slug in OAUTH_PROVIDERS) is True
    for slug in EXCLUDED_SLUGS:
        assert is_oauth_family(slug) is (slug in OAUTH_PROVIDERS) is False


# --- Property 12: refresh-token parameters are set per provider family -------


@settings(max_examples=200)
@given(slug=st.sampled_from(GOOGLE_FAMILY_SLUGS + ZOHO_SLUGS))
def test_google_and_zoho_carry_offline_consent_params(slug: str) -> None:
    """Property 12: Google-family/Zoho entries force offline access + consent.

    ``extra_authorize_params`` contains ``access_type=offline`` and
    ``prompt=consent`` so the provider reliably returns a refresh token.

    Feature: integration-oauth-flow, Property 12: Refresh-token parameters are
    set per provider family.

    Validates: Requirements 5.1.
    """
    params = OAUTH_PROVIDERS[slug].extra_authorize_params
    assert params.get("access_type") == "offline"
    assert params.get("prompt") == "consent"


@settings(max_examples=200)
@given(slug=st.sampled_from(MICROSOFT_FAMILY_SLUGS))
def test_microsoft_family_carries_offline_access_scope(slug: str) -> None:
    """Property 12: Microsoft-family entries include the offline_access scope.

    The ``offline_access`` scope is what yields a refresh token in the
    Microsoft/Entra flow.

    Feature: integration-oauth-flow, Property 12: Refresh-token parameters are
    set per provider family.

    Validates: Requirements 5.2.
    """
    assert "offline_access" in OAUTH_PROVIDERS[slug].scopes
