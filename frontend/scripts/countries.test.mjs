/**
 * Country list + phone helper tests (SMS notifications account page).
 *
 * Pure module — imported via Node's native TS type-stripping, mirroring the
 * other .mjs tests. Asserts the country lookup, E.164 validation (kept in sync
 * with the backend `is_valid_e164`), and the phone-input normalizer.
 */

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(__dirname, "..", "src", "lib", "countries.ts");
const {
  COUNTRIES,
  countryByCode,
  isValidE164,
  normalizePhoneInput,
  composeE164,
  localPartFromE164,
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

check("COUNTRIES is non-empty and well-formed", () => {
  assert.ok(COUNTRIES.length > 20);
  for (const c of COUNTRIES) {
    assert.match(c.code, /^[A-Z]{2}$/, `bad code ${c.code}`);
    assert.ok(typeof c.name === "string" && c.name.length > 0);
    assert.match(c.dialCode, /^\d{1,4}$/, `bad dialCode ${c.dialCode}`);
  }
});

check("countryByCode is case-insensitive and handles misses", () => {
  assert.equal(countryByCode("US")?.name, "United States");
  assert.equal(countryByCode("us")?.dialCode, "1");
  assert.equal(countryByCode("gb")?.code, "GB");
  assert.equal(countryByCode("ZZ"), undefined);
  assert.equal(countryByCode(null), undefined);
  assert.equal(countryByCode(undefined), undefined);
});

check("isValidE164 matches the backend contract", () => {
  assert.equal(isValidE164("+14155550123"), true);
  assert.equal(isValidE164("+2348012345678"), true);
  assert.equal(isValidE164("14155550123"), false); // no +
  assert.equal(isValidE164("+0155550123"), false); // leading 0
  assert.equal(isValidE164("+123"), false); // too short
  assert.equal(isValidE164(""), false);
  assert.equal(isValidE164(null), false);
  assert.equal(isValidE164("+1415555012a"), false); // non-digit
});

check("normalizePhoneInput strips spaces/dashes/parens but keeps +", () => {
  assert.equal(normalizePhoneInput("+1 (415) 555-0123"), "+14155550123");
  assert.equal(normalizePhoneInput("+44 20 7183 8750"), "+442071838750");
  assert.equal(normalizePhoneInput("  +1.415.555.0123 "), "+14155550123");
});

check("composeE164 joins the country dial code with a local number", () => {
  // The reported bug: Nigeria (+234) + "9131682271" must become E.164.
  assert.equal(composeE164("NG", "9131682271"), "+2349131682271");
  assert.ok(isValidE164(composeE164("NG", "9131682271")));
  // US local number.
  assert.equal(composeE164("US", "4155550123"), "+14155550123");
  // Formatting is stripped.
  assert.equal(composeE164("US", "(415) 555-0123"), "+14155550123");
});

check("composeE164 strips a leading national trunk 0", () => {
  // NG/GB users often write a leading 0 on the local number.
  assert.equal(composeE164("NG", "09131682271"), "+2349131682271");
  assert.equal(composeE164("GB", "07911123456"), "+447911123456");
});

check("composeE164 respects an already-full number", () => {
  assert.equal(composeE164("NG", "+2349131682271"), "+2349131682271");
  // Dial code typed without the +.
  assert.equal(composeE164("NG", "2349131682271"), "+2349131682271");
});

check("composeE164 handles empty + no-country gracefully", () => {
  assert.equal(composeE164("NG", ""), "");
  assert.equal(composeE164("", "9131682271"), "9131682271"); // no country -> as typed
  assert.equal(composeE164(null, "+14155550123"), "+14155550123");
});

check("localPartFromE164 inverts composeE164 for display", () => {
  assert.equal(localPartFromE164("NG", "+2349131682271"), "9131682271");
  assert.equal(localPartFromE164("US", "+14155550123"), "4155550123");
  // Mismatched country -> return full value unchanged.
  assert.equal(localPartFromE164("GB", "+14155550123"), "+14155550123");
  assert.equal(localPartFromE164("NG", ""), "");
  assert.equal(localPartFromE164("NG", null), "");
});

if (failures.length > 0) {
  console.error(`\n✗ countries FAILED — ${failures.length} assertion(s):\n`);
  for (const f of failures) console.error(`  - ${f.name}\n      ${f.message}`);
  process.exit(1);
}
console.log(
  `✓ countries passed — ${passed} checks: country list is well-formed, ` +
    `lookup is case-insensitive, E.164 validation matches the backend, and ` +
    `phone normalization strips formatting.`,
);
