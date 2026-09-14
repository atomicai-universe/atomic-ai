/**
 * voice-playback — pure scheduling math for the voice Playback_Pipeline.
 *
 * This module holds the side-effect-free arithmetic that decides *when* each
 * decoded audio buffer should start playing on the AudioContext timeline, and
 * *how* the incoming PCM chunk stream is coalesced into fewer, larger buffers.
 * It touches no Web Audio APIs, no DOM, and no timers, so it is deterministic
 * and unit-testable in plain Node (see the companion test for Property 8).
 *
 * The hook (`use-voice-session.ts`) owns the real AudioContext, the
 * AudioBufferSourceNodes, and the GainNode; it delegates the numeric decisions
 * here so the "no gaps / no overlaps" invariant can be verified without audio
 * hardware.
 *
 * Design goals (voice-navigation-guidance, Req 4.1/4.2, Property 8):
 *   - Coalesce many tiny chunks into ~120 ms buffers so there are far fewer
 *     clickable seams.
 *   - Schedule buffers strictly contiguously: each buffer starts exactly where
 *     the previous one ended (modulo the initial lead), so there are no gaps
 *     (gaps == clicks) and no overlaps (overlaps == retro-scheduled glitches).
 *   - When the play head falls behind the clock (an underrun after silence or a
 *     stall), re-floor it to `currentTime + lead` so we never schedule a start
 *     in the past.
 */

/** Minimum lead (seconds) between "now" and the first scheduled start. */
export const INITIAL_LEAD = 0.12;

/**
 * Target amount of buffered audio (seconds) before the jitter buffer flushes a
 * coalesced buffer. ~120 ms trades a little added latency for far fewer seams.
 */
export const COALESCE_TARGET_SECONDS = 0.12;

/**
 * Slack (seconds) used when deciding whether the play head is "behind" the
 * clock. Starts fresh (re-floor to now + lead) only when we've clearly fallen
 * behind, not on sub-millisecond float noise.
 */
export const UNDERRUN_SLACK = 0.005;

/**
 * Decide the start time for the next buffer given the current play head, the
 * AudioContext clock, and an initial lead.
 *
 * Contiguous scheduling with an underrun guard:
 *   - If the play head is still ahead of the clock (we're mid-phrase and audio
 *     is queued), start exactly at the play head so the new buffer butts up
 *     against the previous one with no gap and no overlap.
 *   - If the play head has fallen behind (first buffer of a phrase, or an
 *     underrun after a stall), re-floor to `now + lead` so we never schedule a
 *     start in the past (which the engine renders as a discontinuity).
 *
 * Pure: returns the chosen start time; the caller advances the play head with
 * {@link advancePlayHead}.
 */
export function nextStartTime(
  playHead: number,
  now: number,
  lead: number = INITIAL_LEAD,
): number {
  if (playHead < now + UNDERRUN_SLACK) {
    return now + lead;
  }
  return playHead;
}

/** The play head after scheduling a buffer of `duration` seconds at `startAt`. */
export function advancePlayHead(startAt: number, duration: number): number {
  return startAt + duration;
}

/** True when a buffer is the first of a phrase (nothing currently scheduled). */
export function isPhraseStart(playHead: number, now: number): boolean {
  return playHead < now + UNDERRUN_SLACK;
}

/**
 * Given a sequence of chunk durations (seconds) arriving while a phrase is
 * playing, compute the scheduled [start, end) window for each chunk. This is
 * the exact math the hook uses, exposed for testing Property 8.
 *
 * @param durations per-chunk durations in seconds (in arrival order)
 * @param startNow the AudioContext.currentTime at the first chunk
 * @param lead the initial scheduling lead
 * @returns one `{ start, end }` per input duration, in order
 */
export function scheduleWindows(
  durations: readonly number[],
  startNow: number,
  lead: number = INITIAL_LEAD,
): Array<{ start: number; end: number }> {
  const out: Array<{ start: number; end: number }> = [];
  // Play head starts "behind" so the first chunk gets the initial lead.
  let playHead = startNow;
  for (const dur of durations) {
    const start = nextStartTime(playHead, startNow, lead);
    const end = advancePlayHead(start, dur);
    out.push({ start, end });
    playHead = end;
  }
  return out;
}

/**
 * A tiny jitter buffer that accumulates Float32 PCM samples (all at one sample
 * rate) and flushes a single coalesced buffer once enough audio is queued.
 *
 * The buffer is intentionally minimal and synchronous: `push` appends samples,
 * `shouldFlush` reports whether the target fill has been reached, and `drain`
 * concatenates and clears. The hook pairs this with a short fallback timer so a
 * trailing partial buffer (end of a phrase) is still flushed promptly.
 *
 * Pure data structure — no audio, no timers.
 */
export class JitterBuffer {
  private parts: Float32Array[] = [];
  private queued = 0;
  private rate: number | null = null;

  /**
   * Append a chunk of samples. All chunks must share one sample rate for a
   * given phrase; a rate change forces the caller to flush first (returns
   * false so the caller can drain the previous rate before switching).
   */
  push(samples: Float32Array, sampleRate: number): boolean {
    if (samples.length === 0) return true;
    if (this.rate !== null && this.rate !== sampleRate) {
      return false;
    }
    this.rate = sampleRate;
    this.parts.push(samples);
    this.queued += samples.length;
    return true;
  }

  /** Total buffered duration in seconds (0 when empty or rate unknown). */
  bufferedSeconds(): number {
    if (this.rate === null || this.rate <= 0) return 0;
    return this.queued / this.rate;
  }

  /** Whether at least `targetSeconds` of audio is buffered. */
  shouldFlush(targetSeconds: number = COALESCE_TARGET_SECONDS): boolean {
    return this.bufferedSeconds() >= targetSeconds;
  }

  /** Whether any samples are currently buffered. */
  get isEmpty(): boolean {
    return this.queued === 0;
  }

  /** The sample rate of the currently buffered audio (null when empty). */
  get sampleRate(): number | null {
    return this.rate;
  }

  /**
   * Concatenate all buffered samples into one Float32Array and clear the
   * buffer. Returns `null` when nothing is buffered.
   */
  drain(): { samples: Float32Array; sampleRate: number } | null {
    if (this.queued === 0 || this.rate === null) return null;
    const merged = new Float32Array(this.queued);
    let offset = 0;
    for (const part of this.parts) {
      merged.set(part, offset);
      offset += part.length;
    }
    const rate = this.rate;
    this.parts = [];
    this.queued = 0;
    this.rate = null;
    return { samples: merged, sampleRate: rate };
  }
}
