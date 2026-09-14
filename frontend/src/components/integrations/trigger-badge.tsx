/**
 * Small "instant vs scheduled" trigger badge shown on provider tiles *before*
 * connecting, so the user knows up front how a provider will fire their agents:
 *
 *   - Instant   (emerald ⚡) → push/Pub/Sub webhook; fires immediately on events.
 *   - Scheduled (sky ⏱)     → polled on a schedule (no usable native webhook).
 *
 * Classification comes from the authoritative backend-derived map in
 * lib/trigger-badges.ts. Purely presentational.
 */

import { triggerBadgeFor, type TriggerBadge as Badge } from "@/lib/trigger-badges";

export function TriggerBadge({ provider }: { provider: string }) {
  const badge: Badge = triggerBadgeFor(provider);
  const instant = badge === "instant";
  return (
    <span
      title={
        instant
          ? "Instant triggers: events fire your agents immediately via a webhook."
          : "Scheduled: this provider is checked automatically on a schedule."
      }
      className={
        "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium leading-none " +
        (instant
          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          : "border-sky-500/30 bg-sky-500/10 text-sky-600 dark:text-sky-400")
      }
    >
      <span aria-hidden>{instant ? "⚡" : "⏱"}</span>
      {instant ? "Instant" : "Scheduled"}
    </span>
  );
}
