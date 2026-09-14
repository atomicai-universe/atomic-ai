"use client";

/**
 * Webhook status panel for a connected integration (BUILD.md).
 *
 * Renders a clear, modern status for how the integration triggers agent runs:
 *   - auto      → "Instant triggers active" (auto-registered) or "Setup pending"
 *   - partial   → needs a target field to enable push (lists what's missing)
 *   - manual    → shows the webhook URL to paste in the provider dashboard + steps
 *   - poll      → "Checked on a schedule" (no webhook needed)
 *
 * Designed to make the next action obvious at a glance: a colored status pill,
 * one-line explanation, and (for manual) a copy-to-clipboard webhook URL.
 */

import { useState } from "react";

export interface WebhookInfo {
  mode: "auto" | "partial" | "manual" | "poll" | string;
  registered: boolean;
  webhook_url: string;
  needs: string[];
  note: string;
  trigger_kind: string;
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard blocked — the value is still visible to select */
        }
      }}
      className="shrink-0 rounded-md border border-input bg-background px-2.5 py-1 text-xs font-medium transition-colors hover:bg-accent"
    >
      {copied ? "Copied ✓" : "Copy"}
    </button>
  );
}

interface Pill {
  label: string;
  className: string;
  dot: string;
}

function statusPill(info: WebhookInfo): Pill {
  if (info.mode === "poll") {
    return {
      label: "Scheduled checks",
      className: "bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/30",
      dot: "bg-sky-500",
    };
  }
  if (info.mode === "manual") {
    return {
      label: "Action needed",
      className: "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30",
      dot: "bg-amber-500",
    };
  }
  if (info.mode === "partial" && info.needs.length > 0) {
    return {
      label: "Setup incomplete",
      className: "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30",
      dot: "bg-amber-500",
    };
  }
  if (info.registered) {
    return {
      label: "Instant triggers active",
      className: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
      dot: "bg-emerald-500",
    };
  }
  return {
    label: "Setup pending",
    className: "bg-muted text-muted-foreground border-border",
    dot: "bg-muted-foreground",
  };
}

function headline(info: WebhookInfo): string {
  if (info.mode === "poll")
    return "This provider is checked automatically on a schedule — no setup needed.";
  if (info.registered)
    return "Events from this provider trigger your agents instantly. You're all set.";
  if (info.mode === "manual")
    return "Add the webhook URL below in the provider's dashboard to enable instant triggers.";
  if (info.mode === "partial" && info.needs.length > 0)
    return `Add ${info.needs.join(", ")} above and reconnect to enable instant triggers.`;
  if (info.note) return info.note;
  return "Instant triggers aren't active yet — see the setup note below.";
}

function friendlyNeed(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\bid\b/i, "ID")
    .replace(/^\w/, (c) => c.toUpperCase());
}

export function WebhookStatus({ info }: { info: WebhookInfo | null | undefined }) {
  if (!info) return null;
  const pill = statusPill(info);

  return (
    <div className="mt-3 rounded-lg border bg-muted/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${pill.className}`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${pill.dot}`} aria-hidden />
          {pill.label}
        </span>
        <span className="text-xs text-muted-foreground">{headline(info)}</span>
      </div>

      {/* Partial: list the missing target fields as clear chips. */}
      {info.mode === "partial" && info.needs.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {info.needs.map((n) => (
            <span
              key={n}
              className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-600 dark:text-amber-400"
            >
              Needs: {friendlyNeed(n)}
            </span>
          ))}
        </div>
      ) : null}

      {/* Manual: show the webhook URL to copy + step-by-step note. */}
      {info.mode === "manual" && info.webhook_url ? (
        <div className="mt-2 space-y-2">
          <div className="flex items-center gap-2">
            <code className="block w-full overflow-x-auto rounded bg-background px-2 py-1.5 text-xs">
              {info.webhook_url}
            </code>
            <CopyButton value={info.webhook_url} />
          </div>
          {info.note ? (
            <p className="text-xs leading-relaxed text-muted-foreground">{info.note}</p>
          ) : null}
        </div>
      ) : null}

      {/* Poll: a subtle reassurance note. */}
      {info.mode === "poll" && info.note ? (
        <p className="mt-1.5 text-xs text-muted-foreground">{info.note}</p>
      ) : null}

      {/* Universal fallback: surface the setup note for any remaining state
          (e.g. auto-but-not-registered) so the user never hits a dead-end.
          Manual and poll already render their own note above. */}
      {info.note &&
      info.mode !== "manual" &&
      info.mode !== "poll" &&
      !(info.mode === "partial" && info.needs.length > 0) ? (
        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{info.note}</p>
      ) : null}
    </div>
  );
}
