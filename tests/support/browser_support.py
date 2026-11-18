"""A stand-in app and a Chromium that watches through it.

The app serves a page, the built browser package and one route that relays a viewer's
offer to the service with the session's tokens, by its own HTTP call, as an app's
route does. Chromium never holds a token. With `VIDEO_BROWSER_COVERAGE_DIR` set,
the page's V8 coverage is written there for the CI report.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import uvicorn
from playwright.async_api import async_playwright
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from support.identity_support import valid_headers
from video_client.wire import LOCATION_HEADER, SDP_CONTENT_TYPE, viewer_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import httpx2
    from playwright.async_api import CDPSession, Page
    from starlette.requests import Request

    from support.viewer_support import OpenSession, Service

REPOSITORY: Final = Path(__file__).resolve().parents[2]
PACKAGE_BUILD: Final = REPOSITORY / "packages/browser/dist"
PAGE_FILES: Final = REPOSITORY / "tests/fixtures/browser"
COVERAGE_DIRECTORY_VARIABLE: Final = "VIDEO_BROWSER_COVERAGE_DIR"
VIEWERS_PATH: Final = "/viewers"
WITHIN_MILLISECONDS: Final = 15_000
KILL_SIGNAL: Final = signal.SIGKILL

ALLOWS_AUTOPLAY: Final = "--autoplay-policy=no-user-gesture-required"
REFUSES_AUTOPLAY: Final = "--autoplay-policy=document-user-activation-required"


@dataclass
class StandInApp:
    """The route an app gives its page, and what a scenario makes it do."""

    service: Service
    session: OpenSession
    refusal: int | None = None
    alter: Callable[[httpx2.Response], Response] | None = None
    answer_gate: asyncio.Event = field(default_factory=asyncio.Event)
    posted: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        """Answer at once until a scenario closes the gate."""
        self.answer_gate.set()

    def routes(self) -> list[Route | Mount]:
        """The page, the built package, and the viewer route."""
        return [
            Route(VIEWERS_PATH, self._post, methods=["POST"]),
            Route(f"{VIEWERS_PATH}/{{viewer}}", self._delete, methods=["DELETE"]),
            Mount("/dist", StaticFiles(directory=PACKAGE_BUILD)),
            Mount("/", StaticFiles(directory=PAGE_FILES, html=True)),
        ]

    async def _post(self, request: Request) -> Response:
        if self.refusal is not None:
            return Response(status_code=self.refusal)
        relayed = await self.service.post(self.session, await request.body())
        self.posted.set()
        await self.answer_gate.wait()
        if self.alter is not None:
            return self.alter(relayed)
        return Response(
            relayed.content,
            status_code=relayed.status_code,
            media_type=SDP_CONTENT_TYPE,
            headers={LOCATION_HEADER: self._location(relayed)},
        )

    async def _delete(self, request: Request) -> Response:
        relayed = await self.service.http.delete(
            viewer_path(self.session.session_id, request.path_params["viewer"]),
            headers=valid_headers(),
        )
        return Response(status_code=relayed.status_code)

    @staticmethod
    def _location(relayed: httpx2.Response) -> str:
        return f"{VIEWERS_PATH}/{relayed.headers[LOCATION_HEADER].rpartition('/')[2]}"


class PageCoverage:
    """Chromium's precise V8 coverage of the page, written for the CI report."""

    def __init__(self, directory: Path | None) -> None:
        """Write into `directory`; collect nothing when it is None."""
        self._directory = directory
        self._session: CDPSession | None = None

    async def start(self, page: Page) -> None:
        """Start the profiler on `page`."""
        if self._directory is None:
            return
        self._session = await page.context.new_cdp_session(page)
        await self._session.send("Profiler.enable")
        await self._session.send(
            "Profiler.startPreciseCoverage", {"callCount": True, "detailed": True}
        )

    async def write(self) -> None:
        """Write what the page covered; call it before the page closes."""
        if self._session is None or self._directory is None:
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        taken = await self._session.send("Profiler.takePreciseCoverage")
        (self._directory / f"{uuid4().hex}.json").write_text(json.dumps(taken))


@dataclass(frozen=True)
class Seen:
    """What the page recorded: the states `show` was given and what it played."""

    states: list[str]
    thrown: list[str]
    presented: int
    flashes: list[float]
    heard: list[float]


@dataclass
class Stage:
    """A Chromium page on the stand-in app, and the browser around it."""

    page: Page
    app: StandInApp
    profile: Path
    address: str
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    async def begin(self, path: str = VIEWERS_PATH) -> None:
        """Have the page watch through `path`, with no ICE server."""
        await self.page.evaluate("path => stage.begin(path)", path)

    async def begin_on_load(self, path: str = VIEWERS_PATH) -> None:
        """Load the page so that it watches through `path` with no user activation."""
        await self.page.goto(f"{self.address}?watch={path}")

    async def read(self) -> Seen:
        """What the page recorded: states, errors, frames, flashes, beeps."""
        return Seen(**await self.page.evaluate("stage.read()"))

    async def until(
        self, expression: str, within_milliseconds: int = WITHIN_MILLISECONDS
    ) -> None:
        """Wait until `expression`, evaluated over `stage.read()` as `seen`, holds."""
        await self.page.wait_for_function(
            f"(seen => {expression})(stage.read())", timeout=within_milliseconds
        )

    async def states(self) -> list[str]:
        """The states `show` was called with, in order."""
        return (await self.read()).states

    def kill(self) -> None:
        """Kill Chromium with SIGKILL, so it can send no DTLS close."""
        for pid in _tree_of(self.profile):
            with suppress(ProcessLookupError):
                os.kill(pid, KILL_SIGNAL)


def _processes() -> dict[int, tuple[int, str]]:
    """Each process id with its parent's and its command line."""
    found = {}
    for entry in Path("/proc").glob("[0-9]*"):
        with suppress(OSError, ValueError):
            fields = (entry / "stat").read_text().rpartition(") ")[2].split()
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            found[int(entry.name)] = (int(fields[1]), command)
    return found


def _tree_of(profile: Path) -> list[int]:
    """The browser process that runs on `profile` and every process below it."""
    processes = _processes()
    found = [
        pid
        for pid, (_, command) in processes.items()
        if f"--user-data-dir={profile}" in command and "--type=" not in command
    ]
    for pid in found:
        found.extend(child for child, (parent, _) in processes.items() if parent == pid)
    return found


@asynccontextmanager
async def serving(app: StandInApp) -> AsyncIterator[str]:
    """Serve the stand-in app on a free port; yield its address."""
    server = uvicorn.Server(
        uvicorn.Config(Starlette(routes=app.routes()), log_level="warning")
    )
    # Bound and listening first: a connection never waits for the server to start.
    with socket.create_server(("127.0.0.1", 0)) as bound:
        serve = asyncio.create_task(server.serve([bound]))
        try:
            yield f"http://127.0.0.1:{bound.getsockname()[1]}/"
        finally:
            server.should_exit = True
            await serve


@asynccontextmanager
async def stage(
    service: Service,
    session: OpenSession,
    profile: Path,
    *,
    autoplay_flag: str = ALLOWS_AUTOPLAY,
) -> AsyncIterator[Stage]:
    """A page of Chromium on the stand-in app of `session`."""
    app = StandInApp(service, session)
    coverage_directory = os.environ.get(COVERAGE_DIRECTORY_VARIABLE)
    coverage = PageCoverage(Path(coverage_directory) if coverage_directory else None)
    async with serving(app) as address, async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            profile, args=[autoplay_flag]
        )
        try:
            page = await context.new_page()
            await coverage.start(page)
            watched = Stage(page, app, profile, address)
            page.on("request", lambda r: watched.requests.append((r.url, r.headers)))
            await page.goto(address)
            yield watched
            await coverage.write()
        finally:
            with suppress(Exception):
                await asyncio.wait_for(context.close(), timeout=5)
