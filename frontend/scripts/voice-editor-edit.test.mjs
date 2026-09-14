/**
 * Voice rich-text editor bridge tests (ERROR.md round 3).
 *
 * Covers:
 *   - `replaceTextCI` (pure): case-insensitive, whitespace-tolerant,
 *     replace-all find-and-replace mirroring the backend `_replace_ci`.
 *   - The editor registry: register/get/unregister a voice-field controller.
 *   - `handleType` routing: a `type` action for a field with a REGISTERED
 *     rich-text editor drives the editor (setText+focus), NOT the DOM.
 *   - `edit_field` forwarding: dispatched over the approval CustomEvent bus
 *     with its set / find-and-replace payload intact.
 *
 * Repo convention: a standalone Node script importing the real .ts modules via
 * Node's native type-stripping (see voice-approval-actions.test.mjs).
 */

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REGISTRY_PATH = path.join(__dirname, "..", "src", "lib", "voice-editor-registry.ts");
const ACTIONS_PATH = path.join(__dirname, "..", "src", "lib", "voice-actions.ts");

const {
  replaceTextCI,
  registerVoiceEditor,
  getVoiceEditor,
  _resetVoiceEditors,
} = await import(REGISTRY_PATH);
const { applyVoiceAction, VOICE_APPROVAL_EVENT } = await import(ACTIONS_PATH);

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

function makeRouter() {
  const pushed = [];
  return { pushed, push: (p) => pushed.push(p) };
}

/** A fake editor controller recording calls, backed by a mutable text buffer. */
function makeFakeEditor(initial = "") {
  let text = initial;
  const calls = [];
  return {
    calls,
    getText: () => text,
    setText: (t) => {
      calls.push(["setText", t]);
      text = t;
    },
    focus: () => calls.push(["focus"]),
    replaceText: (s, r) => {
      calls.push(["replaceText", s, r]);
      const [next, replaced] = replaceTextCI(text, s, r);
      text = next;
      return replaced;
    },
  };
}

/* ── replaceTextCI (pure) ───────────────────────────────────────────────── */

check("replaceTextCI replaces case-insensitively", () => {
  const [out, replaced] = replaceTextCI("Hi there, thanks.", "hi there", "Hello PayRogen");
  assert.equal(out, "Hello PayRogen, thanks.");
  assert.equal(replaced, true);
});

check("replaceTextCI is whitespace-tolerant across the search tokens", () => {
  const [out, replaced] = replaceTextCI("Hi   there team", "hi there", "hey");
  assert.equal(out, "hey team");
  assert.equal(replaced, true);
});

check("replaceTextCI replaces EVERY occurrence", () => {
  const [out] = replaceTextCI("no no no", "no", "yes");
  assert.equal(out, "yes yes yes");
});

check("replaceTextCI reports replaced=false when not found", () => {
  const [out, replaced] = replaceTextCI("hello team", "goodbye", "farewell");
  assert.equal(out, "hello team");
  assert.equal(replaced, false);
});

check("replaceTextCI escapes regex metacharacters in the search", () => {
  const [out, replaced] = replaceTextCI("cost is $5 (five)", "$5 (five)", "$6");
  assert.equal(out, "cost is $6");
  assert.equal(replaced, true);
});

check("replaceTextCI empty search is a no-op", () => {
  const [out, replaced] = replaceTextCI("unchanged", "", "x");
  assert.equal(out, "unchanged");
  assert.equal(replaced, false);
});

/* ── registry ───────────────────────────────────────────────────────────── */

check("register/get/unregister a voice editor", () => {
  _resetVoiceEditors();
  const ed = makeFakeEditor();
  assert.equal(getVoiceEditor("reply_body"), undefined);
  const unregister = registerVoiceEditor("reply_body", ed);
  assert.equal(getVoiceEditor("reply_body"), ed);
  unregister();
  assert.equal(getVoiceEditor("reply_body"), undefined);
});

/* ── handleType routes to a registered editor ───────────────────────────── */

check("type into a field with a registered editor drives the editor, not the DOM", () => {
  _resetVoiceEditors();
  // No document: if handleType tried the DOM path it would no-op, but with an
  // editor registered it must use the editor and never touch the DOM.
  const ed = makeFakeEditor("old body");
  registerVoiceEditor("reply_body", ed);
  applyVoiceAction(
    { type: "type", field: "reply_body", text: "brand new body" },
    { router: makeRouter() },
  );
  assert.deepEqual(ed.calls, [["setText", "brand new body"], ["focus"]]);
  assert.equal(ed.getText(), "brand new body");
  _resetVoiceEditors();
});

/* ── edit_field forwarding over the CustomEvent bus ─────────────────────── */

function withStubbedWindow(run) {
  const events = [];
  const originalWindow = globalThis.window;
  const originalCustomEvent = globalThis.CustomEvent;
  class StubCustomEvent {
    constructor(type, init) {
      this.type = type;
      this.detail = init?.detail;
    }
  }
  globalThis.CustomEvent = StubCustomEvent;
  globalThis.window = {
    CustomEvent: StubCustomEvent,
    dispatchEvent: (ev) => {
      events.push(ev);
      return true;
    },
  };
  try {
    run(events);
  } finally {
    globalThis.window = originalWindow;
    globalThis.CustomEvent = originalCustomEvent;
  }
}

check("edit_field (find-and-replace) forwards over the approval event bus", () => {
  withStubbedWindow((events) => {
    applyVoiceAction(
      {
        type: "edit_field",
        position: 1,
        field: "body",
        search: "hi there",
        replacement: "hi payroll",
      },
      { router: makeRouter() },
    );
    assert.equal(events.length, 1);
    assert.equal(events[0].type, VOICE_APPROVAL_EVENT);
    assert.deepEqual(events[0].detail, {
      type: "edit_field",
      position: 1,
      field: "body",
      search: "hi there",
      replacement: "hi payroll",
    });
  });
});

check("edit_field (whole-field set) forwards the value", () => {
  withStubbedWindow((events) => {
    applyVoiceAction(
      { type: "edit_field", position: 2, field: "subject", value: "Support Request" },
      { router: makeRouter() },
    );
    assert.equal(events.length, 1);
    assert.deepEqual(events[0].detail, {
      type: "edit_field",
      position: 2,
      field: "subject",
      value: "Support Request",
    });
  });
});

if (failures.length > 0) {
  console.error(`\n✗ voice editor edit FAILED — ${failures.length} assertion(s):\n`);
  for (const f of failures) console.error(`  - ${f.name}\n      ${f.message}`);
  process.exit(1);
}
console.log(
  `✓ voice editor edit passed — ${passed} checks: replaceTextCI is a pure ` +
    `case-insensitive replace-all, the registry register/get/unregister works, ` +
    `type routes to a registered editor, and edit_field forwards over the ` +
    `"${VOICE_APPROVAL_EVENT}" bus.`,
);
