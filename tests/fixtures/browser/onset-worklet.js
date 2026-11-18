// Passes its input through and posts the context time of the first sample of every sound
// that follows a quiet stretch.
const LOUD = 0.1;
const QUIET_SECONDS = 0.2;

class Onset extends AudioWorkletProcessor {
  lastLoudFrame = -Infinity;

  process(inputs, outputs) {
    inputs[0].forEach((samples, index) => outputs[0][index]?.set(samples));
    const channel = inputs[0][0];
    if (channel === undefined) return true;
    for (let index = 0; index < channel.length; index += 1) {
      if (Math.abs(channel[index]) <= LOUD) continue;
      const frame = currentFrame + index;
      if (frame - this.lastLoudFrame > QUIET_SECONDS * sampleRate) {
        this.port.postMessage(frame / sampleRate);
      }
      this.lastLoudFrame = frame;
    }
    return true;
  }
}

registerProcessor("onset", Onset);
