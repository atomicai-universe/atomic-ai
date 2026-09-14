"use client";

/**
 * Accessibility widget (BUILD.md, bottom-left).
 *
 * A floating button that opens a panel of accessibility preferences for people
 * with disabilities. Preferences are applied as classes / CSS variables on
 * <html> and persisted in localStorage under `atomic.a11y` so they survive
 * reloads. Options:
 *   - Text size (normal / large / larger) via the `--a11y-font-scale` variable
 *   - High contrast mode
 *   - Reduce motion (disables animations/transitions)
 *   - Readable (dyslexia-friendly) font
 *   - Always underline links
 *
 * The panel itself is keyboard accessible and closes on Escape / outside click.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { useVoiceSession, type VoiceStatus } from "@/lib/use-voice-session";
import { applyVoiceAction } from "@/lib/voice-actions";

const STORAGE_KEY = "atomic.a11y";

/** Human-readable status line for the voice control (screen-reader friendly). */
function voiceStatusLabel(status: VoiceStatus, error: string | null): string {
  if (status === "error") return error ?? "Voice control error.";
  switch (status) {
    case "connecting":
      return "Connecting…";
    case "listening":
      return "Listening…";
    case "speaking":
      return "Speaking…";
    case "idle":
    default:
      return "Voice control is off.";
  }
}

/** Return type of useVoiceSession, threaded from the widget to the section. */
type VoiceSession = ReturnType<typeof useVoiceSession>;

/**
 * The "Voice control" section added to the accessibility panel. It bridges the
 * browser mic/speaker to the backend voice agent via useVoiceSession and lets
 * the agent drive the UI via applyVoiceAction. All status/transcript output is
 * in `aria-live="polite"` regions so blind users hear state changes.
 *
 * The session itself is owned by the parent widget (so the floating button can
 * show a live indicator) and passed in here for rendering + controls.
 */
function VoiceControlSection({ session }: { session: VoiceSession }) {
  const { status, transcript, error, active, start, stop } = session;

  const busy = status === "connecting";
  const label = voiceStatusLabel(status, error);

  return (
    <div className="mt-3 border-t pt-3">
      <h3 className="mb-2 text-xs font-medium text-muted-foreground">
        Voice control
      </h3>

      <button
        type="button"
        onClick={() => {
          if (active) stop();
          else void start();
        }}
        aria-pressed={active}
        disabled={busy}
        className={`flex w-full items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-70 ${
          active
            ? "bg-destructive text-white hover:bg-destructive/90"
            : "bg-primary text-primary-foreground hover:bg-primary/90"
        }`}
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden>
          <path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3z" />
          <path d="M18 11a1 1 0 1 0-2 0 4 4 0 0 1-8 0 1 1 0 1 0-2 0 6 6 0 0 0 5 5.9V20H8a1 1 0 1 0 0 2h8a1 1 0 1 0 0-2h-3v-3.1A6 6 0 0 0 18 11z" />
        </svg>
        {active ? "Stop" : busy ? "Connecting…" : "Start voice control"}
      </button>

      {/* Status: announced politely so screen readers report state changes. */}
      <p
        aria-live="polite"
        role="status"
        className={`mt-2 text-xs ${
          status === "error" ? "text-destructive" : "text-muted-foreground"
        }`}
      >
        {label}
      </p>

      {/* Transcript: last few lines, also an aria-live region. */}
      {transcript.length > 0 ? (
        <div
          aria-live="polite"
          aria-label="Voice transcript"
          className="mt-2 max-h-40 space-y-1 overflow-y-auto rounded-md border bg-muted/30 p-2 text-xs"
        >
          {transcript.map((line, i) => (
            <p key={i}>
              <span className="font-medium">
                {line.role === "assistant" ? "Assistant: " : "You: "}
              </span>
              <span className="text-muted-foreground">{line.text}</span>
            </p>
          ))}
        </div>
      ) : null}
    </div>
  );
}

interface A11ySettings {
  fontScale: 1 | 1.15 | 1.3;
  highContrast: boolean;
  reduceMotion: boolean;
  readableFont: boolean;
  underlineLinks: boolean;
}

const DEFAULTS: A11ySettings = {
  fontScale: 1,
  highContrast: false,
  reduceMotion: false,
  readableFont: false,
  underlineLinks: false,
};

function load(): A11ySettings {
  if (typeof window === "undefined") return DEFAULTS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    return { ...DEFAULTS, ...(JSON.parse(raw) as Partial<A11ySettings>) };
  } catch {
    return DEFAULTS;
  }
}

function apply(s: A11ySettings) {
  const root = document.documentElement;
  root.style.setProperty("--a11y-font-scale", String(s.fontScale));
  root.classList.toggle("a11y-contrast", s.highContrast);
  root.classList.toggle("a11y-reduce-motion", s.reduceMotion);
  root.classList.toggle("a11y-readable-font", s.readableFont);
  root.classList.toggle("a11y-underline-links", s.underlineLinks);
}

export function AccessibilityWidget() {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<A11ySettings>(DEFAULTS);
  const ref = useRef<HTMLDivElement>(null);
  const router = useRouter();

  // The voice session is owned here so the floating button can reflect a live
  // session even while the panel is closed.
  const voice = useVoiceSession({
    onAction: (action) => applyVoiceAction(action, { router }),
  });

  useEffect(() => {
    const loaded = load();
    setSettings(loaded);
    apply(loaded);
  }, []);

  const update = useCallback((patch: Partial<A11ySettings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      apply(next);
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* storage unavailable — still applied for this session */
      }
      return next;
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const reset = useCallback(() => update(DEFAULTS), [update]);

  return (
    <div ref={ref} className="fixed bottom-4 left-4 z-50">
      {open ? (
        <div
          role="dialog"
          aria-label="Accessibility options"
          className="mb-3 w-72 rounded-xl border bg-card p-4 text-card-foreground shadow-xl"
        >
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">Accessibility</h2>
            <button
              type="button"
              onClick={reset}
              className="text-xs font-medium text-primary underline underline-offset-2 hover:no-underline"
            >
              Reset
            </button>
          </div>

          <fieldset className="mb-3">
            <legend className="mb-1.5 text-xs font-medium text-muted-foreground">
              Text size
            </legend>
            <div className="flex gap-2">
              {(
                [
                  ["A", 1],
                  ["A+", 1.15],
                  ["A++", 1.3],
                ] as const
              ).map(([label, scale]) => (
                <button
                  key={scale}
                  type="button"
                  onClick={() => update({ fontScale: scale })}
                  aria-pressed={settings.fontScale === scale}
                  className={`flex-1 rounded-md border px-2 py-1.5 text-sm transition-colors ${
                    settings.fontScale === scale
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-input hover:bg-accent"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </fieldset>

          <div className="space-y-1">
            {(
              [
                ["highContrast", "High contrast"],
                ["reduceMotion", "Reduce motion"],
                ["readableFont", "Readable font"],
                ["underlineLinks", "Underline links"],
              ] as const
            ).map(([key, label]) => (
              <label
                key={key}
                className="flex cursor-pointer items-center justify-between rounded-md px-1 py-1.5 text-sm hover:bg-accent"
              >
                {label}
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[hsl(var(--primary))]"
                  checked={settings[key]}
                  onChange={(e) => update({ [key]: e.target.checked })}
                />
              </label>
            ))}
          </div>

          {/* Voice control (Phase 2) — screen-reader-friendly voice agent. */}
          <VoiceControlSection session={voice} />
        </div>
      ) : null}

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={
          voice.active
            ? "Accessibility options (voice control active)"
            : "Accessibility options"
        }
        title="Accessibility options"
        className="relative flex h-12 w-12 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg ring-2 ring-background transition-transform hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {/* Universal accessibility person icon */}
        <svg viewBox="0 0 24 24" className="h-7 w-7" fill="currentColor" aria-hidden>
          <circle cx="12" cy="3.6" r="2" />
          <path d="M20 7.5c0 .6-.5 1-1 1.1l-4 .7v3.2l2.3 6.6a1.1 1.1 0 0 1-2 .8L12 15.5l-1.3 4.4a1.1 1.1 0 0 1-2-.8L11 12.5V9.3l-4-.7A1.1 1.1 0 0 1 7.3 6.4l3.7.7c.7.1 1.3.1 2 0l3.7-.7c.6-.1 1.2.3 1.3.9z" />
        </svg>

        {/* Live mic indicator while a voice session is active. */}
        {voice.active ? (
          <span
            aria-hidden
            className={`absolute -right-0.5 -top-0.5 flex h-4 w-4 items-center justify-center rounded-full ring-2 ring-background ${
              voice.status === "speaking"
                ? "bg-emerald-500"
                : "bg-red-500 animate-pulse"
            }`}
          >
            <svg viewBox="0 0 24 24" className="h-2.5 w-2.5" fill="white" aria-hidden>
              <path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3z" />
            </svg>
          </span>
        ) : null}
      </button>
    </div>
  );
}
