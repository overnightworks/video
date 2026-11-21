"""A local page that shows one session live and speaks audio files into it.

The command starts the service from the checkout under `probe-run`, with the provider
and the card settings of a live test, and opens one session on the look `alpha`, which
`--look-image` sets. It serves, on 127.0.0.1 only, a page that shows the session through
the browser package's `watch`, and a route that relays the viewers to the service with
the session's tokens: the browser holds none. The page's buttons speak the chosen
`--audio` file as one sentence, cut the sentence off and end the command. Without
`--dry-run` the card is guarded as the card proof guards it. The command ends the
service when the end button is pressed, when no viewer has been attached for a minute,
at SIGINT or SIGTERM and at the cap; it writes nothing but the service's log.
"""

# ruff: noqa: T201 - the command reports its address and its end on the command line

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import signal
import socket
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple, Protocol

import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from websockets.exceptions import ConnectionClosed

from card_drive import (
    ENVIRONMENT_PREFIX,
    EVENT_WITHIN_SECONDS,
    FAKE_PROVIDER,
    KERNEL,
    STOP_SIGNALS,
    DriveError,
    ProcessControl,
    opened_session,
    read_audio,
    running_service,
)
from card_identity import LOOK_NAMES, throwaway_identity
from card_memory import GroupWatch
from card_record import FREE_MEMORY_LINE
from card_supervision import (
    CHILD_MARKER,
    Stops,
    Supervision,
    become_subreaper,
    begun_child,
    ended_below,
    service_pid_file,
    started_under_probe_run,
)
from prove_card import card_provider, checked_look_image, checked_record
from video_client.session_wire import ErrorEvent, parse_server_event
from video_client.wire import (
    LOCATION_HEADER,
    MAX_OFFER_BYTES,
    SDP_CONTENT_TYPE,
    viewer_path,
    viewers_path,
)
from video_service.card import read_card_memory

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

    from card_drive import Audio, Service, Session
    from card_identity import Identity

REPOSITORY: Final = Path(__file__).resolve().parents[1]
PAGE_FILES: Final = Path(__file__).resolve().parent / "watch_live_page"
PACKAGE_BUILD: Final = REPOSITORY / "packages/browser/dist"
VIEWERS_PATH: Final = "/viewers"
HOST: Final = "127.0.0.1"
# What a live test gives the service: the provider's cap on the card and the lag it
# tolerates.
CARD_MEMORY_MB: Final = 7168
MAX_LAG_MS: Final = 2400
IDLE_SECONDS: Final = 60.0
IDLE_POLL_SECONDS: Final = 1.0
# How long the page server waits for a request that is still open when the command ends.
PAGE_CLOSE_SECONDS: Final = 2
# `timeout`'s status for a command it ended at the cap.
CAP_STATUS: Final = 124
GROUP_NAME: Final = "watch"


class Over(Protocol):
    """What ends the command: the reason, and whether the session failed by itself."""

    def __call__(self, reason: str, *, failed: bool = False) -> None:
        """End the command, unless something has ended it already; from any thread."""


class Ended(NamedTuple):
    """Why the page was served no longer, and whether that is a failure."""

    reason: str
    failed: bool


class LivePage:
    """The page's routes: the viewer relay, which holds the tokens, and the buttons."""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        identity: Identity,
        session: Session,
        audio: Mapping[str, Audio],
        over: Over,
    ) -> None:
        """Serve `session`, whose service `client` reaches, and speak `audio`."""
        self.admitted = 0
        self._client = client
        self._identity = identity
        self._session = session
        self._audio = audio
        self._over = over
        self._sending = asyncio.Lock()
        self._sentence_numbers = itertools.count(1)

    def routes(self, package: Path) -> list[Route | Mount]:
        """The relay, the buttons, the built `package` and the page."""
        return [
            Route(VIEWERS_PATH, self._offer, methods=["POST"]),
            Route(f"{VIEWERS_PATH}/{{viewer}}", self._delete, methods=["DELETE"]),
            Route("/files", self._files),
            Route("/speak/{name}", self._speak, methods=["POST"]),
            Route("/clear", self._clear, methods=["POST"]),
            Route("/end", self._end, methods=["POST"]),
            Mount("/dist", StaticFiles(directory=package)),
            Mount("/", StaticFiles(directory=PAGE_FILES, html=True)),
        ]

    async def _offer(self, request: Request) -> Response:
        """Relay the browser's offer with the session's tokens (WIRE-14)."""
        offer = b""
        async for chunk in request.stream():
            offer += chunk
            if len(offer) > MAX_OFFER_BYTES:
                return Response(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        relayed = await self._client.post(
            viewers_path(self._session.created.session.id),
            content=offer,
            headers=self._identity.headers() | {"Content-Type": SDP_CONTENT_TYPE},
        )
        if relayed.status_code != HTTPStatus.CREATED:
            return Response(
                relayed.content,
                status_code=relayed.status_code,
                media_type=relayed.headers.get("content-type"),
            )
        self.admitted += 1
        viewer = relayed.headers[LOCATION_HEADER].rpartition("/")[2]
        return Response(
            relayed.content,
            status_code=HTTPStatus.CREATED,
            media_type=SDP_CONTENT_TYPE,
            headers={LOCATION_HEADER: f"{VIEWERS_PATH}/{viewer}"},
        )

    async def _delete(self, request: Request) -> Response:
        relayed = await self._client.delete(
            viewer_path(
                self._session.created.session.id, request.path_params["viewer"]
            ),
            headers=self._identity.headers(),
        )
        return Response(status_code=relayed.status_code)

    def _files(self, _request: Request) -> Response:
        return JSONResponse(list(self._audio))

    async def _speak(self, request: Request) -> Response:
        name = request.path_params["name"]
        if name not in self._audio:
            return Response(status_code=HTTPStatus.NOT_FOUND)
        # The wire refuses an id that is still queued, so each press has its own.
        sentence = f"sentence-{next(self._sentence_numbers)}"
        return await self._sent(
            lambda: self._session.speak(sentence, self._audio[name])
        )

    async def _clear(self, _request: Request) -> Response:
        return await self._sent(self._session.clear)

    def _end(self, _request: Request) -> Response:
        self._over("the end button")
        return Response(status_code=HTTPStatus.NO_CONTENT)

    async def _sent(self, send: Callable[[], Awaitable[object]]) -> Response:
        """Send to the session, one event or sentence at a time; 503 when it is over."""
        try:
            async with self._sending:
                await send()
        except DriveError, ConnectionClosed:
            return Response(status_code=HTTPStatus.SERVICE_UNAVAILABLE)
        return Response(status_code=HTTPStatus.NO_CONTENT)


@dataclass
class Idle:
    """Whether no viewer has been attached for `seconds`, counted from `since`."""

    seconds: float
    since: float

    def expired(self, now: float, viewers: int) -> bool:
        """Count the viewers seen at `now`; whether the idle time has run out."""
        if viewers > 0:
            self.since = now
        return now - self.since >= self.seconds


def viewers_attached(service: Service, page: LivePage) -> int:
    """The viewers attached: the offers the relay admitted less the admitted ones ended.

    Only the line of a viewer that was admitted and has ended names who ended it; the
    line of an offer that was refused or failed does not.
    """
    ended = sum("ended_by" in line for line in service.lines("viewer.ended"))
    return page.admitted - ended


async def _end_when_unwatched(
    service: Service, page: LivePage, seconds: float, over: Over
) -> None:
    idle = Idle(seconds, time.monotonic())
    while True:
        await asyncio.sleep(IDLE_POLL_SECONDS)
        if idle.expired(time.monotonic(), viewers_attached(service, page)):
            break
    over(f"no viewer for {seconds:g} seconds")


async def _end_with_session(session: Session, over: Over) -> None:
    """Read the session's events until it ends, which ends the command as a failure."""
    with suppress(ConnectionClosed):
        async for message in session.socket:
            event = parse_server_event(message)
            if isinstance(event, ErrorEvent):
                over(f"the session ended with {event.error.code}", failed=True)
                return
    over("the session's connection closed", failed=True)


class _SameOrigin:
    """Refuses a request from another origin than the page's own.

    No other site's page may press the buttons or attach a viewer: the relay holds the
    session's tokens and would let it through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            own_origin = f"{scope['scheme']}://{headers.get('host')}"
            if origin is not None and origin != own_origin:
                await Response(status_code=HTTPStatus.FORBIDDEN)(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _application(page: LivePage, package: Path) -> Starlette:
    """The page's routes, answered only for the loopback host and the page's origin."""
    return Starlette(
        routes=page.routes(package),
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=[HOST]),
            Middleware(_SameOrigin),
        ],
    )


class _Server(uvicorn.Server):
    """A server that leaves the command's own stop signals in place."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _show(
    service: Service,
    identity: Identity,
    audio: Mapping[str, Audio],
    arguments: argparse.Namespace,
) -> Ended:
    """Serve the page until something ends it; what ended it."""
    loop = asyncio.get_running_loop()
    ended: asyncio.Future[Ended] = loop.create_future()

    def settle(result: Ended) -> None:
        if not ended.done():
            ended.set_result(result)

    def over(reason: str, *, failed: bool = False) -> None:
        # Starlette runs a plain route handler in a worker thread.
        loop.call_soon_threadsafe(settle, Ended(reason, failed))

    async with (
        opened_session(service, identity, LOOK_NAMES[0]) as session,
        httpx2.AsyncClient(
            base_url=service.base_url, timeout=EVENT_WITHIN_SECONDS
        ) as client,
    ):
        page = LivePage(client, identity, session, audio, over)
        server = _Server(
            uvicorn.Config(
                _application(page, arguments.package),
                log_level="warning",
                timeout_graceful_shutdown=PAGE_CLOSE_SECONDS,
            )
        )
        # Bound and listening first: a connection never waits for the server to start.
        with socket.create_server((HOST, 0)) as bound:
            serving = asyncio.create_task(server.serve([bound]))
            watchers = [
                asyncio.create_task(_end_with_session(session, over)),
                asyncio.create_task(
                    _end_when_unwatched(service, page, arguments.idle_seconds, over)
                ),
            ]
            print(f"watch at http://{HOST}:{bound.getsockname()[1]}/", flush=True)
            result = await ended
            for watcher in watchers:
                watcher.cancel()
            server.should_exit = True
            await serving
    return result


def _card_environment() -> Mapping[str, str]:
    """The `VIDEO_` variables of the card provider, with the cap of a live test."""
    cap = f"{ENVIRONMENT_PREFIX}CARD_MEMORY_MB"
    return card_provider(os.environ | {cap: str(CARD_MEMORY_MB)}).environment


def _provider_of(arguments: argparse.Namespace) -> Mapping[str, str]:
    return FAKE_PROVIDER if arguments.dry_run else _card_environment()


def _audio_by_name(paths: Sequence[Path]) -> dict[str, Audio]:
    """Each file's audio, by its name, once the proof's check accepts it."""
    names = [path.name for path in paths]
    if len(set(names)) != len(names):
        message = "--audio names a file name twice"
        raise SystemExit(message)
    return {path.name: read_audio(path) for path in paths}


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True, help="the service's log")
    parser.add_argument(
        "--audio",
        type=Path,
        nargs="+",
        required=True,
        help="mono 16-bit WAV files, one sentence each; the page chooses by name",
    )
    parser.add_argument(
        "--look-image",
        type=Path,
        help="the face image the service takes, served as the look alpha",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="drive the fake provider"
    )
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    parser.add_argument("--package", type=Path, default=PACKAGE_BUILD)
    parser.add_argument("--fragment", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _checked(arguments: argparse.Namespace) -> tuple[dict[str, Audio], bytes | None]:
    """Check the arguments before any service starts; the audio and the image."""
    arguments.log = checked_record(arguments.log, "--log")
    audio = _audio_by_name(arguments.audio)
    if not (arguments.package / "index.js").is_file():
        message = (
            "the browser package is not built: run `pnpm run build` in packages/browser"
        )
        raise SystemExit(message)
    if arguments.look_image is None:
        return audio, None
    arguments.look_image = arguments.look_image.resolve()
    return audio, checked_look_image(arguments.look_image)


def _child(arguments: argparse.Namespace) -> int:
    """Run the service and the page where `probe-run` started them."""
    identity_directory = begun_child(arguments.fragment)
    audio, look_image = _checked(arguments)
    with (
        throwaway_identity(
            identity_directory, look_image, looks=LOOK_NAMES[:1]
        ) as identity,
        running_service(
            identity,
            arguments.log,
            max_lag_ms=MAX_LAG_MS,
            compiler_home=arguments.fragment.parent / "compiler-home",
            service_pid_file=service_pid_file(arguments.fragment),
            provider=_provider_of(arguments),
        ) as service,
    ):
        ended = asyncio.run(_show(service, identity, audio, arguments))
    print(f"ended: {ended.reason}")
    return 1 if ended.failed else 0


def _under_probe_run(
    arguments: argparse.Namespace,
    fragment: Path,
    supervision: Supervision,
) -> int | None:
    """Run the child under `probe-run`; its status, none when it did not start."""
    tail = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--log",
        str(arguments.log),
        "--fragment",
        str(fragment),
        "--idle-seconds",
        str(arguments.idle_seconds),
        "--package",
        str(arguments.package),
        "--audio",
        *map(str, arguments.audio),
        *(["--look-image", str(arguments.look_image)] if arguments.look_image else []),
        *(["--dry-run"] if arguments.dry_run else []),
    ]
    return started_under_probe_run(tail, GROUP_NAME, fragment, supervision).status


def main(argv: Sequence[str] | None = None, control: ProcessControl = KERNEL) -> int:
    """Serve the page until it ends; non-zero when the card or the service failed."""
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    if os.environ.get(CHILD_MARKER) == GROUP_NAME:
        return _child(arguments)
    _checked(arguments)
    if not arguments.dry_run:
        _card_environment()
    become_subreaper(control)
    stops = Stops()
    for stopping in STOP_SIGNALS:
        signal.signal(stopping, stops.record)
    watch = (
        None
        if arguments.dry_run
        else GroupWatch(read_card_memory, CARD_MEMORY_MB, int(FREE_MEMORY_LINE.limit))
    )
    with tempfile.TemporaryDirectory(prefix="watch-live-") as scratch:
        (Path(scratch) / "compiler-home").mkdir()
        # Only its names are used: the child's pid file and keys lie beside it.
        fragment = Path(scratch) / "watch.json"
        status = _under_probe_run(
            arguments, fragment, Supervision(stops, control, watch)
        )
        kill = ended_below(control, watch)
    return _outcome(status, kill_failed=kill.failed, stops=stops, watch=watch)


def _outcome(
    status: int | None, *, kill_failed: bool, stops: Stops, watch: GroupWatch | None
) -> int:
    """The command's status once the child is gone and what was left is killed."""
    if kill_failed:
        message = "a process below the command was not ended"
        raise SystemExit(message)
    if watch is not None and watch.stop is not None:
        return 1
    if (interrupt := stops.to_act_on) is not None:
        print(f"stopped by {signal.Signals(interrupt).name}")
        return 0
    if status == CAP_STATUS:
        print("ended at the cap")
        return 0
    return 0 if status == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
