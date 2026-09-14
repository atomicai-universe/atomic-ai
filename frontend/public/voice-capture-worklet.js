/**
 * voice-capture-worklet.js — classic AudioWorklet module for the Atomic AI
 * voice pipeline (Phase 2).
 *
 * The mic hardware runs at the AudioContext's native rate (usually 44.1 or
 * 48 kHz). The backend voice WebSocket (Nova Sonic contract) expects raw
 * 16 kHz mono PCM16 (little-endian Int16) audio. This processor:
 *   1. Receives 128-sample mono Float32 render quanta from the mic.
 *   2. Accumulates them into a buffer.
 *   3. Downsamples the buffer from the input sample rate to 16 kHz using a
 *      simple averaging/linear resampler (adequate for speech).
 *   4. Converts the downsampled Float32 [-1,1] to Int16 little-endian.
 *   5. Posts the Int16 PCM as a transferable ArrayBuffer to the main thread,
 *      which forwards it as a BINARY WebSocket frame.
 *
 * It emits chunks of roughly OUTPUT_CHUNK samples (~50 ms at 16 kHz) so frames
 * stay small. `sampleRate` is a global available inside AudioWorkletGlobalScope.
 */

const TARGET_RATE = 16000;
// ~50 ms of 16 kHz audio per posted chunk (800 samples).
const OUTPUT_CHUNK = 800;

class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // Ratio of input samples per one output (16 kHz) sample.
    this._ratio = sampleRate / TARGET_RATE;
    // Float32 accumulator of already-downsampled 16 kHz samples.
    this._out = new Float32Array(OUTPUT_CHUNK);
    this._outLen = 0;
    // Fractional read position into the incoming stream (for resampling).
    this._pos = 0;
    // Leftover input tail carried across process() calls so resampling is
    // continuous across render quanta.
    this._tail = new Float32Array(0);
    this._running = true;

    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "stop") {
        this._running = false;
      }
    };
  }

  /** Convert accumulated 16 kHz Float32 samples to Int16LE and post them. */
  _flush() {
    if (this._outLen === 0) return;
    const int16 = new Int16Array(this._outLen);
    for (let i = 0; i < this._outLen; i++) {
      let s = this._out[i];
      if (s > 1) s = 1;
      else if (s < -1) s = -1;
      // Symmetric scaling to the Int16 range.
      int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    // Transfer the underlying buffer to avoid a copy.
    this.port.postMessage(int16.buffer, [int16.buffer]);
    this._outLen = 0;
  }

  _pushSample(sample) {
    this._out[this._outLen++] = sample;
    if (this._outLen >= OUTPUT_CHUNK) {
      this._flush();
    }
  }

  process(inputs) {
    if (!this._running) {
      this._flush();
      return false; // tear down the processor
    }

    const input = inputs[0];
    if (!input || input.length === 0) {
      return true; // no input connected yet; keep alive
    }
    const channel = input[0];
    if (!channel || channel.length === 0) {
      return true;
    }

    // Concatenate any carried-over tail with the new render quantum so the
    // resampler reads a continuous stream.
    let buf;
    if (this._tail.length > 0) {
      buf = new Float32Array(this._tail.length + channel.length);
      buf.set(this._tail, 0);
      buf.set(channel, this._tail.length);
    } else {
      buf = channel;
    }

    const ratio = this._ratio;
    let pos = this._pos;
    // Linear interpolation resample down to 16 kHz.
    while (pos + 1 < buf.length) {
      const idx = Math.floor(pos);
      const frac = pos - idx;
      const sample = buf[idx] * (1 - frac) + buf[idx + 1] * frac;
      this._pushSample(sample);
      pos += ratio;
    }

    // Preserve the unconsumed tail (samples beyond the last read position) so
    // the next call continues seamlessly.
    const consumed = Math.floor(pos);
    if (consumed < buf.length) {
      this._tail = buf.slice(consumed);
    } else {
      this._tail = new Float32Array(0);
    }
    this._pos = pos - consumed;

    return true;
  }
}

registerProcessor("voice-capture-processor", VoiceCaptureProcessor);
