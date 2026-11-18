/** What the viewer shows: waiting for a picture, the first picture painted, or over for good. */
export type WatchState = "loading" | "drawn" | "ended";

export interface Watching {
  /** Ends the viewer and deletes it at the service, once the route has answered. */
  stop(): void;
}

const SDP_TYPE = "application/sdp";
const CREATED = 201;
const OVER = new Set(["failed", "closed"]);

/**
 * Watches a session through the app's own route: a receive-only peer, one POST of its
 * offer to `path`, a `<video>` in `host`. Every failure is `ended` and deletes the viewer
 * the route made, as `stop` does; after it the viewer stays ended; nothing here throws.
 */
export function watch(
  host: HTMLElement,
  path: string,
  iceServers: RTCIceServer[],
  show: (state: WatchState) => void,
): Watching {
  let state: WatchState = "loading";
  let location: string | undefined;
  let peer: RTCPeerConnection | undefined;
  const stream = new MediaStream();
  const video = document.createElement("video");
  video.playsInline = true;
  video.srcObject = stream;
  host.append(video);

  const report = (next: WatchState): void => {
    if (state === "ended" || state === next) return;
    state = next;
    show(next);
  };
  const end = (): void => {
    report("ended");
    video.srcObject = null;
    video.remove();
  };
  // The service keeps a viewer until it is deleted, so every end deletes what the route made.
  const release = async (): Promise<void> => {
    if (state !== "ended" || location === undefined) return;
    const viewer = location;
    location = undefined;
    await fetch(viewer, { method: "DELETE" }).catch(() => undefined);
  };
  // Once the route has answered, the peer stays up until the delete has gone out, so the
  // service ends the viewer by it; before the answer, the peer closes at once and the
  // delete follows the answer.
  const finish = (): void => {
    end();
    void release().finally(() => peer?.close());
  };

  const begin = async (): Promise<void> => {
    const connection = new RTCPeerConnection({ iceServers });
    peer = connection;
    connection.addTransceiver("audio", { direction: "recvonly" });
    connection.addTransceiver("video", { direction: "recvonly" });
    connection.ontrack = ({ track }) => stream.addTrack(track);
    // A closed DTLS transport is the service's close of the viewer; the connection's own
    // state follows it only after the ICE consent has lapsed.
    const check = (): void => {
      const dtls = connection.getReceivers()[0]!.transport!.state;
      if (OVER.has(connection.connectionState) || OVER.has(dtls)) finish();
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
    void release();
    if (state === "ended") return;
    await connection.setRemoteDescription({ type: "answer", sdp: await response.text() });
    connection.onconnectionstatechange = check;
    connection.getReceivers()[0]!.transport!.onstatechange = check;
    video.requestVideoFrameCallback(() => report("drawn"));
    await video.play();
  };

  show("loading");
  begin().catch(finish);
  return {
    stop: finish,
  };
}
