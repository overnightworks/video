/** What the viewer shows: waiting for a picture, the first picture painted, or over for good. */
export type WatchState = "loading" | "drawn" | "ended";

export interface Watching {
  /** Ends the viewer and deletes it at the service, once the route has answered. */
  stop(): void;
}

const SDP_TYPE = "application/sdp";
const CREATED = 201;

/**
 * Watches a session through the app's own route: a receive-only peer, one POST of its
 * offer to `path`, a `<video>` in `host`. Every failure is `ended`, after which the
 * viewer stays ended; nothing here throws.
 */
export function watch(
  host: HTMLElement,
  path: string,
  iceServers: RTCIceServer[],
  show: (state: WatchState) => void,
): Watching {
  let state: WatchState = "loading";
  let location: string | undefined;
  let stopped = false;
  let peer: RTCPeerConnection | undefined;
  const video = document.createElement("video");
  video.playsInline = true;
  video.srcObject = new MediaStream();
  host.append(video);

  const report = (next: WatchState): void => {
    if (state === "ended" || state === next) return;
    state = next;
    show(next);
  };
  const end = (): void => {
    report("ended");
    peer?.close();
    video.remove();
  };
  // The service keeps a viewer until it is deleted, so a stop deletes what the route made.
  const release = (): void => {
    if (!stopped || location === undefined) return;
    void fetch(location, { method: "DELETE" }).catch(() => undefined);
    location = undefined;
  };

  const begin = async (): Promise<void> => {
    const connection = new RTCPeerConnection({ iceServers });
    peer = connection;
    connection.addTransceiver("audio", { direction: "recvonly" });
    connection.addTransceiver("video", { direction: "recvonly" });
    connection.ontrack = ({ track }) => (video.srcObject as MediaStream).addTrack(track);
    connection.onconnectionstatechange = () => {
      if (connection.connectionState === "failed" || connection.connectionState === "closed") end();
    };
    const gathered = new Promise<void>((resolve) => {
      connection.onicecandidate = ({ candidate }) => {
        if (candidate === null) resolve();
      };
    });
    await connection.setLocalDescription(await connection.createOffer());
    await gathered;
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": SDP_TYPE },
      body: connection.localDescription!.sdp,
    });
    const created = response.headers.get("Location");
    if (response.status !== CREATED || created === null) throw new Error(`the route answered ${response.status}`);
    location = created;
    release();
    if (state === "ended") return;
    await connection.setRemoteDescription({ type: "answer", sdp: await response.text() });
    video.requestVideoFrameCallback(() => report("drawn"));
    await video.play();
  };

  show("loading");
  begin().catch(end);
  return {
    stop() {
      stopped = true;
      end();
      release();
    },
  };
}
