/**
 * Property 20 verification — OAuth-family field visibility.
 *
 * Feature: integration-oauth-flow, Property 20: OAuth-family providers hide
 * token inputs but keep client fields. Validates: Requirements 12.1, 12.2.
 *
 * The integrations connect form (src/app/dashboard/integrations/page.tsx)
 * derives its visible credential fields through `filterOAuthFields` from
 * src/lib/oauth-providers.ts. This test exercises that pure helper — the exact
 * same code path the page uses — to assert that:
 *
 *   - For every OAuth-family provider, the manual `refresh_token` and
 *     `access_token` inputs are hidden (Req 12.1), while `client_id` /
 *     `client_secret` (and any other non-token field) remain visible (Req 12.2).
 *   - For non-OAuth-family providers, the field list is returned unchanged.
 *
 * Framework: this Next.js repo ships no JS test runner (see package.json); the
 * established convention is standalone Node scripts under frontend/scripts/
 * (e.g. check-guide-links.mjs). We follow that convention and rely on Node's
 * native TypeScript type-stripping (Node >=22.6) to import the real .ts module.
 *
 * Usage:  node scripts/oauth-field-visibility.test.mjs
 * Exit code is non-zero on any failed assertion so it can gate CI.
 */

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(
  __dirname,
  "..",
  "src",
  "lib",
  "oauth-providers.ts",
);

const {
  OAUTH_FAMILY_SLUGS,
  isOAuthFamily,
  filterOAuthFields,
  OAUTH_MANAGED_TOKEN_FIELDS,
} = await import(MODULE_PATH);

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push({ name, message: err?.message ?? String(err) });
  }
}

// A realistic field list an OAuth-family provider would expose: the two
// OAuth-managed token inputs plus the client credentials the server-side
// exchange needs, plus an unrelated non-secret config field.
const SAMPLE_FIELDS = [
  { name: "client_id", secret: false, required: true },
  { name: "client_secret", secret: true, required: true },
  { name: "refresh_token", secret: true, required: false },
  { name: "access_token", secret: true, required: false },
  { name: "tenant_id", secret: false, required: false },
];

const HIDDEN = new Set(OAUTH_MANAGED_TOKEN_FIELDS);
const KEEP = ["client_id", "client_secret", "tenant_id"];

// Property 20 (core): every OAuth-family provider hides the token inputs but
// keeps the client fields.
for (const slug of OAUTH_FAMILY_SLUGS) {
  check(`OAuth-family '${slug}' hides token inputs, keeps client fields`, () => {
    assert.equal(isOAuthFamily(slug), true, `${slug} should be OAuth-family`);

    const visible = filterOAuthFields(slug, SAMPLE_FIELDS);
    const visibleNames = visible.map((f) => f.name);

    // Token inputs are hidden (Req 12.1).
    for (const hidden of HIDDEN) {
      assert.ok(
        !visibleNames.includes(hidden),
        `${slug}: '${hidden}' must be hidden for OAuth-family providers`,
      );
    }

    // Client credentials (and other non-token fields) remain visible (Req 12.2).
    for (const keep of KEEP) {
      assert.ok(
        visibleNames.includes(keep),
        `${slug}: '${keep}' must remain visible for OAuth-family providers`,
      );
    }
  });
}

// Property 20 (converse): non-OAuth-family providers keep their full field
// list, including manual token inputs.
for (const slug of ["slack", "notion", "github", "hubspot", "not_a_provider"]) {
  check(`non-OAuth-family '${slug}' keeps all fields including tokens`, () => {
    assert.equal(isOAuthFamily(slug), false, `${slug} should NOT be OAuth-family`);
    const visible = filterOAuthFields(slug, SAMPLE_FIELDS);
    assert.deepEqual(
      visible.map((f) => f.name),
      SAMPLE_FIELDS.map((f) => f.name),
      `${slug}: non-OAuth-family field list must be unchanged`,
    );
  });
}

// Ordering / stability: the helper must not reorder retained fields.
check("retained fields preserve their original order", () => {
  const visible = filterOAuthFields("gmail", SAMPLE_FIELDS);
  assert.deepEqual(visible.map((f) => f.name), ["client_id", "client_secret", "tenant_id"]);
});

if (failures.length > 0) {
  console.error(`\n✗ Property 20 FAILED — ${failures.length} assertion(s):\n`);
  for (const f of failures) {
    console.error(`  - ${f.name}\n      ${f.message}`);
  }
  process.exit(1);
}

console.log(
  `✓ Property 20 passed — ${passed} checks across ` +
    `${OAUTH_FAMILY_SLUGS.length} OAuth-family providers ` +
    `(token inputs hidden, client fields kept).`,
);
