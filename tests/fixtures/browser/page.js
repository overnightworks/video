// The page of a stand-in app: it calls `watch` and records what the browser shows and plays.
import { watch } from "/dist/index.js";

const BRIGHT = 128;
const SAMPLED_PIXELS = 8;
const POLL_MILLISECONDS = 20;

const host = document.getElementById("host");
const states = [];
const thrown = [];
const flashes = [];
const heard = [];
let presented = 0;
let watching;

window.addEventListener("error", (event) => thrown.push(String(event.message)));
window.addEventListener("unhandledrejection", (event) => thrown.push(String(event.reason)));

const until = async (condition) => {
  while (!condition()) await new Promise((resolve) => setTimeout(resolve, POLL_MILLISECONDS));
};

function recordFrames(video) {
  const context = new OffscreenCanvas(SAMPLED_PIXELS, SAMPLED_PIXELS).getContext("2d", { willReadFrequently: true });
  let wasBright = false;
  const onFrame = (_now, metadata) => {
    presented = metadata.presentedFrames;
    context.drawImage(video, 0, 0, SAMPLED_PIXELS, SAMPLED_PIXELS);
    const bright = context.getImageData(0, 0, 1, 1).data[0] > BRIGHT;
    if (bright && !wasBright) flashes.push(metadata.presentationTime);
    wasBright = bright;
    video.requestVideoFrameCallback(onFrame);
  };
  video.requestVideoFrameCallback(onFrame);
}

async function recordSound(video) {
  await until(() => video.srcObject.getAudioTracks().length > 0);
  const context = new AudioContext();
  await context.audioWorklet.addModule("/onset-worklet.js");
  const onset = new AudioWorkletNode(context, "onset");
  onset.port.onmessage = ({ data: contextTime }) => {
    const output = context.getOutputTimestamp();
    heard.push(output.performanceTime + (contextTime - output.contextTime) * 1000);
  };
  // Chromium gives a MediaElementAudioSourceNode only silence for a MediaStream source, so
  // the onset node taps the stream the element plays and passes it on to the speakers.
  context.createMediaStreamSource(video.srcObject).connect(onset).connect(context.destination);
}

window.stage = {
  begin(path, iceServers = []) {
    try {
      watching = watch(host, path, iceServers, (state) => states.push(state));
    } catch (error) {
      thrown.push(String(error));
    }
  },
  stop: () => watching.stop(),
  async measure() {
    const video = host.querySelector("video");
    recordFrames(video);
    await recordSound(video);
  },
  read: () => ({ states, thrown, presented, flashes, heard }),
};

// A page that begins on load has had no user activation, as an app's page has not.
const requested = new URLSearchParams(location.search).get("watch");
if (requested !== null) window.stage.begin(requested);
