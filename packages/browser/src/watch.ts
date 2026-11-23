/** What the viewer shows: waiting for a picture, the first picture painted, or over for good. */
export type WatchState = "loading" | "drawn" | "ended";

export interface Watching {
  /** Ends the viewer and deletes it at the service, once the route has answered. */
  stop(): void;
}

const SDP_TYPE = "application/sdp";
const CREATED = 201;
const OVER = new Set(["failed", "closed"]);

function appendVideo(host: HTMLElement, stream: MediaStream): HTMLVideoElement {
  const video = document.createElement("video");
  video.playsInline = true;
  video.srcObject = stream;
  host.append(video);
  return video;
}

// A closed DTLS transport is the service's close of the viewer; the connection's own
// state follows it only after the ICE consent has lapsed.
function onOver(connection: RTCPeerConnection, over: () => void): void {
  const transport = connection.getReceivers()[0]!.transport!;
  const check = (): void => {
    if (OVER.has(connection.connectionState) || OVER.has(transport.state)) over();
  };
  connection.onconnectionstatechange = check;
  transport.onstatechange = check;
}

/** A peer that only receives audio and video, whose tracks go to `stream`; `created` sees it before it is set up. */
function receiveOnlyPeer(
  iceServers: RTCIceServer[],
  stream: MediaStream,
  created: (connection: RTCPeerConnection) => void,
): RTCPeerConnection {
  const connection = new RTCPeerConnection({ iceServers });
  created(connection);
  connection.addTransceiver("audio", { direction: "recvonly" });
  connection.addTransceiver("video", { direction: "recvonly" });
  connection.ontrack = ({ track }) => stream.addTrack(track);
  return connection;
}

/** Gathers the peer's candidates, POSTs its offer to `path` and returns the viewer's location and the answer. */
async function exchangeOffer(
  connection: RTCPeerConnection,
  path: string,
): Promise<{ location: string; response: Response }> {
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
  const location = response.headers.get("Location");
  if (response.status !== CREATED || location === null) throw new Error(`the route answered ${response.status}`);
  return { location, response };
}

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
  const video = appendVideo(host, stream);

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
  // delete follows the answer. A second end, a `stop()` from `show` included, waits for
  // the first one's delete instead of closing the peer ahead of it.
  let finished: Promise<void> | undefined;
  const finish = (): void => {
    end();
    finished ??= release().finally(() => peer?.close());
  };

  const begin = async (): Promise<void> => {
    const connection = receiveOnlyPeer(iceServers, stream, (created) => {
      peer = created;
    });
    const exchange = await exchangeOffer(connection, path);
    location = exchange.location;
    void release();
    if (state === "ended") return;
    await connection.setRemoteDescription({ type: "answer", sdp: await exchange.response.text() });
    onOver(connection, finish);
    video.requestVideoFrameCallback(() => report("drawn"));
    await video.play();
  };

  show("loading");
  begin().catch(finish);
  return {
    stop: finish,
  };
}
