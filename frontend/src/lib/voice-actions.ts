"use client";

/**
 * Minimal shape of a registered rich-text editor controller (see
 * `voice-editor-registry.ts` — the source of truth). Duplicated structurally
 * here to avoid a static import that would break the Node `.mjs` test runner's
 * module resolution; the registry is shared at runtime via a `globalThis` key.
 */
interface RegisteredVoiceEditor {
  getText: () => string;
  setText: (text: string) => void;
  focus: () => void;
  replaceText: (search: string, replacement: string) => boolean;
}

/**
 * Look up a mounted rich-text editor for `field` from the shared global
 * registry (populated by `registerVoiceEditor`). Returns `undefined` when no
 * editor is mounted for the field or when running outside a JS realm.
 */
function getVoiceEditor(field: string): RegisteredVoiceEditor | undefined {
  const g = globalThis as unknown as {
    __atomicVoiceEditors__?: Map<string, RegisteredVoiceEditor>;
  };
  return g.__atomicVoiceEditors__?.get(field);
}

/**
 * Frontend side-effects for server-issued voice actions (Phase 2).
 *
 * The backend voice agent (Nova Sonic) can drive the UI by sending
 * `{"type":"action", ...}` frames over the voice WebSocket. This module turns
 * those actions into concrete DOM/router effects so a blind user can navigate,
 * fill fields, and submit forms entirely by voice.
 *
 * Targeting convention (light-touch, opt-in):
 *   - Inputs opt in with `data-voice-field="<field>"`, e.g.
 *       data-voice-field="rule_prompt"
 *       data-voice-field="credential:client_id"   (for `credential:<name>`)
 *   - Submit targets opt in with `data-voice-form="<form>"` on a <form>, a
 *     <button>, or any clickable element, e.g. data-voice-form="create_rule".
 *
 * Everything is defensive: a missing target is a graceful no-op (the agent will
 * simply narrate that it typed/submitted). Unknown action types are ignored.
 */

/** A `navigate` action: client-side route change. */
export interface NavigateAction {
  type: "navigate";
  path: string;
}

/** A `type` action: set the value of a logical field. */
export interface TypeAction {
  type: "type";
  field: string;
  text: string;
}

/** A `submit` action: submit/click a logical form target. */
export interface SubmitAction {
  type: "submit";
  form: string;
}

/** An `interrupt` action: barge-in signal (handled in the hook, not here). */
export interface InterruptAction {
  type: "interrupt";
  reason?: string;
}

/**
 * A `focus_reply` action: highlight/scroll to the Nth PENDING reply.
 * `position` is 1-based and matches the Nth item in the Approvals page's
 * `pending` array (created_at ASC), i.e. index `position - 1`.
 */
export interface FocusReplyAction {
  type: "focus_reply";
  position: number;
}

/** An `open_edit` action: open the inline Edit form on the Nth pending reply. */
export interface OpenEditAction {
  type: "open_edit";
  position: number;
}

/**
 * An `open_schedule` action: open + focus the schedule datetime picker on the
 * Nth pending reply. When `when_iso` is present the picker is prefilled with
 * that instant (converted to the browser's local `datetime-local` value).
 */
export interface OpenScheduleAction {
  type: "open_schedule";
  position: number;
  when_iso?: string;
}

/**
 * A `cancel_edit` action: the user said "cancel"/"never mind" — close ANY open
 * inline editor or scheduler on every pending card. No position: it applies to
 * whatever is currently open.
 *
 * Backend emitter: YES — the voice backend now emits `{"type":"cancel_edit"}`.
 */
export interface CancelEditAction {
  type: "cancel_edit";
}

/**
 * An `approvals_cleared` action: a bulk clear happened server-side (all pending
 * replies were rejected, keeping the audit trail). The frontend just needs to
 * resync the queue — the backend already did the rejections. `cleared` is the
 * count for narration; no position is needed.
 *
 * Backend emitter: YES — emitted after a bulk clear-pending.
 */
export interface ApprovalsClearedAction {
  type: "approvals_cleared";
  cleared?: number;
}

/**
 * A `focus_field` action: focus a specific field of the Nth pending reply's
 * open Edit form so the user can dictate into it ("edit the To of reply 1",
 * "change the subject"). `position` is 1-based (matches the rendered order).
 *
 * Backend emitter: NOT YET — this is FRONTEND-READY. The handler + card handle
 * method exist so per-field focus works the moment the backend starts emitting
 * `{"type":"focus_field","position":N,"field":"to"|"subject"|"body"}`.
 */
export interface FocusFieldAction {
  type: "focus_field";
  position: number;
  field: "to" | "subject" | "body";
}

/**
 * A `regenerate_body` action: ask the AI to regenerate the whole reply for the
 * Nth pending reply (which replaces the body). If the Edit form is open, the
 * body is refocused after regeneration.
 *
 * Backend emitter: NOT YET — FRONTEND-READY, awaiting a backend emitter for
 * `{"type":"regenerate_body","position":N}`.
 */
export interface RegenerateBodyAction {
  type: "regenerate_body";
  position: number;
}

/**
 * An `edit_field` action: apply an in-place edit to a field of the Nth pending
 * reply's editor, reflecting the server-side change live in the UI. Either:
 *   - a whole-field SET (`value` present): set the field to `value`; or
 *   - a find-and-replace (`search` + `replacement`): replace `search` with
 *     `replacement` (case-insensitive) in the field's current content.
 * The body field is a TipTap rich-text editor (driven via the editor registry);
 * `to`/`subject` are native inputs (driven via the DOM). `position` is 1-based.
 *
 * Backend emitter: YES — emitted by the gateway after a voice edit so a live
 * reviewer sees the change immediately.
 */
export interface EditFieldAction {
  type: "edit_field";
  position: number;
  field: "to" | "subject" | "body";
  value?: string;
  search?: string;
  replacement?: string;
}

export type VoiceAction =
  | NavigateAction
  | TypeAction
  | SubmitAction
  | InterruptAction
  | FocusReplyAction
  | OpenEditAction
  | OpenScheduleAction
  | CancelEditAction
  | ApprovalsClearedAction
  | FocusFieldAction
  | RegenerateBodyAction
  | EditFieldAction
  | { type: string; [key: string]: unknown };

/**
 * The DOM CustomEvent name the widget-owned voice session uses to forward
 * position-aware approval actions to the Approvals page. The Approvals page
 * owns the pending cards, but the voice session lives in the accessibility
 * widget, so a lightweight window event bus decouples the two without any
 * shared React context. The event `detail` is the raw {@link VoiceAction}
 * (one of focus_reply / open_edit / open_schedule).
 */
export const VOICE_APPROVAL_EVENT = "atomic:voice-approval-action";

/**
 * The subset of actions forwarded to the Approvals page over the event bus.
 *
 * Position-aware (need a card): focus_reply, open_edit, open_schedule,
 * focus_field, regenerate_body. Queue-wide (no position): cancel_edit (close
 * every open editor) and approvals_cleared (resync the queue).
 */
export type ApprovalVoiceAction =
  | FocusReplyAction
  | OpenEditAction
  | OpenScheduleAction
  | CancelEditAction
  | ApprovalsClearedAction
  | FocusFieldAction
  | RegenerateBodyAction
  | EditFieldAction;

/**
 * Convert an ISO-8601 instant to a browser-local `datetime-local` input value
 * (`YYYY-MM-DDTHH:mm`, no timezone — the picker interprets it in local time).
 *
 * Pure and side-effect free so it can be unit-tested without a DOM. Returns
 * `null` when the input doesn't parse, so a bad `when_iso` degrades to "open
 * the picker without prefilling" rather than throwing.
 */
export function isoToDatetimeLocal(iso: string | null | undefined): string | null {
  if (typeof iso !== "string" || iso.length === 0) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/** Minimal router surface we need (matches `useRouter()` from next/navigation). */
export interface VoiceRouter {
  push: (path: string) => void;
}

export interface ApplyVoiceActionDeps {
  router: VoiceRouter;
}

/**
 * Set an input/textarea value through React's native setter so controlled
 * components observe the change, then fire input+change events.
 */
function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, text: string): void {
  const proto =
    el instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
  const setter = descriptor?.set;
  if (setter) {
    setter.call(el, text);
  } else {
    // Extremely defensive fallback; normally the descriptor always exists.
    el.value = text;
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

/** CSS-escape a data-attribute value so arbitrary field names are safe in a selector. */
function cssEscape(value: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(value);
  }
  // Basic fallback: escape characters that would break an attribute selector.
  return value.replace(/["\\\]]/g, "\\$&");
}

function handleType(action: TypeAction): void {
  // Prefer a registered rich-text editor (TipTap) for this field: dictating
  // into the body must go through the editor's model, not a raw DOM set. This
  // is checked BEFORE the `document` guard because the editor controller does
  // not need the DOM query path.
  const editor = getVoiceEditor(action.field);
  if (editor) {
    editor.setText(action.text ?? "");
    editor.focus();
    return;
  }

  if (typeof document === "undefined") return;
  const selector = `[data-voice-field="${cssEscape(action.field)}"]`;
  const el = document.querySelector(selector);
  if (!el) return; // graceful no-op

  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
    setNativeValue(el, action.text ?? "");
    try {
      el.focus();
    } catch {
      /* focus can throw in odd states — ignore */
    }
    return;
  }

  // contentEditable or other elements: best-effort text set.
  if (el instanceof HTMLElement && el.isContentEditable) {
    el.textContent = action.text ?? "";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

function handleSubmit(action: SubmitAction): void {
  if (typeof document === "undefined") return;
  const selector = `[data-voice-form="${cssEscape(action.form)}"]`;
  const el = document.querySelector(selector);
  if (!el) return; // graceful no-op

  if (el instanceof HTMLFormElement) {
    if (typeof el.requestSubmit === "function") {
      el.requestSubmit();
    } else {
      el.submit();
    }
    return;
  }

  if (el instanceof HTMLElement) {
    el.click();
  }
}

/**
 * Forward a position-aware approval action to the Approvals page over the
 * window event bus. The Approvals page listens for {@link VOICE_APPROVAL_EVENT}
 * and resolves `position` → the pending card at index `position - 1`.
 * SSR-safe: a no-op when there is no `window`.
 */
function dispatchApprovalAction(action: ApprovalVoiceAction): void {
  if (typeof window === "undefined") return;
  try {
    window.dispatchEvent(
      new CustomEvent(VOICE_APPROVAL_EVENT, { detail: action }),
    );
  } catch {
    /* CustomEvent unavailable in some non-DOM environments — ignore */
  }
}

/**
 * Perform the frontend effect for a server voice action.
 *
 * - `navigate`      → router.push(path)
 * - `type`          → set the target field's value (React-aware) + focus
 * - `submit`        → requestSubmit()/click() the target
 * - `focus_reply`      → forward to the Approvals page (scroll/highlight/focus Nth)
 * - `open_edit`        → forward to the Approvals page (open Edit on the Nth)
 * - `open_schedule`    → forward to the Approvals page (open picker on the Nth)
 * - `cancel_edit`      → forward: close every open editor/scheduler
 * - `approvals_cleared`→ forward: resync the queue after a bulk clear
 * - `focus_field`      → forward: focus a field of the Nth reply's edit form
 * - `edit_field`       → forward: apply a live set/find-and-replace to a field
 * - `regenerate_body`  → forward: regenerate the Nth reply (replaces the body)
 * - `interrupt`        → no-op here (the hook flushes playback on barge-in)
 * - unknown            → no-op
 */
export function applyVoiceAction(
  action: VoiceAction | null | undefined,
  deps: ApplyVoiceActionDeps,
): void {
  if (!action || typeof action.type !== "string") return;

  switch (action.type) {
    case "navigate": {
      const path = (action as NavigateAction).path;
      if (typeof path !== "string" || path.length === 0) return;
      // Only allow same-origin, absolute app paths ("/dashboard/...") to guard
      // against an unexpected external/redirect target arriving over the wire.
      if (!path.startsWith("/") || path.startsWith("//")) return;
      // Primary: client-side navigation via the App Router. Fall back to a hard
      // location change if router.push is unavailable or throws (navigation
      // triggered from the voice WebSocket callback must be reliable — a blind
      // user has no other way to get there).
      try {
        deps.router.push(path);
      } catch {
        if (typeof window !== "undefined") {
          window.location.assign(path);
        }
      }
      return;
    }
    case "type": {
      handleType(action as TypeAction);
      return;
    }
    case "submit": {
      handleSubmit(action as SubmitAction);
      return;
    }
    case "focus_reply":
    case "open_edit":
    case "open_schedule":
    case "cancel_edit":
    case "approvals_cleared":
    case "focus_field":
    case "edit_field":
    case "regenerate_body": {
      // Forward every approval-scoped action over the same window event bus.
      // Backend emitters today: focus_reply/open_edit/open_schedule/type +
      // cancel_edit/approvals_cleared. Frontend-ready (awaiting a backend
      // emitter): focus_field/regenerate_body.
      dispatchApprovalAction(action as ApprovalVoiceAction);
      return;
    }
    case "interrupt":
    default:
      // interrupt is handled inside the voice hook (stops playback);
      // any unknown action type is ignored.
      return;
  }
}
