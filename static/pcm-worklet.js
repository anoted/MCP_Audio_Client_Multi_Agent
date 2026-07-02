// Captures mic audio, resamples to 16 kHz mono, and posts Int16 chunks
// (512 samples = 32 ms) to the main thread for VAD + WebSocket streaming.

const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 512;

class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.residual = new Float32Array(0);
    this.out = new Int16Array(CHUNK_SAMPLES);
    this.outLen = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;

    // Concatenate residual + new block, then linear-interpolate down to 16 kHz.
    const joined = new Float32Array(this.residual.length + input.length);
    joined.set(this.residual);
    joined.set(input, this.residual.length);

    const usable = joined.length - 1; // need i+1 for interpolation
    const count = Math.max(0, Math.floor(usable / this.ratio));
    for (let i = 0; i < count; i++) {
      const pos = i * this.ratio;
      const i0 = Math.floor(pos);
      const frac = pos - i0;
      const sample = joined[i0] * (1 - frac) + joined[i0 + 1] * frac;
      const clamped = Math.max(-1, Math.min(1, sample));
      this.out[this.outLen++] = (clamped * 32767) | 0;
      if (this.outLen === CHUNK_SAMPLES) {
        this.port.postMessage(this.out.slice(0));
        this.outLen = 0;
      }
    }
    const consumed = Math.floor(count * this.ratio);
    this.residual = joined.slice(consumed);
    return true;
  }
}

registerProcessor('pcm-recorder', PCMRecorder);
