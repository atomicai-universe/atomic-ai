/**
 * Country list for the phone-number country selector (SMS notifications).
 *
 * Each entry pairs an ISO 3166-1 alpha-2 code with a display name and the E.164
 * dialing-code prefix. Kept as a small, dependency-free, pure module so the
 * account page can render a dropdown and a `.mjs` test can assert lookups
 * without a DOM. The set mirrors the backend's priced destinations
 * (`app.services.sns_service.SMS_PRICE_USD_BY_COUNTRY`); unknown numbers still
 * work (the backend falls back to a default rate).
 */

export interface Country {
  /** ISO 3166-1 alpha-2 code, e.g. "US". */
  code: string;
  /** Display name, e.g. "United States". */
  name: string;
  /** E.164 dialing prefix WITHOUT the leading "+", e.g. "1". */
  dialCode: string;
}

export const COUNTRIES: Country[] = [
  { code: "US", name: "United States", dialCode: "1" },
  { code: "CA", name: "Canada", dialCode: "1" },
  { code: "GB", name: "United Kingdom", dialCode: "44" },
  { code: "IE", name: "Ireland", dialCode: "353" },
  { code: "NG", name: "Nigeria", dialCode: "234" },
  { code: "GH", name: "Ghana", dialCode: "233" },
  { code: "KE", name: "Kenya", dialCode: "254" },
  { code: "ZA", name: "South Africa", dialCode: "27" },
  { code: "IN", name: "India", dialCode: "91" },
  { code: "AU", name: "Australia", dialCode: "61" },
  { code: "NZ", name: "New Zealand", dialCode: "64" },
  { code: "DE", name: "Germany", dialCode: "49" },
  { code: "FR", name: "France", dialCode: "33" },
  { code: "ES", name: "Spain", dialCode: "34" },
  { code: "IT", name: "Italy", dialCode: "39" },
  { code: "NL", name: "Netherlands", dialCode: "31" },
  { code: "PT", name: "Portugal", dialCode: "351" },
  { code: "BE", name: "Belgium", dialCode: "32" },
  { code: "CH", name: "Switzerland", dialCode: "41" },
  { code: "AT", name: "Austria", dialCode: "43" },
  { code: "SE", name: "Sweden", dialCode: "46" },
  { code: "NO", name: "Norway", dialCode: "47" },
  { code: "DK", name: "Denmark", dialCode: "45" },
  { code: "FI", name: "Finland", dialCode: "358" },
  { code: "PL", name: "Poland", dialCode: "48" },
  { code: "UA", name: "Ukraine", dialCode: "380" },
  { code: "TR", name: "Türkiye", dialCode: "90" },
  { code: "AE", name: "United Arab Emirates", dialCode: "971" },
  { code: "SA", name: "Saudi Arabia", dialCode: "966" },
  { code: "IL", name: "Israel", dialCode: "972" },
  { code: "BR", name: "Brazil", dialCode: "55" },
  { code: "MX", name: "Mexico", dialCode: "52" },
  { code: "AR", name: "Argentina", dialCode: "54" },
  { code: "JP", name: "Japan", dialCode: "81" },
  { code: "KR", name: "South Korea", dialCode: "82" },
  { code: "CN", name: "China", dialCode: "86" },
  { code: "SG", name: "Singapore", dialCode: "65" },
  { code: "MY", name: "Malaysia", dialCode: "60" },
  { code: "PH", name: "Philippines", dialCode: "63" },
  { code: "ID", name: "Indonesia", dialCode: "62" },
  { code: "TH", name: "Thailand", dialCode: "66" },
];

/** Look up a country by ISO code (case-insensitive), or undefined. */
export function countryByCode(code: string | null | undefined): Country | undefined {
  if (!code) return undefined;
  const upper = code.toUpperCase();
  return COUNTRIES.find((c) => c.code === upper);
}

/**
 * Basic E.164 check: "+" then 7–15 digits, first digit non-zero. Mirrors the
 * backend `is_valid_e164` so the UI can validate before calling the API. Pure.
 */
export function isValidE164(phone: string | null | undefined): boolean {
  if (!phone) return false;
  return /^\+[1-9]\d{6,14}$/.test(phone);
}

/** Strip spaces/hyphens/parens from a typed phone number (keep leading "+"). */
export function normalizePhoneInput(raw: string): string {
  return raw.replace(/[\s()\-.]/g, "");
}

/**
 * Combine a selected country's dial code with a typed phone number into E.164.
 *
 * The country dropdown already carries the "+<dialCode>" (e.g. Nigeria +234),
 * so the user types only the local part ("9131682271"). This joins them:
 *
 *   - Empty input -> "" (cleared).
 *   - Input already starting with "+" -> used as-is (user typed full E.164).
 *   - Input starting with the country's dial code but no "+" ("2349131...") ->
 *     just prepend "+".
 *   - Otherwise -> strip a single leading national trunk "0" (common in NG/GB/
 *     many countries) and prepend "+<dialCode>".
 *
 * When no country is selected, a bare number can't be dialed, so the input is
 * returned normalized (and E.164 validation will require the user to add "+").
 * Pure.
 */
export function composeE164(
  countryCode: string | null | undefined,
  rawInput: string,
): string {
  const input = normalizePhoneInput(rawInput || "");
  if (input === "") return "";
  if (input.startsWith("+")) return input;

  const country = countryByCode(countryCode);
  if (!country) {
    // No country context: can't know the dial code — leave as typed.
    return input;
  }

  const dial = country.dialCode;
  const digits = input.replace(/\D/g, "");
  if (digits.startsWith(dial)) {
    // User typed the dial code without the "+".
    return `+${digits}`;
  }
  // Strip a single leading national trunk "0" then prepend the dial code.
  const local = digits.replace(/^0+/, "");
  return `+${dial}${local}`;
}

/**
 * Given a stored E.164 number and a country, return the LOCAL part to show in
 * the input (the digits after the country's dial code). When the number does
 * not start with the country's dial code (or no country is known), the full
 * E.164 is returned so nothing is lost. Pure — inverse of {@link composeE164}.
 */
export function localPartFromE164(
  countryCode: string | null | undefined,
  e164: string | null | undefined,
): string {
  const value = (e164 || "").trim();
  if (value === "") return "";
  const country = countryByCode(countryCode);
  if (country && value.startsWith(`+${country.dialCode}`)) {
    return value.slice(country.dialCode.length + 1);
  }
  return value;
}
