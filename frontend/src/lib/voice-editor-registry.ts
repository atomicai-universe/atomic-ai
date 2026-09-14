/**
 * Voice ↔ rich-text editor bridge (Phase 3 / ERROR.md round 3).
 *
 * The reply body is now a TipTap (ProseMirror) rich-text editor rather than a
 * plain <textarea>. TipTap renders a contentEditable ProseMirror node, so the
 * old `data-voice-field` + `setNativeValue` path (which drives native
 * input/textarea `.value`) cannot type into it correctly. Instead, each mounted
 * rich-text editor REGISTERS a small imperative controller here, keyed by its
 * logical voice-field name (e.g. `"reply_body"`). The voice action layer
 * (`voice-actions.ts`) looks the controller up and drives the editor through
 * ProseMirror's transaction API — so dictation, whole-field set, and
 * programmatic find-and-replace all go through the editor's real model instead
 * of clobbering the DOM.
 *
 * This module is intentionally tiny and framework-agnostic (no React import) so
 * the pure helper `replaceTextCI` can be unit-tested in a plain Node `.mjs`
 * test, mirroring the backend `_replace_ci`.
 */

/** Imperative surface a registered rich-text editor exposes to the voice layer. */
export interface VoiceEditorController {
  /** Current plain-text content of the editor. */
  getText: () => string;
  /** Replace the ENTIRE content with plain text (whole-field set / dictation). */
  setText: (text: string) => void;
  /** Move focus (and caret) into the editor. */
  focus: () => void;
  /**
   * Case-insensitive find-and-replace of `search` → `replacement` over the
   * editor's plain text. Returns true when at least one match was replaced.
   */
  replaceText: (search: string, replacement: string) => boolean;
}

// Module-level registry. Keyed by the logical voice-field name so the voice
// layer can resolve "reply_body" → the mounted editor's controller. A field is
// registered on mount and removed on unmount to avoid stale controllers.
//
// It is also mirrored onto `globalThis` under a unique symbol so the voice
// action layer (voice-actions.ts) can read it WITHOUT a static ESM import of
// this module. That decoupling keeps voice-actions.ts importable by the plain
// Node `.mjs` test runner (Node's type-stripping loader does not resolve the
// `@/` path alias or extensionless relative TS imports). Both this module and
// voice-actions.ts run in the SAME JS realm at runtime, so the shared global is
// the single source of truth for the mounted editors.
const REGISTRY_KEY = "__atomicVoiceEditors__";

function sharedRegistry(): Map<string, VoiceEditorController> {
  const g = globalThis as unknown as {
    [REGISTRY_KEY]?: Map<string, VoiceEditorController>;
  };
  if (!g[REGISTRY_KEY]) g[REGISTRY_KEY] = new Map();
  return g[REGISTRY_KEY];
}

const registry = sharedRegistry();

/** Register (or replace) the controller for a voice-field. Returns an unregister fn. */
export function registerVoiceEditor(
  field: string,
  controller: VoiceEditorController,
): () => void {
  registry.set(field, controller);
  return () => {
    // Only delete if we are still the current controller (guards against a
    // remount race replacing a newer controller with an older unmount).
    if (registry.get(field) === controller) registry.delete(field);
  };
}

/** Look up the controller for a voice-field, or `undefined` when none is mounted. */
export function getVoiceEditor(field: string): VoiceEditorController | undefined {
  return registry.get(field);
}

/** Test-only: clear the registry between cases. */
export function _resetVoiceEditors(): void {
  registry.clear();
}

/**
 * Case-insensitive, whitespace-tolerant find-and-replace over plain text.
 *
 * Pure (no DOM). Mirrors the backend `_replace_ci` in
 * `app/services/voice_gateway.py` so the frontend live-edit and the server-side
 * stored edit stay in agreement: the search matches case-insensitively, a
 * single spoken space in `search` matches any run of whitespace in the text,
 * and EVERY occurrence is replaced. Returns `[newText, replaced]`.
 */
export function replaceTextCI(
  text: string,
  search: string,
  replacement: string,
): [string, boolean] {
  const needle = (search ?? "").trim();
  if (!needle) return [text, false];
  // Escape regex metacharacters in each whitespace-split token, then join with
  // `\s+` so "hi there" matches "Hi  there" / "Hi\nthere".
  const tokens = needle.split(/\s+/).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pattern = new RegExp(tokens.join("\\s+"), "gi");
  let replaced = false;
  const out = text.replace(pattern, () => {
    replaced = true;
    return replacement;
  });
  return [out, replaced];
}
