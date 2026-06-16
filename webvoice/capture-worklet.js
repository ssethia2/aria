// Capture worklet: forwards each mic frame (Float32, at the context's native rate) to the
// main thread, which downsamples to 16 kHz and streams it to Gemini Live. No output is
// written, so when connected through a zero-gain node it pulls audio without feedback.
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));   // copy: the input buffer is reused
    return true;
  }
}
registerProcessor('capture', CaptureProcessor);
