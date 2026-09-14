/**
 * Position-aware voice approval action tests.
 *
 * The backend now emits position-aware frames over the voice WebSocket:
 *   {"type":"focus_reply","position":N}
 *   {"type":"open_edit","position":N}
 *   {"type":"open_schedule","position":N,"when_iso"?:"<ISO8601>"}
 *
 * The voice session lives in the accessibility widget, but the Approvals page
 * owns the pending cards. `applyVoiceAction` bridges the two by dispatching a
 * window CustomEvent ("atomic:voice-approval-action") whose `detail` is the raw
 * action. The Approvals page listens and resolves position → card.
 *
 * This test exercises `applyVoiceAction` directly and asserts the CustomEvent
 * contract (correct name + detail) for the three new actions, that navigate/
 * unknown actions are unaffected, and unit-tests the pure ISO→datetime-local
 * helper. Repo convention: a standalone Node script importing the real .ts
 * module via Node's native type-stripping (see voice-actions-navigate.test.mjs).
 */

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(__dirname, "..", "src", "lib", "voice-actions.ts");
const { applyVoiceAction, isoToDatetimeLocal, VOICE_APPROVAL_EVENT } =
  await import(MODULE_PATH);

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

/** A recording router matching the VoiceRouter surface. */
function makeRouter() {
  const pushed = [];
  return { pushed, push: (p) => pushed.push(p) };
}

/**
 * Install a minimal window with a CustomEvent + dispatchEvent that records
 * dispatched events, mirroring the browser contract applyVoiceAction relies on.
 * Returns the recorded events array and a restore() to put globals back.
 */
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

check("the event name is the shared constant", () => {
  assert.equal(VOICE_APPROVAL_EVENT, "atomic:voice-approval-action");
});

check("focus_reply dispatches the CustomEvent with the exact detail", () => {
  withStubbedWindow((events) => {
    const router = makeRouter();
    const action = { type: "focus_reply", position: 2 };
    applyVoiceAction(action, { router });
    assert.equal(events.length, 1);
    assert.equal(events[0].type, VOICE_APPROVAL_EVENT);
    assert.deepEqual(events[0].detail, { type: "focus_reply", position: 2 });
    // Forwarding an approval action must not touch the router.
    assert.deepEqual(router.pushed, []);
  });
});

check("open_edit dispatches with the position preserved", () => {
  withStubbedWindow((events) => {
    applyVoiceAction({ type: "open_edit", position: 1 }, { router: makeRouter() });
    assert.equal(events.length, 1);
    assert.equal(events[0].type, VOICE_APPROVAL_EVENT);
    assert.deepEqual(events[0].detail, { type: "open_edit", position: 1 });
  });
});

check("open_schedule WITHOUT when_iso dispatches position only", () => {
  withStubbedWindow((events) => {
    applyVoiceAction({ type: "open_schedule", position: 3 }, { router: makeRouter() });
    assert.equal(events.length, 1);
    assert.deepEqual(events[0].detail, { type: "open_schedule", position: 3 });
    assert.equal("when_iso" in events[0].detail, false);
  });
});

check("open_schedule WITH when_iso forwards the ISO instant unchanged", () => {
  withStubbedWindow((events) => {
    const when = "2025-03-04T09:30:00Z";
    applyVoiceAction(
      { type: "open_schedule", position: 2, when_iso: when },
      { router: makeRouter() },
    );
    assert.equal(events.length, 1);
    assert.deepEqual(events[0].detail, {
      type: "open_schedule",
      position: 2,
      when_iso: when,
    });
  });
});

check("cancel_edit dispatches the CustomEvent (no position needed)", () => {
  withStubbedWindow((events) => {
    const router = makeRouter();
    applyVoiceAction({ type: "cancel_edit" }, { router });
    assert.equal(events.length, 1);
    assert.equal(events[0].type, VOICE_APPROVAL_EVENT);
    assert.deepEqual(events[0].detail, { type: "cancel_edit" });
    assert.deepEqual(router.pushed, []);
  });
});

check("approvals_cleared forwards with the cleared count in the detail", () => {
  withStubbedWindow((events) => {
    applyVoiceAction(
      { type: "approvals_cleared", cleared: 3 },
      { router: makeRouter() },
    );
    assert.equal(events.length, 1);
    assert.equal(events[0].type, VOICE_APPROVAL_EVENT);
    assert.deepEqual(events[0].detail, { type: "approvals_cleared", cleared: 3 });
  });
});

check("focus_field forwards position + field unchanged", () => {
  withStubbedWindow((events) => {
    applyVoiceAction(
      { type: "focus_field", position: 2, field: "subject" },
      { router: makeRouter() },
    );
    assert.equal(events.length, 1);
    assert.equal(events[0].type, VOICE_APPROVAL_EVENT);
    assert.deepEqual(events[0].detail, {
      type: "focus_field",
      position: 2,
      field: "subject",
    });
  });
});

check("regenerate_body forwards with the position", () => {
  withStubbedWindow((events) => {
    applyVoiceAction(
      { type: "regenerate_body", position: 1 },
      { router: makeRouter() },
    );
    assert.equal(events.length, 1);
    assert.equal(events[0].type, VOICE_APPROVAL_EVENT);
    assert.deepEqual(events[0].detail, { type: "regenerate_body", position: 1 });
  });
});

check("navigate is unaffected: no approval event, router is used", () => {
  withStubbedWindow((events) => {
    const router = makeRouter();
    applyVoiceAction({ type: "navigate", path: "/dashboard/approvals" }, { router });
    assert.deepEqual(router.pushed, ["/dashboard/approvals"]);
    // navigate must NOT emit the approval CustomEvent.
    const approvalEvents = events.filter((e) => e.type === VOICE_APPROVAL_EVENT);
    assert.deepEqual(approvalEvents, []);
  });
});

check("unknown / interrupt actions dispatch nothing", () => {
  withStubbedWindow((events) => {
    const router = makeRouter();
    applyVoiceAction({ type: "interrupt" }, { router });
    applyVoiceAction({ type: "totally_unknown", position: 9 }, { router });
    assert.deepEqual(events, []);
    assert.deepEqual(router.pushed, []);
  });
});

check("SSR-safe: no window means a graceful no-op (no throw)", () => {
  const originalWindow = globalThis.window;
  // Simulate SSR: window is undefined.
  // eslint-disable-next-line no-undef
  globalThis.window = undefined;
  try {
    assert.doesNotThrow(() =>
      applyVoiceAction({ type: "focus_reply", position: 1 }, { router: makeRouter() }),
    );
  } finally {
    globalThis.window = originalWindow;
  }
});

/* ── isoToDatetimeLocal (pure) ──────────────────────────────────────────── */

check("isoToDatetimeLocal formats explicit local components (YYYY-MM-DDTHH:mm)", () => {
  // Build an ISO from an explicit LOCAL wall-clock time so the assertion is
  // independent of the runner's timezone: the helper reads local getters, so a
  // Date built from local components round-trips to those same components.
  const local = new Date(2025, 2, 4, 9, 5); // 2025-03-04 09:05 local
  const out = isoToDatetimeLocal(local.toISOString());
  assert.equal(out, "2025-03-04T09:05");
});

check("isoToDatetimeLocal zero-pads month/day/hour/minute", () => {
  const local = new Date(2025, 0, 1, 0, 0); // 2025-01-01 00:00 local
  assert.equal(isoToDatetimeLocal(local.toISOString()), "2025-01-01T00:00");
});

check("isoToDatetimeLocal returns null for bad / empty input", () => {
  assert.equal(isoToDatetimeLocal(undefined), null);
  assert.equal(isoToDatetimeLocal(null), null);
  assert.equal(isoToDatetimeLocal(""), null);
  assert.equal(isoToDatetimeLocal("not-a-date"), null);
});

if (failures.length > 0) {
  console.error(`\n✗ voice approval actions FAILED — ${failures.length} assertion(s):\n`);
  for (const f of failures) console.error(`  - ${f.name}\n      ${f.message}`);
  process.exit(1);
}
console.log(
  `✓ voice approval actions passed — ${passed} checks: focus_reply/open_edit/` +
    `open_schedule/cancel_edit/approvals_cleared/focus_field/regenerate_body ` +
    `dispatch the "${VOICE_APPROVAL_EVENT}" CustomEvent with the correct detail, ` +
    `navigate/unknown are unaffected, and ISO→datetime-local is pure.`,
);
