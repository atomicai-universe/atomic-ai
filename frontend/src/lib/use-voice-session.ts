"use client";

/**
 * useVoiceSession — the browser side of the Atomic AI voice pipeline (Phase 2).
 *
 * Connects the microphone/speaker to the backend voice WebSocket built in
 * Phase 1 (Nova Sonic contract):
 *
 *   WS: `${API_BASE_URL(ws/wss)}/api/v1/voice/stream?workspace_id=<uuid>`
 *   Auth: the browser sends the HttpOnly `session` cookie automatically on the
 *   WS handshake (same-origin/CORS permitting). We do not hold a token in JS.
 *
 * ── Audio pipeline ─────────────────────────────────────────────────────────
 * Capture (client → server):
 *   mic (native 44.1/48 kHz) → AudioWorklet `voice-capture-processor`
 *   (downsample → 16 kHz mono, Int16LE) → BINARY WebSocket frames.
 *   A ScriptProcessorNode path is used as a fallback when AudioWorklet is
 *   unavailable; it downsamples + converts on the main thread.
 *
 * Playback (server → client):
 *   `{type:"audio", data:<base64 pcm>}` → Int16 → Float32 → accumulated in a
 *   small jitter buffer and flushed as fewer, larger AudioBuffers (~120 ms) so
 *   there are far fewer clickable seams. Buffers are scheduled strictly
 *   contiguously (each starts where the previous ends, modulo an initial lead)
 *   through a single continuous GainNode that ramps in only on the first buffer
 *   of a phrase and ramps out only when the queue drains — so mid-phrase seams
 *   are sample-continuous. Playback runs at the server-declared `sample_rate`.
 *   The scheduling math lives in the pure `voice-playback` module. While audio
 *   is queued status is "speaking"; it returns to "listening" when the queue
 *   drains.
 *
 * Barge-in: when the user speaks again (or an `{type:"interrupt"}` action
 * arrives) we flush the playback queue with a short fade-out (not a hard stop)
 * so the assistant stops talking without a click.
 *
 * Everything guards for SSR and degrades gracefully: a denied mic permission or
 * an auth failure (close codes 4401/4403/4404) becomes a friendly `error`
 * string rather than a crash.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE_URL, getActiveWorkspaceId } from "@/lib/api";
import type { VoiceAction } from "@/lib/voice-actions";
import {
  COALESCE_TARGET_SECONDS,
  INITIAL_LEAD,
  JitterBuffer,
  advancePlayHead,
  isPhraseStart,
  nextStartTime,
} from "@/lib/voice-playback";

/** Path the backend mounts the voice stream at. */
export const VOICE_STREAM_PATH = "/api/v1/voice/stream";

/** Sample rates fixed by the backend contract. */
const CAPTURE_RATE = 16000; // outbound to server
const PLAYBACK_RATE = 16000; // inbound from server (Nova Sonic/Strands default)

// Barge-in tuning: outbound mic RMS (0..1) must exceed BARGE_IN_RMS for
// BARGE_IN_FRAMES consecutive frames before we interrupt the assistant's
// playback. Conservative so speaker bleed / a single blip never chops it.
const BARGE_IN_RMS = 0.08;
const BARGE_IN_FRAMES = 3;

/** Application WS close codes from the backend. */
const CLOSE_UNAUTHORIZED = 4401; // not signed in
const CLOSE_FORBIDDEN = 4403; // not a member of the workspace
const CLOSE_VOICE_DISABLED = 4404; // voice disabled server-side

const MAX_TRANSCRIPT = 20;

export type VoiceStatus =
  | "idle"
  | "connecting"
  | "listening"
  | "speaking"
  | "error";

export interface TranscriptLine {
  role: "user" | "assistant";
  text: string;
}

export interface UseVoiceSessionOptions {
  /** Called for every server `{"type":"action"}` frame (navigate/type/submit). */
  onAction?: (action: VoiceAction) => void;
}

export interface UseVoiceSessionResult {
  status: VoiceStatus;
  transcript: TranscriptLine[];
  error: string | null;
  /** True whenever a session is live (connecting/listening/speaking). */
  active: boolean;
  start: () => Promise<void>;
  stop: () => void;
}

/** Derive the WebSocket origin from the HTTP API base (http→ws, https→wss). */
function deriveWsBase(apiBase: string): string {
  if (apiBase.startsWith("https://")) return "wss://" + apiBase.slice("https://".length);
  if (apiBase.startsWith("http://")) return "ws://" + apiBase.slice("http://".length);
  return apiBase;
}

/** Decode a base64 string to a Uint8Array (browser-safe). */
function base64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Int16LE PCM bytes → Float32 samples in [-1, 1]. */
function pcm16ToFloat32(bytes: Uint8Array): Float32Array {
  // Respect byteOffset in case the underlying buffer is shared/offset.
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const sampleCount = Math.floor(bytes.byteLength / 2);
  const out = new Float32Array(sampleCount);
  for (let i = 0; i < sampleCount; i++) {
    const s = view.getInt16(i * 2, true); // little-endian
    out[i] = s < 0 ? s / 0x8000 : s / 0x7fff;
  }
  return out;
}

/** Normalized RMS amplitude (0..1) of an Int16LE PCM ArrayBuffer. */
function int16Rms(buffer: ArrayBuffer): number {
  const view = new DataView(buffer);
  const n = Math.floor(buffer.byteLength / 2);
  if (n === 0) return 0;
  let sumSq = 0;
  for (let i = 0; i < n; i++) {
    const s = view.getInt16(i * 2, true) / 0x8000; // normalize to [-1, 1)
    sumSq += s * s;
  }
  return Math.sqrt(sumSq / n);
}

/** Float32 [-1,1] → Int16LE ArrayBuffer (used by the ScriptProcessor fallback). */
function float32ToInt16(samples: Float32Array): ArrayBuffer {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    let s = samples[i];
    if (s > 1) s = 1;
    else if (s < -1) s = -1;
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out.buffer;
}

/** Linear-interpolation downsample from `inRate` to `outRate` (mono). */
function downsample(samples: Float32Array, inRate: number, outRate: number): Float32Array {
  if (outRate >= inRate) return samples;
  const ratio = inRate / outRate;
  const outLen = Math.floor(samples.length / ratio);
  const out = new Float32Array(outLen);
  let pos = 0;
  for (let i = 0; i < outLen; i++) {
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const next = idx + 1 < samples.length ? samples[idx + 1] : samples[idx];
    out[i] = samples[idx] * (1 - frac) + next * frac;
    pos += ratio;
  }
  return out;
}

// A cross-vendor AudioContext constructor (Safari prefixes it).
type AudioContextCtor = typeof AudioContext;
function getAudioContextCtor(): AudioContextCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    AudioContext?: AudioContextCtor;
    webkitAudioContext?: AudioContextCtor;
  };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

export function useVoiceSession(
  options: UseVoiceSessionOptions = {},
): UseVoiceSessionResult {
  const [status, setStatus] = useState<VoiceStatus>("idle");
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Keep the latest onAction in a ref so the socket handlers don't go stale.
  const onActionRef = useRef<UseVoiceSessionOptions["onAction"]>(options.onAction);
  useEffect(() => {
    onActionRef.current = options.onAction;
  }, [options.onAction]);

  // Live resource refs (not state — mutated imperatively during a session).
  const wsRef = useRef<WebSocket | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const scriptNodeRef = useRef<ScriptProcessorNode | null>(null);
  const sourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null);

  // Playback scheduling: `playHead` is the AudioContext time at which the next
  // queued buffer should start; `sources` tracks scheduled buffer sources so we
  // can fade/stop them on barge-in.
  const playHeadRef = useRef(0);
  const activeSourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set());
  const speakingRef = useRef(false);
  // Jitter buffer: coalesces the many tiny PCM chunks Nova Sonic streams into
  // fewer, larger AudioBuffers (~120 ms) so there are far fewer clickable
  // seams. A short fallback timer flushes a trailing partial buffer promptly.
  const jitterRef = useRef<JitterBuffer>(new JitterBuffer());
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A single continuous phrase gain: ramps in on the first buffer of a phrase
  // and ramps out only when the queue drains, so mid-phrase seams are
  // sample-continuous rather than gated per chunk.
  const phraseGainRef = useRef<GainNode | null>(null);
  // Consecutive loud outbound frames observed while the assistant is
  // speaking. Barge-in only fires after several (debounced) so speaker
  // bleed / a single blip never chops the assistant's audio.
  const bargeInFramesRef = useRef(0);

  // Guards a stale async start() from touching torn-down resources.
  const sessionIdRef = useRef(0);

  const appendTranscript = useCallback((line: TranscriptLine) => {
    setTranscript((prev) => {
      const next = [...prev, line];
      return next.length > MAX_TRANSCRIPT ? next.slice(next.length - MAX_TRANSCRIPT) : next;
    });
  }, []);

  /** Fade-out length (seconds) applied to in-flight audio on a barge-in flush. */
  const FLUSH_FADE = 0.02;

  /**
   * Stop and clear all scheduled playback (barge-in / stop).
   *
   * On barge-in we apply a short fade-out on the phrase gain and stop the
   * sources just after the fade completes, rather than an immediate hard
   * `stop()`, so interrupting the assistant does not produce an audible click
   * (Req 4.3). The RMS + debounce barge-in *policy* is unchanged; only the
   * flush is gentler.
   */
  const flushPlayback = useCallback(() => {
    const ctx = audioCtxRef.current;
    // Drop any buffered-but-unscheduled audio so it can't be played after the
    // interruption.
    jitterRef.current.drain();
    if (flushTimerRef.current !== null) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }

    const gain = phraseGainRef.current;
    if (ctx && gain && activeSourcesRef.current.size > 0) {
      const now = ctx.currentTime;
      const stopAt = now + FLUSH_FADE;
      try {
        gain.gain.cancelScheduledValues(now);
        // Anchor at the current value, then ramp to (near) zero.
        gain.gain.setValueAtTime(Math.max(gain.gain.value, 0.0001), now);
        gain.gain.exponentialRampToValueAtTime(0.0001, stopAt);
      } catch {
        /* scheduling may fail on a closing context — fall through to stop */
      }
      for (const src of activeSourcesRef.current) {
        try {
          src.onended = null;
          src.stop(stopAt);
        } catch {
          /* already stopped */
        }
      }
    } else {
      for (const src of activeSourcesRef.current) {
        try {
          src.onended = null;
          src.stop();
        } catch {
          /* already stopped */
        }
      }
    }
    activeSourcesRef.current.clear();
    phraseGainRef.current = null;
    playHeadRef.current = ctx ? ctx.currentTime : 0;
    if (speakingRef.current) {
      speakingRef.current = false;
      setStatus((s) => (s === "speaking" ? "listening" : s));
    }
  }, []);

  /** Ramp length (seconds) for the continuous phrase gain at start/drain. */
  const PHRASE_RAMP = 0.008;

  /**
   * Schedule one coalesced Float32 buffer for contiguous playback.
   *
   * Scheduling math (start time, underrun re-floor, phrase-start detection)
   * comes from the pure `voice-playback` module so it can be unit-tested
   * without audio hardware (Property 8). This function only turns those numbers
   * into Web Audio nodes.
   *
   * Gain is continuous across the phrase: a single shared GainNode ramps in on
   * the first buffer of a phrase and ramps out only when the queue drains, so
   * mid-phrase seams are sample-continuous (no per-chunk gating notches).
   */
  const scheduleBuffer = useCallback((floats: Float32Array, rate: number) => {
    const ctx = audioCtxRef.current;
    if (!ctx || floats.length === 0) return;

    // Play at the rate the server declared. Playing 16 kHz data as 24 kHz made
    // the voice fast + robotic; the context resamples to the hardware rate on
    // playback so pitch stays correct regardless of hardware rate.
    const playRate = rate && rate > 0 ? rate : PLAYBACK_RATE;
    const buffer = ctx.createBuffer(1, floats.length, playRate);
    // Write straight into the channel's backing array to sidestep the
    // Float32Array<ArrayBuffer> vs ArrayBufferLike overload mismatch.
    buffer.getChannelData(0).set(floats);

    const now = ctx.currentTime;
    const phraseStart = isPhraseStart(playHeadRef.current, now);

    // A fresh phrase needs a fresh continuous gain node.
    if (phraseStart || !phraseGainRef.current) {
      const g = ctx.createGain();
      g.gain.setValueAtTime(0, now);
      g.connect(ctx.destination);
      phraseGainRef.current = g;
    }
    const gain = phraseGainRef.current;

    // Contiguous start with underrun guard: start exactly at the running play
    // head, or re-floor to now + lead when we've fallen behind the clock.
    const startAt = nextStartTime(playHeadRef.current, now, INITIAL_LEAD);
    const dur = buffer.duration;

    // Ramp the CONTINUOUS gain in only at the phrase start; hold at unity for
    // subsequent buffers so seams are sample-continuous.
    if (phraseStart) {
      const ramp = Math.min(PHRASE_RAMP, dur / 2);
      gain.gain.setValueAtTime(0, startAt);
      gain.gain.linearRampToValueAtTime(1, startAt + ramp);
    } else {
      // Ensure gain is (still) at unity mid-phrase without introducing a step.
      gain.gain.setValueAtTime(1, startAt);
    }

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(gain);
    src.start(startAt);
    playHeadRef.current = advancePlayHead(startAt, dur);

    activeSourcesRef.current.add(src);
    if (!speakingRef.current) {
      speakingRef.current = true;
      setStatus("speaking");
    }
    src.onended = () => {
      activeSourcesRef.current.delete(src);
      if (activeSourcesRef.current.size === 0 && speakingRef.current) {
        // Queue drained: ramp the continuous gain out to avoid a tail click.
        const g = phraseGainRef.current;
        const c = audioCtxRef.current;
        if (g && c) {
          const t = c.currentTime;
          try {
            g.gain.cancelScheduledValues(t);
            g.gain.setValueAtTime(Math.max(g.gain.value, 0.0001), t);
            g.gain.exponentialRampToValueAtTime(0.0001, t + PHRASE_RAMP);
          } catch {
            /* closing context — ignore */
          }
        }
        phraseGainRef.current = null;
        speakingRef.current = false;
        setStatus((s) => (s === "speaking" ? "listening" : s));
      }
    };
  }, []);

  /** Drain the jitter buffer and schedule whatever is currently buffered. */
  const flushJitter = useCallback(() => {
    if (flushTimerRef.current !== null) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    const drained = jitterRef.current.drain();
    if (drained) scheduleBuffer(drained.samples, drained.sampleRate);
  }, [scheduleBuffer]);

  /**
   * Accept a server PCM chunk. Chunks are coalesced in the jitter buffer and
   * flushed as fewer, larger buffers once ~120 ms is buffered; a short fallback
   * timer flushes a trailing partial buffer so end-of-phrase audio isn't held.
   */
  const enqueuePlayback = useCallback(
    (bytes: Uint8Array, sampleRate: number) => {
      const ctx = audioCtxRef.current;
      if (!ctx) return;
      const floats = pcm16ToFloat32(bytes);
      if (floats.length === 0) return;
      const rate = sampleRate && sampleRate > 0 ? sampleRate : PLAYBACK_RATE;

      // A sample-rate change can't be coalesced with the pending audio: flush
      // what we have first, then buffer the new-rate samples.
      if (!jitterRef.current.push(floats, rate)) {
        flushJitter();
        jitterRef.current.push(floats, rate);
      }

      if (jitterRef.current.shouldFlush(COALESCE_TARGET_SECONDS)) {
        flushJitter();
        return;
      }

      // Not enough buffered yet — arm a short timer so a trailing partial
      // buffer is still played promptly (~1 coalesce window).
      if (flushTimerRef.current === null) {
        flushTimerRef.current = setTimeout(() => {
          flushTimerRef.current = null;
          flushJitter();
        }, Math.ceil(COALESCE_TARGET_SECONDS * 1000));
      }
    },
    [flushJitter],
  );

  /** Send an outbound 16 kHz Int16 audio chunk as a BINARY frame. */
  const sendAudioChunk = useCallback((buffer: ArrayBuffer) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    // Barge-in — ONLY when the user genuinely speaks OVER the assistant, not on
    // every mic frame (that tore the assistant's audio apart -> "crack"). We
    // require several consecutive frames whose RMS clears a threshold so
    // speaker bleed / a single blip never interrupts playback.
    if (speakingRef.current) {
      const rms = int16Rms(buffer);
      if (rms >= BARGE_IN_RMS) {
        bargeInFramesRef.current += 1;
        if (bargeInFramesRef.current >= BARGE_IN_FRAMES) {
          flushPlayback();
          bargeInFramesRef.current = 0;
        }
      } else {
        bargeInFramesRef.current = 0;
      }
    } else {
      bargeInFramesRef.current = 0;
    }

    // Always stream the mic to the server regardless of barge-in state.
    ws.send(buffer);
  }, [flushPlayback]);

  /** Tear down every live resource. Safe to call multiple times. */
  const teardown = useCallback(() => {
    // Invalidate any in-flight start().
    sessionIdRef.current += 1;

    if (flushTimerRef.current !== null) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    jitterRef.current.drain();
    flushPlayback();

    const ws = wsRef.current;
    wsRef.current = null;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      try {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "stop" }));
        }
      } catch {
        /* ignore */
      }
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    }

    if (workletNodeRef.current) {
      try {
        workletNodeRef.current.port.postMessage({ type: "stop" });
        workletNodeRef.current.port.onmessage = null;
        workletNodeRef.current.disconnect();
      } catch {
        /* ignore */
      }
      workletNodeRef.current = null;
    }
    if (scriptNodeRef.current) {
      try {
        scriptNodeRef.current.onaudioprocess = null;
        scriptNodeRef.current.disconnect();
      } catch {
        /* ignore */
      }
      scriptNodeRef.current = null;
    }
    if (sourceNodeRef.current) {
      try {
        sourceNodeRef.current.disconnect();
      } catch {
        /* ignore */
      }
      sourceNodeRef.current = null;
    }
    if (streamRef.current) {
      for (const track of streamRef.current.getTracks()) {
        try {
          track.stop();
        } catch {
          /* ignore */
        }
      }
      streamRef.current = null;
    }
    if (audioCtxRef.current) {
      const ctx = audioCtxRef.current;
      audioCtxRef.current = null;
      try {
        void ctx.close();
      } catch {
        /* ignore */
      }
    }
    playHeadRef.current = 0;
    speakingRef.current = false;
  }, [flushPlayback]);

  /** Handle a parsed server → client JSON message. */
  const handleServerMessage = useCallback(
    (mySession: number, data: unknown) => {
      if (sessionIdRef.current !== mySession) return;
      if (!data || typeof data !== "object") return;
      const msg = data as { type?: unknown; [key: string]: unknown };
      switch (msg.type) {
        case "ready": {
          setStatus("listening");
          return;
        }
        case "audio": {
          if (typeof msg.data === "string") {
            try {
              const rate = typeof msg.sample_rate === "number" ? msg.sample_rate : PLAYBACK_RATE;
              enqueuePlayback(base64ToBytes(msg.data), rate);
            } catch {
              /* bad base64 — skip this chunk */
            }
          }
          return;
        }
        case "transcript": {
          const role = msg.role === "assistant" ? "assistant" : "user";
          const text = typeof msg.text === "string" ? msg.text : "";
          if (text) appendTranscript({ role, text });
          return;
        }
        case "action": {
          // The server nests the action under an "action" key:
          //   {"type":"action","action":{"type":"navigate","path":"..."}}
          // (A previous shape spread the action into the envelope, which
          // clobbered the envelope "type" and made this case never match, so
          // navigation silently failed.) Fall back to the flat message for
          // backward compatibility with any older server frame.
          const raw =
            msg.action && typeof msg.action === "object"
              ? (msg.action as unknown)
              : (msg as unknown);
          const action = raw as VoiceAction;
          if (!action || typeof action.type !== "string") return;
          // interrupt is a barge-in: flush playback locally.
          if (action.type === "interrupt") {
            flushPlayback();
          }
          try {
            onActionRef.current?.(action);
          } catch {
            /* action handler must never crash the socket */
          }
          return;
        }
        case "error": {
          const message = typeof msg.message === "string" ? msg.message : "Voice error.";
          setError(message);
          // Surface the failure in the UI: without this the status stays on
          // "connecting" and the subsequent normal socket close resets it to
          // "idle", hiding the error. Marking "error" keeps the message shown.
          setStatus("error");
          return;
        }
        default:
          return;
      }
    },
    [appendTranscript, enqueuePlayback, flushPlayback],
  );

  /** Attach the mic capture graph (AudioWorklet preferred, ScriptProcessor fallback). */
  const attachCapture = useCallback(
    async (ctx: AudioContext, stream: MediaStream) => {
      const source = ctx.createMediaStreamSource(stream);
      sourceNodeRef.current = source;

      const canUseWorklet =
        typeof AudioWorkletNode !== "undefined" && !!ctx.audioWorklet;

      if (canUseWorklet) {
        try {
          await ctx.audioWorklet.addModule("/voice-capture-worklet.js");
          const node = new AudioWorkletNode(ctx, "voice-capture-processor");
          node.port.onmessage = (e: MessageEvent) => {
            if (e.data instanceof ArrayBuffer) {
              sendAudioChunk(e.data);
            }
          };
          source.connect(node);
          // Worklet doesn't need to reach the destination; it only posts data.
          workletNodeRef.current = node;
          return;
        } catch {
          // Fall through to ScriptProcessor if the worklet fails to load.
        }
      }

      // ── ScriptProcessorNode fallback (deprecated but widely supported) ──
      const bufferSize = 4096;
      const processor = ctx.createScriptProcessor(bufferSize, 1, 1);
      processor.onaudioprocess = (ev: AudioProcessingEvent) => {
        const input = ev.inputBuffer.getChannelData(0);
        // Copy — the underlying buffer is reused by the engine.
        const chunk = downsample(new Float32Array(input), ctx.sampleRate, CAPTURE_RATE);
        sendAudioChunk(float32ToInt16(chunk));
      };
      source.connect(processor);
      // A muted sink keeps the processor pumping without echoing the mic.
      const sink = ctx.createGain();
      sink.gain.value = 0;
      processor.connect(sink);
      sink.connect(ctx.destination);
      scriptNodeRef.current = processor;
    },
    [sendAudioChunk],
  );

  const start = useCallback(async () => {
    if (typeof window === "undefined") return;
    // Prevent double-start.
    if (wsRef.current || status === "connecting" || status === "listening" || status === "speaking") {
      return;
    }

    setError(null);

    const workspaceId = getActiveWorkspaceId();
    if (!workspaceId) {
      setError("Select a workspace first.");
      setStatus("error");
      return;
    }

    const AudioCtx = getAudioContextCtor();
    if (!AudioCtx || typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      setError("Voice control isn't supported in this browser.");
      setStatus("error");
      return;
    }

    const mySession = ++sessionIdRef.current;
    setStatus("connecting");
    setTranscript([]);

    // 1) Microphone.
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch {
      setError("Microphone access was blocked. Allow the mic to use voice control.");
      setStatus("error");
      return;
    }
    if (sessionIdRef.current !== mySession) {
      for (const t of stream.getTracks()) t.stop();
      return;
    }
    streamRef.current = stream;

    // 2) AudioContext + capture graph.
    let ctx: AudioContext;
    try {
      ctx = new AudioCtx();
      audioCtxRef.current = ctx;
      if (ctx.state === "suspended") {
        await ctx.resume();
      }
      playHeadRef.current = ctx.currentTime;
      await attachCapture(ctx, stream);
    } catch {
      setError("Couldn't start the audio pipeline.");
      setStatus("error");
      teardown();
      return;
    }
    if (sessionIdRef.current !== mySession) {
      return; // stop() ran during setup; teardown already handled it
    }

    // 3) WebSocket. Cookie auth rides the handshake automatically.
    const wsBase = deriveWsBase(API_BASE_URL);
    const url = `${wsBase}${VOICE_STREAM_PATH}?workspace_id=${encodeURIComponent(workspaceId)}`;
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
    } catch {
      setError("Couldn't connect to the voice service.");
      setStatus("error");
      teardown();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      if (sessionIdRef.current !== mySession) return;
      try {
        ws.send(JSON.stringify({ type: "start" }));
      } catch {
        /* ignore */
      }
    };

    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data !== "string") return; // server → client is JSON text
      let parsed: unknown;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        return;
      }
      handleServerMessage(mySession, parsed);
    };

    ws.onerror = () => {
      // The close handler drives user-facing state; just ensure it closes.
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    };

    ws.onclose = (ev: CloseEvent) => {
      if (sessionIdRef.current !== mySession) return;
      wsRef.current = null;
      let friendly: string | null = null;
      switch (ev.code) {
        case CLOSE_UNAUTHORIZED:
          friendly = "Voice needs you to be signed in.";
          break;
        case CLOSE_FORBIDDEN:
          friendly = "No access to this workspace.";
          break;
        case CLOSE_VOICE_DISABLED:
          friendly = "Voice is disabled.";
          break;
        default:
          friendly = null;
      }
      if (friendly) {
        setError(friendly);
        setStatus("error");
      } else {
        setStatus((s) => (s === "error" ? s : "idle"));
      }
      teardown();
    };
  }, [status, attachCapture, handleServerMessage, teardown]);

  const stop = useCallback(() => {
    teardown();
    setStatus("idle");
  }, [teardown]);

  // Clean everything up on unmount.
  useEffect(() => {
    return () => {
      teardown();
    };
  }, [teardown]);

  const active = status === "connecting" || status === "listening" || status === "speaking";

  return { status, transcript, error, active, start, stop };
}
