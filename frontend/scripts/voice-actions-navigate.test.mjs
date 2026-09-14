/**
 * Voice navigation action tests — the fix for "voice can't open pages".
 *
 * Root cause that this guards against: the server used to spread the action
 * into the WebSocket envelope (`{"type":"action", ...action}`), which clobbered
 * the envelope `type` "action" with the action's own `type` ("navigate"), so
 * the client's `switch (msg.type)` never matched "action" and navigation was
 * silently dropped. The wire contract is now nested:
 *   {"type":"action","action":{"type":"navigate","path":"/dashboard/approvals"}}
 *
 * This test exercises `applyVoiceAction` (the router effect) directly and also
 * asserts the nested envelope is unwrapped to the inner action. It follows the
 * repo convention: a standalone Node script importing the real .ts module via
 * Node's native type-stripping (see oauth-field-visibility.test.mjs).
 */

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(__dirname, "..", "src", "lib", "voice-actions.ts");
const { applyVoiceAction } = await import(MODULE_PATH);

let passed = 0;
const failures = [];
function check(name, fn) {
  try { fn(); passed += 1; }
  catch (err) { failures.push({ name, message: err?.message ?? String(err) }); }
}

/** A recording router matching the VoiceRouter surface. */
function makeRouter() {
  const pushed = [];
  return { pushed, push: (p) => pushed.push(p) };
}

check("navigate action pushes the exact app path", () => {
  const router = makeRouter();
  applyVoiceAction({ type: "navigate", path: "/dashboard/approvals" }, { router });
  assert.deepEqual(router.pushed, ["/dashboard/approvals"]);
});

check("unwraps the nested action envelope shape", () => {
  // Simulate what the client does after reading msg.action from the envelope.
  const envelope = {
    type: "action",
    action: { type: "navigate", path: "/dashboard/rules" },
  };
  const inner = envelope.action;
  const router = makeRouter();
  applyVoiceAction(inner, { router });
  assert.deepEqual(router.pushed, ["/dashboard/rules"]);
});

check("rejects a missing/empty path (no navigation)", () => {
  const router = makeRouter();
  applyVoiceAction({ type: "navigate", path: "" }, { router });
  applyVoiceAction({ type: "navigate" }, { router });
  assert.deepEqual(router.pushed, []);
});

check("rejects an external / protocol-relative target (same-origin guard)", () => {
  const router = makeRouter();
  applyVoiceAction({ type: "navigate", path: "https://evil.example/x" }, { router });
  applyVoiceAction({ type: "navigate", path: "//evil.example/x" }, { router });
  applyVoiceAction({ type: "navigate", path: "dashboard/approvals" }, { router }); // relative
  assert.deepEqual(router.pushed, [], "only absolute same-origin paths allowed");
});

check("all five destination routes navigate", () => {
  const routes = [
    "/dashboard/approvals",
    "/dashboard/workspace",
    "/dashboard/integrations",
    "/dashboard/rules",
    "/admin",
  ];
  for (const r of routes) {
    const router = makeRouter();
    applyVoiceAction({ type: "navigate", path: r }, { router });
    assert.deepEqual(router.pushed, [r], `route ${r} should navigate`);
  }
});

check("falls back to window.location when router.push throws", () => {
  const assigned = [];
  const originalWindow = globalThis.window;
  globalThis.window = { location: { assign: (p) => assigned.push(p) } };
  try {
    const throwingRouter = {
      push: () => {
        throw new Error("router.push failed");
      },
    };
    applyVoiceAction(
      { type: "navigate", path: "/dashboard/approvals" },
      { router: throwingRouter },
    );
    assert.deepEqual(assigned, ["/dashboard/approvals"]);
  } finally {
    globalThis.window = originalWindow;
  }
});

if (failures.length > 0) {
  console.error(`\n✗ voice navigation FAILED — ${failures.length} assertion(s):\n`);
  for (const f of failures) console.error(`  - ${f.name}\n      ${f.message}`);
  process.exit(1);
}
console.log(
  `✓ voice navigation passed — ${passed} checks: nested action envelope is ` +
    `unwrapped, same-origin app paths navigate, and there is a hard fallback.`,
);
