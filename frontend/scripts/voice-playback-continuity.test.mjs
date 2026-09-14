/**
 * Property 8 verification — Playback scheduling continuity.
 *
 * Feature: voice-navigation-guidance, Property 8 (Req 4.1, 4.2): for any stream
 * of audio chunks, scheduled start times are strictly non-overlapping and
 * gap-free — each buffer starts exactly where the previous one ended (modulo
 * the initial lead), so there are no boundary discontinuities (gaps == clicks)
 * and no overlaps (retro-scheduled glitches).
 *
 * The Playback_Pipeline (src/hooks/use-voice-session.ts) delegates all of its
 * "when does this buffer start" arithmetic to the pure module
 * src/lib/voice-playback.ts (task 4.1). This test exercises that exact math —
 * `scheduleWindows`, `nextStartTime`/`advancePlayHead`, `isPhraseStart`, and
 * the `JitterBuffer` coalescing (`shouldFlush`/`drain`) — so the invariant can
 * be verified without an AudioContext.
 *
 * Framework: this Next.js repo ships no JS test runner (see package.json); the
 * established convention is standalone Node scripts under frontend/scripts/
 * (e.g. oauth-field-visibility.test.mjs). We follow that convention and rely on
 * Node's native TypeScript type-stripping (Node >=22.6) to import the real .ts
 * module.
 *
 * Usage:  node scripts/voice-playback-continuity.test.mjs
 * Exit code is non-zero on any failed assertion so it can gate CI.
 */

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(
  __dirname,
  "..",
  "src",
  "lib",
  "voice-playback.ts",
);

const {
  INITIAL_LEAD,
  COALESCE_TARGET_SECONDS,
  UNDERRUN_SLACK,
  nextStartTime,
  advancePlayHead,
  isPhraseStart,
  scheduleWindows,
  JitterBuffer,
} = await import(MODULE_PATH);

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

// Float comparison helper: audio-timeline arithmetic accumulates tiny FP error,
// so windows that are "the same instant" may differ by ~1e-12. Treat anything
// within EPS as equal; anything beyond it is a real gap/overlap.
const EPS = 1e-9;
const approxEqual = (a, b) => Math.abs(a - b) <= EPS;

/**
 * Assert Property 8 over a computed window list: contiguity (each start == the
 * previous end, no gap and no overlap) and no start scheduled in the past
 * relative to the phrase clock + lead.
 */
function assertContiguousAndForward(windows, startNow, lead, label) {
  assert.ok(windows.length > 0, `${label}: expected at least one window`);

  // The first buffer of a phrase must start no earlier than now + lead — never
  // retro-scheduled into the past.
  assert.ok(
    windows[0].start >= startNow + lead - EPS,
    `${label}: first start ${windows[0].start} scheduled before now+lead ` +
      `${startNow + lead}`,
  );

  for (let i = 0; i < windows.length; i += 1) {
    // Each window must be forward in time (positive duration).
    assert.ok(
      windows[i].end >= windows[i].start - EPS,
      `${label}: window ${i} ends before it starts`,
    );

    if (i + 1 < windows.length) {
      const gap = windows[i + 1].start - windows[i].end;
      // Contiguous: next start == this end. gap ~0 (no gap AND no overlap).
      assert.ok(
        approxEqual(windows[i + 1].start, windows[i].end),
        `${label}: discontinuity between window ${i} and ${i + 1} ` +
          `(gap ${gap}s — must be zero)`,
      );
      // Explicitly: no negative gap (overlap == retro-scheduled glitch).
      assert.ok(
        gap >= -EPS,
        `${label}: negative gap (overlap) of ${gap}s between window ` +
          `${i} and ${i + 1}`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// Property 8 (core): a stream of small chunks schedules contiguously, gap-free.
// ---------------------------------------------------------------------------

// A stream of small, uneven chunk durations (seconds) — the realistic case:
// many tiny PCM segments arriving mid-phrase.
const SMALL_CHUNKS = [
  0.02, 0.02, 0.021, 0.019, 0.02, 0.018, 0.022, 0.02, 0.02, 0.017, 0.023, 0.02,
];

check("small-chunk stream schedules strictly contiguous, gap-free windows", () => {
  const startNow = 3.14159;
  const windows = scheduleWindows(SMALL_CHUNKS, startNow, INITIAL_LEAD);
  assert.equal(windows.length, SMALL_CHUNKS.length);
  assertContiguousAndForward(windows, startNow, INITIAL_LEAD, "small-chunk");

  // Total scheduled span equals sum of durations (no time lost or added
  // between chunks) and the last end equals first start + total duration.
  const total = SMALL_CHUNKS.reduce((a, b) => a + b, 0);
  assert.ok(
    approxEqual(windows[windows.length - 1].end, windows[0].start + total),
    "cumulative end must equal first start + total duration",
  );
});

check("first chunk of a phrase starts at exactly now + lead", () => {
  const startNow = 10;
  const windows = scheduleWindows([0.02, 0.02, 0.02], startNow, INITIAL_LEAD);
  assert.ok(
    approxEqual(windows[0].start, startNow + INITIAL_LEAD),
    `first start ${windows[0].start} should equal now+lead ${startNow + INITIAL_LEAD}`,
  );
  // Each subsequent start equals the running play head.
  assert.ok(approxEqual(windows[1].start, windows[0].end));
  assert.ok(approxEqual(windows[2].start, windows[1].end));
});

check("many randomized small-chunk streams stay contiguous (property sweep)", () => {
  // Deterministic PRNG so failures reproduce (no external dependency).
  let seed = 0x9e3779b9;
  const rand = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 0x100000000;
  };

  for (let trial = 0; trial < 500; trial += 1) {
    const n = 1 + Math.floor(rand() * 40);
    const durations = Array.from(
      { length: n },
      // small chunks: 5 ms .. 45 ms
      () => 0.005 + rand() * 0.04,
    );
    const startNow = rand() * 1000;
    const lead = INITIAL_LEAD;
    const windows = scheduleWindows(durations, startNow, lead);
    assertContiguousAndForward(windows, startNow, lead, `trial ${trial}`);
  }
});

check("varying the initial lead only shifts the phrase, never breaks contiguity", () => {
  for (const lead of [0, 0.01, 0.12, 0.5, 1.0]) {
    const startNow = 42;
    const windows = scheduleWindows(SMALL_CHUNKS, startNow, lead);
    assertContiguousAndForward(windows, startNow, lead, `lead=${lead}`);
    assert.ok(
      approxEqual(windows[0].start, startNow + lead),
      `lead=${lead}: first start should honor the initial lead`,
    );
  }
});

// ---------------------------------------------------------------------------
// Underrun re-floor: a play head that has fallen behind the clock re-floors to
// now + lead rather than scheduling a start in the past.
// ---------------------------------------------------------------------------

check("nextStartTime re-floors a behind play head to now + lead", () => {
  const now = 100;
  // Play head is well behind the clock (a stall / silence gap occurred).
  const behind = now - 5;
  const start = nextStartTime(behind, now, INITIAL_LEAD);
  assert.ok(
    approxEqual(start, now + INITIAL_LEAD),
    `behind play head should re-floor to now+lead, got ${start}`,
  );
  assert.ok(start >= now, "re-floored start must not be in the past");
});

check("nextStartTime keeps an ahead play head contiguous (no overlap)", () => {
  const now = 100;
  const ahead = now + 0.5; // audio still queued ahead of the clock
  const start = nextStartTime(ahead, now, INITIAL_LEAD);
  assert.ok(
    approxEqual(start, ahead),
    `ahead play head should start exactly at the play head, got ${start}`,
  );
});

check("isPhraseStart flags a behind play head and clears once ahead", () => {
  const now = 50;
  assert.equal(isPhraseStart(now - 1, now), true, "behind => phrase start");
  assert.equal(isPhraseStart(now + 1, now), false, "ahead => mid phrase");
  // Within the underrun slack the play head is still considered behind.
  assert.equal(
    isPhraseStart(now + UNDERRUN_SLACK / 2, now),
    true,
    "inside slack still counts as behind",
  );
});

check("advancePlayHead advances by exactly the buffer duration", () => {
  assert.ok(approxEqual(advancePlayHead(10, 0.25), 10.25));
  assert.ok(approxEqual(advancePlayHead(0, 0), 0));
});

check("an underrun mid-stream re-floors forward and stays contiguous after", () => {
  // Simulate: schedule a phrase, then the clock advances past the play head
  // (a stall), and a new chunk arrives — it must re-floor to now+lead and the
  // subsequent chunks stay contiguous from there.
  const lead = INITIAL_LEAD;
  const firstNow = 0;
  const firstPhrase = scheduleWindows([0.02, 0.02, 0.02], firstNow, lead);
  const playHeadAfter = firstPhrase[firstPhrase.length - 1].end;

  // Clock jumps far beyond the play head (underrun after silence).
  const laterNow = playHeadAfter + 2.0;
  const resumeStart = nextStartTime(playHeadAfter, laterNow, lead);
  assert.ok(
    approxEqual(resumeStart, laterNow + lead),
    "resumed start after underrun must re-floor to now+lead",
  );
  assert.ok(
    resumeStart >= laterNow,
    "resumed start must never be scheduled in the past",
  );

  // Continue scheduling from the re-floored head: still contiguous.
  let head = advancePlayHead(resumeStart, 0.02);
  const resumed = [{ start: resumeStart, end: head }];
  for (const dur of [0.02, 0.02]) {
    const s = nextStartTime(head, laterNow, lead);
    const e = advancePlayHead(s, dur);
    resumed.push({ start: s, end: e });
    head = e;
  }
  assertContiguousAndForward(resumed, laterNow, lead, "post-underrun");
});

// ---------------------------------------------------------------------------
// JitterBuffer coalescing: shouldFlush / drain behavior underpinning the
// "fewer, larger buffers" scheduling that keeps windows contiguous.
// ---------------------------------------------------------------------------

check("JitterBuffer flushes once the coalesce target is reached", () => {
  const rate = 24000; // 24 kHz PCM
  const jb = new JitterBuffer();
  assert.equal(jb.isEmpty, true);
  assert.equal(jb.shouldFlush(COALESCE_TARGET_SECONDS), false);

  // Push 20 ms chunks; target is ~120 ms, so it should flush at/after 6 chunks.
  const chunkSamples = Math.round(rate * 0.02);
  let flushedAt = -1;
  for (let i = 1; i <= 12; i += 1) {
    jb.push(new Float32Array(chunkSamples), rate);
    if (flushedAt === -1 && jb.shouldFlush(COALESCE_TARGET_SECONDS)) {
      flushedAt = i;
    }
  }
  assert.ok(flushedAt >= 1, "buffer must eventually report shouldFlush");
  // 6 * 20ms = 120ms exactly reaches the 0.12s target.
  assert.equal(flushedAt, 6, `expected flush at 120ms (chunk 6), got ${flushedAt}`);
});

check("JitterBuffer.drain concatenates in order and clears state", () => {
  const rate = 16000;
  const jb = new JitterBuffer();
  jb.push(Float32Array.from([1, 2, 3]), rate);
  jb.push(Float32Array.from([4, 5]), rate);
  jb.push(Float32Array.from([6]), rate);

  const drained = jb.drain();
  assert.notEqual(drained, null);
  assert.equal(drained.sampleRate, rate);
  assert.deepEqual(Array.from(drained.samples), [1, 2, 3, 4, 5, 6]);

  // Fully cleared after drain.
  assert.equal(jb.isEmpty, true);
  assert.equal(jb.sampleRate, null);
  assert.equal(jb.drain(), null, "draining an empty buffer returns null");
});

check("the coalesced buffer's duration equals the total pushed audio", () => {
  // The scheduler uses drained.samples.length / rate as the buffer duration;
  // that must equal the sum of the pushed chunk durations so windows stay
  // gap-free after coalescing.
  const rate = 24000;
  const jb = new JitterBuffer();
  const chunkDurations = [0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.03];
  let totalSamples = 0;
  for (const dur of chunkDurations) {
    const n = Math.round(rate * dur);
    totalSamples += n;
    jb.push(new Float32Array(n), rate);
  }
  const drained = jb.drain();
  assert.equal(drained.samples.length, totalSamples);
  const coalescedDuration = drained.samples.length / rate;

  // Scheduling the single coalesced buffer plus a following one stays contiguous.
  const startNow = 7;
  const windows = scheduleWindows(
    [coalescedDuration, 0.04],
    startNow,
    INITIAL_LEAD,
  );
  assertContiguousAndForward(windows, startNow, INITIAL_LEAD, "coalesced");
});

check("JitterBuffer rejects a mismatched sample rate so the caller drains first", () => {
  const jb = new JitterBuffer();
  assert.equal(jb.push(Float32Array.from([1, 2]), 24000), true);
  // A rate change must be refused (false) rather than silently mixed.
  assert.equal(jb.push(Float32Array.from([3]), 16000), false);
  // Empty pushes are a no-op success.
  assert.equal(jb.push(new Float32Array(0), 16000), true);
});

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------

if (failures.length > 0) {
  console.error(`\n✗ Property 8 FAILED — ${failures.length} assertion(s):\n`);
  for (const f of failures) {
    console.error(`  - ${f.name}\n      ${f.message}`);
  }
  process.exit(1);
}

console.log(
  `✓ Property 8 passed — ${passed} checks: scheduled windows are contiguous ` +
    `and gap-free (incl. underrun re-floor and jitter-buffer coalescing).`,
);
