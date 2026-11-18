"""A look source over TCP: it serves what a test stores and records each request."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from video_service.looks import LookSource

if TYPE_CHECKING:
    from collections.abc import Iterator

POLL_SECONDS = 0.01


class LookServer:
    """The files a look source serves, by path, and the paths it was asked for."""

    def __init__(self) -> None:
        """Serve nothing yet; `base_url` ends with a slash once the server listens."""
        self.base_url = ""
        self.files: dict[str, bytes] = {}
        self.redirects: dict[str, str] = {}
        self.requested: list[str] = []

    def url(self, name: str) -> str:
        """The URL of the file stored as `name`."""
        return f"{self.base_url}{name}"

    def host(self, name: str, data: bytes, *, sha256: str | None = None) -> LookSource:
        """Serve `data` as `name`; the look names the true digest unless given one."""
        self.files[name] = data
        return LookSource(
            url=self.url(name), sha256=sha256 or hashlib.sha256(data).hexdigest()
        )


def _handler(source: LookServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            del args

        def do_GET(self) -> None:
            name = self.path.removeprefix("/looks/")
            source.requested.append(name)
            if name in source.redirects:
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", source.redirects[name])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = source.files.get(name)
            self.send_response(HTTPStatus.NOT_FOUND if body is None else HTTPStatus.OK)
            body = body or b""
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@contextmanager
def look_server() -> Iterator[LookServer]:
    """Serve look files under `/looks/` on a free loopback port."""
    source = LookServer()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(source))
    source.base_url = f"http://127.0.0.1:{server.server_address[1]}/looks/"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": POLL_SECONDS}
    )
    thread.start()
    try:
        yield source
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
