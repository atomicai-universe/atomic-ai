"use client";

/**
 * Account settings page — profile + SMS reply-approval notifications.
 *
 * Lets the signed-in user add/update the phone number that receives an SMS
 * (via Amazon SNS) whenever a reply awaits their approval, choose the number's
 * country (used to estimate SMS cost by Amazon SNS pricing), toggle
 * notifications on/off, and see their SMS usage + estimated spend broken down by
 * country. All calls go through the authenticated API client (`/api/v1/profile`
 * and `/api/v1/profile/sms-usage`); the phone number is only ever the user's own.
 */

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  getProfile,
  getSmsUsage,
  updateProfile,
  type Profile,
  type SmsUsage,
} from "@/lib/api";
import {
  COUNTRIES,
  composeE164,
  countryByCode,
  isValidE164,
  localPartFromE164,
  normalizePhoneInput,
} from "@/lib/countries";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

export default function AccountPage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [usage, setUsage] = useState<SmsUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Editable form state.
  const [phone, setPhone] = useState("");
  const [country, setCountry] = useState("");
  const [smsEnabled, setSmsEnabled] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [p, u] = await Promise.all([getProfile(), getSmsUsage()]);
      setProfile(p);
      setCountry(p.phone_country ?? "");
      setPhone(localPartFromE164(p.phone_country, p.phone_number));
      setSmsEnabled(p.sms_notifications_enabled);
      setUsage(u);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : "Failed to load your account.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The input holds the LOCAL number; combine it with the selected country's
  // dial code (e.g. +234) to form the E.164 number we validate + save.
  const rawInput = normalizePhoneInput(phone);
  const composedPhone = composeE164(country, phone);
  const phoneValid = rawInput === "" || isValidE164(composedPhone);
  const selectedDial = countryByCode(country)?.dialCode ?? "";

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const e164 = composeE164(country, phone);
      const body =
        e164 === ""
          ? { phone_number: null as string | null }
          : {
              phone_number: e164,
              phone_country: country || undefined,
              sms_notifications_enabled: smsEnabled,
            };
      const updated = await updateProfile(body);
      setProfile(updated);
      setCountry(updated.phone_country ?? "");
      // Show the LOCAL part in the input (dial code lives in the country field).
      setPhone(localPartFromE164(updated.phone_country, updated.phone_number));
      setSmsEnabled(updated.sms_notifications_enabled);
      setSuccess(
        e164 === ""
          ? "Phone number removed."
          : "Notification settings saved.",
      );
      // Refresh usage in the background (a new number doesn't change history).
      void getSmsUsage().then(setUsage).catch(() => undefined);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : "Failed to save your settings.",
      );
    } finally {
      setSaving(false);
    }
  }, [country, phone, smsEnabled]);

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-8">
      <div>
        <h1 className="text-2xl font-semibold">Account</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Manage your profile and SMS reply-approval notifications.
        </p>
      </div>

      {error ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          {error}
        </div>
      ) : null}
      {success ? (
        <div
          role="status"
          className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-300"
        >
          {success}
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">SMS notifications</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <p className="text-sm text-muted-foreground">
            Add your mobile number to get a text message whenever a reply is
            waiting for your approval. Standard carrier rates and Amazon SNS
            pricing apply based on your country.
          </p>

          {loading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : (
            <>
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="block space-y-1.5">
                  <span className="text-sm font-medium">Country</span>
                  <Select
                    value={country}
                    onChange={(e) => setCountry(e.target.value)}
                    aria-label="Phone number country"
                  >
                    <option value="">Select a country…</option>
                    {COUNTRIES.map((c) => (
                      <option key={c.code} value={c.code}>
                        {c.name} (+{c.dialCode})
                      </option>
                    ))}
                  </Select>
                </label>

                <label className="block space-y-1.5">
                  <span className="text-sm font-medium">Phone number</span>
                  <div className="flex items-stretch">
                    {selectedDial ? (
                      <span
                        aria-hidden
                        className="inline-flex items-center rounded-l-md border border-r-0 border-input bg-muted px-3 text-sm text-muted-foreground"
                      >
                        +{selectedDial}
                      </span>
                    ) : null}
                    <Input
                      type="tel"
                      inputMode="tel"
                      autoComplete="tel"
                      placeholder={selectedDial ? "9131682271" : "+14155550123"}
                      value={phone}
                      onChange={(e) => setPhone(e.target.value)}
                      aria-invalid={!phoneValid}
                      aria-describedby="phone-help"
                      className={selectedDial ? "rounded-l-none" : undefined}
                    />
                  </div>
                </label>
              </div>
              <p id="phone-help" className="text-xs text-muted-foreground">
                {selectedDial ? (
                  <>
                    Pick your country, then enter your number without the country
                    code — we&apos;ll add <code>+{selectedDial}</code> for you.
                  </>
                ) : (
                  <>
                    Select a country, or type the full international number, e.g.{" "}
                    <code>+14155550123</code>.
                  </>
                )}
                {!phoneValid ? (
                  <span className="ml-1 text-destructive">
                    Enter a valid phone number.
                  </span>
                ) : rawInput !== "" && composedPhone ? (
                  <span className="ml-1">
                    Will be saved as <code>{composedPhone}</code>.
                  </span>
                ) : null}
              </p>

              <div className="flex items-center justify-between rounded-md border bg-muted/30 px-4 py-3">
                <div>
                  <p className="text-sm font-medium">
                    Text me when a reply needs approval
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {rawInput === ""
                      ? "Add a phone number to enable."
                      : "You'll get an SMS for each pending reply."}
                  </p>
                </div>
                <Switch
                  checked={smsEnabled}
                  onCheckedChange={setSmsEnabled}
                  disabled={rawInput === "" || !phoneValid}
                  aria-label="Enable SMS notifications"
                />
              </div>

              <div className="flex items-center gap-3">
                <Button onClick={() => void handleSave()} disabled={saving || !phoneValid}>
                  {saving ? "Saving…" : "Save"}
                </Button>
                {profile?.phone_number ? (
                  <Button
                    variant="ghost"
                    onClick={() => {
                      setPhone("");
                      setSmsEnabled(false);
                    }}
                    disabled={saving}
                  >
                    Clear number
                  </Button>
                ) : null}
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">SMS usage &amp; estimated spend</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {usage ? (
            <>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Stat label="Messages sent" value={String(usage.total_sent)} />
                <Stat label="Segments" value={String(usage.total_segments)} />
                <Stat
                  label="Estimated spend"
                  value={`$${usage.total_spend_usd}`}
                />
                <Stat label="Failed" value={String(usage.total_failed)} />
              </div>

              {usage.by_country.length > 0 ? (
                <div className="overflow-hidden rounded-md border">
                  <table className="w-full text-sm">
                    <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                      <tr>
                        <th className="px-4 py-2 font-medium">Country</th>
                        <th className="px-4 py-2 font-medium">Messages</th>
                        <th className="px-4 py-2 font-medium">Segments</th>
                        <th className="px-4 py-2 font-medium">Spend (USD)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {usage.by_country.map((row) => (
                        <tr key={row.country} className="border-t">
                          <td className="px-4 py-2">
                            {countryByCode(row.country)?.name ?? row.country}
                          </td>
                          <td className="px-4 py-2 tabular-nums">{row.count}</td>
                          <td className="px-4 py-2 tabular-nums">{row.segments}</td>
                          <td className="px-4 py-2 tabular-nums">${row.spend_usd}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No SMS notifications sent yet.
                </p>
              )}
              <p className="text-xs text-muted-foreground">{usage.note}</p>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              {loading ? "Loading…" : "Usage unavailable."}
            </p>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-muted/20 px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold tabular-nums">{value}</p>
    </div>
  );
}
