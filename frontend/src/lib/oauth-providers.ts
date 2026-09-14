/**
 * OAuth-family provider classifier (mirrors the backend registry).
 *
 * This set is the authoritative frontend copy of the OAuth-authorization-code
 * provider family, derived from the backend registry
 * `backend/app/services/integration_oauth.py` (`OAUTH_PROVIDERS` keys). Only
 * these providers support the in-app OAuth authorize flow; every other provider
 * keeps its manual credential form.
 *
 * It drives field visibility on the integrations page: for an OAuth-family
 * provider the manual `refresh_token` / `access_token` inputs are hidden while
 * `client_id` / `client_secret` remain collected (Requirement 12.1/12.2).
 *
 * DO NOT hand-edit entries out of sync with the backend: a backend parity test
 * asserts this set exactly matches `OAUTH_PROVIDERS` (Requirement 9.1/12.2).
 * Keeping it static (like `trigger-badges.ts`) avoids an extra round-trip and
 * renders identically in local dev and Docker.
 */

/**
 * The 21 OAuth-authorization-code provider slugs (Requirement 9.1), matching the
 * keys of the backend `OAUTH_PROVIDERS` registry exactly.
 */
export const OAUTH_FAMILY_SLUGS: readonly string[] = [
  // ---- Google family ----
  "gmail",
  "google_calendar",
  "google_drive",
  "google_docs",
  "google_sheets",
  "google_slides",
  "google_meet",
  "gcp",
  // ---- Microsoft/Entra family ----
  "outlook",
  "outlook_calendar",
  "onedrive",
  "teams",
  "word",
  "excel",
  "powerpoint",
  // ---- Other OAuth-authorization-code providers ----
  "salesforce",
  "zoho_crm",
  "quickbooks",
  "xero",
  "yahoo",
  "workday",
] as const;

/** Fast membership set built once from {@link OAUTH_FAMILY_SLUGS}. */
const OAUTH_FAMILY_SET: ReadonlySet<string> = new Set(OAUTH_FAMILY_SLUGS);

/**
 * Return true when `slug` is an OAuth-family provider (Requirement 9.1/9.2).
 *
 * Mirrors the backend `is_oauth_family`: API-key, bot-token, basic-auth, and
 * client-credentials-only providers are not classified as OAuth-family.
 */
export function isOAuthFamily(slug: string): boolean {
  return OAUTH_FAMILY_SET.has(slug);
}

/**
 * Credential-field names obtained through the in-app OAuth authorize flow
 * rather than typed by hand. For OAuth-family providers these manual inputs are
 * hidden while the client credentials the server-side exchange needs are kept
 * (Requirement 12.1/12.2).
 */
export const OAUTH_MANAGED_TOKEN_FIELDS: readonly string[] = [
  "refresh_token",
  "access_token",
] as const;

/**
 * Compute the visible credential fields for a provider's connect form.
 *
 * For OAuth-family providers, the manual `refresh_token` / `access_token`
 * inputs are removed (they are obtained via the authorize flow) while every
 * other field — notably `client_id` / `client_secret` — is preserved. For
 * non-OAuth-family providers the field list is returned unchanged
 * (Requirement 12.1/12.2).
 *
 * Generic over the field shape so it can drive the page's `CredentialFieldSpec`
 * list while staying trivially unit-testable with plain objects.
 */
export function filterOAuthFields<T extends { name: string }>(
  slug: string,
  fields: readonly T[],
): T[] {
  if (!isOAuthFamily(slug)) return [...fields];
  return fields.filter((f) => !OAUTH_MANAGED_TOKEN_FIELDS.includes(f.name));
}
