"""The live page's command: it serves one session, speaks files and ends clean."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sys
import types
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import httpx2
import pytest
from websockets.exceptions import ConcurrencyError, ConnectionClosedOK

import watch_live
from card_drive import Session
from card_supervision import CAP_SECONDS, KILL_GRACE_SECONDS
from support.browser_support import PACKAGE_BUILD, opened
from support.kill_harness import top_command
from support.prove_card_support import (
    APPEARS_WITHIN_SECONDS,
    FLOOR_MB,
    GONE_WITHIN_SECONDS,
    MAX_QUEUED_AUDIO_SECONDS,
    SCRIPTS,
    USED_MB,
    Run,
    write_audio,
)
from video_client.session_wire import ErrorEvent, SessionCreated, SessionFailure
from video_client.wire import MAX_OFFER_BYTES, REFUSAL_STATUS, RefusalCode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from card_identity import Identity

ADDRESS = re.compile(r"watch at (http://\S+)")
IDLE_SECONDS = 2
# Shorter than the run's own audio, so the log tells which file was spoken.
SECOND_SECONDS = 1
SENTENCE_WITHIN_SECONDS = 30
ENDED_WITHIN_SECONDS = 30
CAP_TEST_SECONDS = 3
# Free memory that leaves less than the floor beside the cap a live test gives.
NO_ROOM_FOR_THE_CAP = f"{USED_MB}, {watch_live.CARD_MEMORY_MB + FLOOR_MB - 1}, 24576"


class Live:
    """The command started on a run's surroundings."""

    def __init__(
        self,
        run: Run,
        audio: list[Path],
        arguments: list[str],
        top: list[str] | None = None,
    ) -> None:
        """Start the command, or `top` that runs it, with the run's audio and more."""
        self.run = run
        self.log = run.directory / "service.log"
        self.command = run.launch(
            [
                *(top or [sys.executable, str(SCRIPTS / "watch_live.py")]),
                "--log",
                str(self.log),
                "--audio",
                str(run.audio),
                *map(str, audio),
                *arguments,
            ]
        )

    @property
    def address(self) -> str:
        """The page's address, once the command has printed it."""
        assert self.run.wait_until(
            lambda: ADDRESS.search(self.run.printed()) is not None,
            APPEARS_WITHIN_SECONDS,
        )
        found = ADDRESS.search(self.run.printed())
        assert found is not None
        return found.group(1)

    def post(self, path: str) -> int:
        """The status the page's route answers a POST to `path` with."""
        return httpx2.post(self.address + path.lstrip("/")).status_code

    def records(self, event: str) -> list[dict[str, object]]:
        """The service's log lines of `event`."""
        records = (
            json.loads(line)
            for line in self.log.read_text(errors="replace").splitlines()
            if line.startswith("{")
        )
        return [record for record in records if record["event"] == event]

    def sentences(self) -> list[int]:
        """The audio length in ms of each sentence the service has ended."""
        return [line["audio_ms"] for line in self.records("sentence.ended")]

    def ended(self) -> tuple[int, str]:
        """The command's status and standard error, once it has ended."""
        status, error = self.run.finish(self.command)
        assert self.run.wait_until(lambda: not self.run.alive(), GONE_WITHIN_SECONDS)
        return status, error


@pytest.fixture
def package(run: Run) -> Path:
    """A stand-in for the browser package's build, which the page needs to exist."""
    directory = run.directory / "package"
    directory.mkdir()
    (directory / "index.js").write_text("")
    return directory


@pytest.fixture
def live(run: Run, package: Path, harness: Path) -> Callable[..., Live]:
    """Start the command on the fake provider, or on the card's stand-in.

    A `mishap` runs it as the top of the kill harness case of that name.
    """

    def start(
        *arguments: str,
        audio: list[Path] | None = None,
        card: bool = False,
        mishap: str | None = None,
    ) -> Live:
        if card:
            run.in_card_mode()
        return Live(
            run,
            audio or [],
            ["--package", str(package), *([] if card else ["--dry-run"]), *arguments],
            None if mishap is None else top_command(harness, "watch_live", mishap, []),
        )

    return start


def test_the_end_button_ends_the_service_and_the_command(
    live: Callable[..., Live],
) -> None:
    command = live()
    assert command.address.startswith("http://127.0.0.1:")

    assert command.post("/end") == httpx2.codes.NO_CONTENT

    status, _ = command.ended()
    assert status == 0
    assert "ended: the end button" in command.run.printed()


def test_a_viewer_request_that_never_finishes_does_not_keep_the_command_running(
    live: Callable[..., Live],
) -> None:
    command = live()
    page = urlsplit(command.address)
    with socket.create_connection((page.hostname, page.port)) as unfinished:
        unfinished.sendall(
            f"POST /viewers HTTP/1.1\r\nHost: {page.netloc}\r\n"
            "Content-Length: 1000\r\n\r\nv=0".encode()
        )

        assert command.post("/end") == httpx2.codes.NO_CONTENT

        assert command.run.wait_until(
            lambda: command.command.poll() is not None, ENDED_WITHIN_SECONDS
        )
    assert command.command.returncode == 0


def test_the_service_is_given_the_portrait_as_its_one_look(
    live: Callable[..., Live],
) -> None:
    command = live()
    assert command.address

    assert [line["look"] for line in command.records("look_preparation.ended")] == [
        watch_live.LOOK_NAMES[0]
    ]

    command.post("/end")
    command.ended()


@pytest.mark.parametrize("nested", [False, True], ids=["exec", "timeout-as-child"])
@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM])
def test_a_stop_signal_ends_the_service_and_the_command(
    run: Run, live: Callable[..., Live], stop: signal.Signals, *, nested: bool
) -> None:
    if nested:
        run.nest_timeout_under_probe_run()
    command = live()
    assert command.address

    command.command.send_signal(stop)

    # Sooner than the page's idle time would end it.
    assert command.run.wait_until(
        lambda: command.command.poll() is not None, ENDED_WITHIN_SECONDS
    )
    status, _ = command.ended()
    assert status == 0
    assert f"stopped by {stop.name}" in command.run.printed()


def test_a_session_the_service_ends_ends_the_command_with_a_failure(
    live: Callable[..., Live], run: Run
) -> None:
    longer = run.directory / "longer.wav"
    write_audio(longer, seconds=MAX_QUEUED_AUDIO_SECONDS + 1)
    command = live(audio=[longer])
    command.post(f"/speak/{longer.name}")

    status, _ = command.ended()

    assert status == 1
    assert "ended: the session ended with too_large" in command.run.printed()


def test_a_process_below_the_command_that_is_not_ended_fails_the_command(
    live: Callable[..., Live],
) -> None:
    command = live(mishap="raise")
    assert command.address

    command.post("/end")

    # The process the harness leaves below the top stays: the run's cleanup ends it.
    status, error = command.run.finish(command.command)
    assert status == 1
    assert "a process below the command was not ended" in error


def test_a_run_that_reaches_the_cap_ends_at_the_cap(
    live: Callable[..., Live], run: Run
) -> None:
    run.cut_the_cap(CAP_TEST_SECONDS)
    command = live()

    status, _ = command.ended()

    assert status == 0
    assert "ended at the cap" in command.run.printed()
    assert run.processes() == []


def test_a_service_that_dies_ends_the_command_with_a_failure(
    live: Callable[..., Live],
) -> None:
    command = live()
    assert command.address

    os.kill(command.run.service_pid(), signal.SIGKILL)

    status, _ = command.ended()
    assert status != 0
    assert "ended: the session's connection closed" in command.run.printed()


@pytest.mark.parametrize(
    ("header", "value", "refused"),
    [
        ("Origin", "http://other.example", httpx2.codes.FORBIDDEN),
        ("Origin", "http://127.0.0.1:1", httpx2.codes.FORBIDDEN),
        ("Host", "other.example", httpx2.codes.BAD_REQUEST),
    ],
)
def test_a_request_for_another_host_or_from_another_origin_is_refused(
    live: Callable[..., Live], header: str, value: str, refused: int
) -> None:
    command = live()

    foreign = httpx2.post(command.address + "end", headers={header: value})

    assert foreign.status_code == refused
    assert command.post("/end") == httpx2.codes.NO_CONTENT
    status, _ = command.ended()
    assert status == 0
    assert "ended: the end button" in command.run.printed()


def test_an_offer_over_the_wires_bound_is_refused_with_413(
    live: Callable[..., Live],
) -> None:
    command = live()

    refused = httpx2.post(
        command.address + "viewers", content=bytes(MAX_OFFER_BYTES + 1)
    )

    assert refused.status_code == httpx2.codes.CONTENT_TOO_LARGE
    command.post("/end")
    command.ended()


def test_an_offer_the_service_refuses_is_relayed_with_its_status_and_body(
    live: Callable[..., Live],
) -> None:
    command = live()

    refused = httpx2.post(command.address + "viewers", content=b"not an offer")

    assert refused.status_code == REFUSAL_STATUS[RefusalCode.INVALID_REQUEST]
    assert refused.json()["error"]["code"] == RefusalCode.INVALID_REQUEST
    command.post("/end")
    command.ended()


def test_a_request_from_the_pages_own_origin_is_answered(
    live: Callable[..., Live],
) -> None:
    command = live()
    origin = command.address.rstrip("/")

    own = httpx2.post(command.address + "end", headers={"Origin": origin})

    assert own.status_code == httpx2.codes.NO_CONTENT
    command.ended()


def test_a_page_nobody_watches_ends_after_its_idle_time(
    live: Callable[..., Live],
) -> None:
    command = live("--idle-seconds", str(IDLE_SECONDS))

    status, _ = command.ended()

    assert status == 0
    assert f"ended: no viewer for {IDLE_SECONDS} seconds" in command.run.printed()


def test_the_page_lists_the_audio_files_and_speaks_the_one_it_is_told_to(
    live: Callable[..., Live], run: Run
) -> None:
    second = run.directory / "second.wav"
    write_audio(second, seconds=SECOND_SECONDS)
    command = live(audio=[second])
    assert httpx2.get(command.address + "files").json() == [run.audio.name, second.name]

    assert command.post(f"/speak/{second.name}") == httpx2.codes.NO_CONTENT
    assert run.wait_until(lambda: bool(command.sentences()), SENTENCE_WITHIN_SECONDS)
    assert command.sentences() == [SECOND_SECONDS * 1000]
    assert command.post("/speak/unknown.wav") == httpx2.codes.NOT_FOUND

    command.post("/end")
    command.ended()


def test_a_file_pressed_again_while_it_plays_is_spoken_again(
    live: Callable[..., Live], run: Run
) -> None:
    second = run.directory / "second.wav"
    write_audio(second, seconds=SECOND_SECONDS)
    gate = run.directory / "sentences.gate"
    run.hold_sentences_until(gate)
    command = live(audio=[second], card=True)

    assert command.post(f"/speak/{second.name}") == httpx2.codes.NO_CONTENT
    assert command.post(f"/speak/{second.name}") == httpx2.codes.NO_CONTENT
    gate.touch()

    assert run.wait_until(
        lambda: command.sentences() == [SECOND_SECONDS * 1000] * 2,
        SENTENCE_WITHIN_SECONDS,
    )
    command.post("/end")
    status, _ = command.ended()
    assert status == 0


def test_clear_is_accepted_while_a_sentence_plays(
    live: Callable[..., Live], run: Run
) -> None:
    command = live()
    command.post(f"/speak/{run.audio.name}")

    assert command.post("/clear") == httpx2.codes.NO_CONTENT

    command.post("/end")
    command.ended()


def test_a_card_run_starts_under_probe_run_and_ends_at_the_button(
    live: Callable[..., Live], run: Run
) -> None:
    command = live(card=True)
    assert command.address

    command.post("/end")

    status, _ = command.ended()
    assert status == 0
    assert run.probe_run_log.read_text().startswith(
        f"-k {KILL_GRACE_SECONDS} {CAP_SECONDS} "
    )


def test_a_card_without_room_for_the_cap_starts_nothing(
    live: Callable[..., Live], run: Run
) -> None:
    run.write_reading(NO_ROOM_FOR_THE_CAP)
    command = live(card=True)

    status, error = command.ended()

    assert status != 0
    assert "refused" in error
    assert not run.probe_run_log.exists()


def test_a_card_that_falls_below_the_floor_ends_the_service(
    live: Callable[..., Live], run: Run
) -> None:
    run.breach_while_loading()
    command = live(card=True)

    status, error = command.ended()

    assert status != 0
    assert "watchdog breach" in error


@pytest.fixture
def given(tmp_path: Path, package: Path) -> Callable[..., list[str]]:
    """The command line with a good audio file, which a case changes one part of."""
    good = tmp_path / "speech.wav"
    write_audio(good, seconds=1)

    def arguments(**changed: object) -> list[str]:
        parts = {
            "log": tmp_path / "service.log",
            "audio": [good],
            "package": package,
            "dry_run": True,
        } | changed
        return [
            "--log",
            str(parts["log"]),
            "--audio",
            *map(str, parts["audio"]),
            "--package",
            str(parts["package"]),
            *(["--dry-run"] if parts["dry_run"] else []),
        ]

    return arguments


def _refusal(arguments: list[str]) -> str:
    with pytest.raises(SystemExit) as refused:
        watch_live.main(arguments)
    return str(refused.value)


def test_audio_that_is_not_mono_is_refused_before_any_service_starts(
    given: Callable[..., list[str]], tmp_path: Path
) -> None:
    stereo = tmp_path / "stereo.wav"
    write_audio(stereo, seconds=1, channels=2)

    assert "mono 16-bit" in _refusal(given(audio=[stereo]))


def test_a_file_name_given_twice_is_refused(
    given: Callable[..., list[str]], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_audio(elsewhere / "speech.wav", seconds=1)

    message = _refusal(given(audio=[tmp_path / "speech.wav", elsewhere / "speech.wav"]))

    assert "twice" in message


def test_a_log_in_a_missing_directory_is_refused(
    given: Callable[..., list[str]], tmp_path: Path
) -> None:
    assert "--log" in _refusal(given(log=tmp_path / "missing" / "service.log"))


def test_a_browser_package_that_is_not_built_is_refused(
    given: Callable[..., list[str]], tmp_path: Path
) -> None:
    assert "not built" in _refusal(given(package=tmp_path))


def test_a_look_image_the_service_refuses_is_refused(
    given: Callable[..., list[str]], tmp_path: Path
) -> None:
    message = _refusal([*given(), "--look-image", str(tmp_path / "missing.png")])

    assert "--look-image" in message


def test_a_card_run_needs_the_providers_root(
    given: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIDEO_PROVIDER_ROOT", raising=False)

    assert "VIDEO_PROVIDER_ROOT" in _refusal(given(dry_run=False))


@pytest.mark.parametrize(
    ("viewers", "elapsed", "expired"),
    [(0, 59, False), (0, 60, True), (1, 600, False)],
)
def test_the_idle_time_runs_only_while_no_viewer_is_attached(
    *, viewers: int, elapsed: float, expired: bool
) -> None:
    idle = watch_live.Idle(seconds=60, since=0)

    assert idle.expired(elapsed, viewers) is expired


def test_the_idle_time_starts_over_when_the_last_viewer_leaves() -> None:
    idle = watch_live.Idle(seconds=60, since=0)
    idle.expired(100, 1)

    assert not idle.expired(159, 0)
    assert idle.expired(160, 0)


class _ViewerLines:
    """The service's lines of `viewer.ended`, as the page counts viewers from them."""

    def __init__(self, *lines: dict[str, str]) -> None:
        self._lines = list(lines)

    def lines(self, _event: str) -> list[dict[str, str]]:
        return self._lines


_ADMITTED_ENDED = {"outcome": "ok", "ended_by": "peer_closed"}
_REFUSED = {"outcome": "refused", "reason": "unavailable"}
_FAILED = {"outcome": "failed"}


@pytest.mark.parametrize(
    ("admitted", "lines", "attached"),
    [
        (0, [], 0),
        (2, [_ADMITTED_ENDED], 1),
        (2, [_ADMITTED_ENDED, _ADMITTED_ENDED], 0),
        (1, [_REFUSED], 1),
        (1, [_FAILED], 1),
    ],
)
def test_the_viewers_attached_are_the_admitted_ones_that_have_not_ended(
    admitted: int, lines: list[dict[str, str]], attached: int
) -> None:
    page = types.SimpleNamespace(admitted=admitted)

    assert watch_live.viewers_attached(_ViewerLines(*lines), page) == attached


_STATE_IS = "document.getElementById('state').dataset.state === '{}'"
_NOTE_IS = "document.getElementById('note').textContent === '{}'"
_WITHIN_MILLISECONDS = 30_000


@pytest.mark.browser
def test_the_page_shows_the_face_speaks_a_file_and_ends_at_its_button(
    live: Callable[..., Live], tmp_path: Path
) -> None:
    command = live("--package", str(PACKAGE_BUILD))
    address = command.address

    async def scenario() -> None:
        async with opened(address, tmp_path / "profile") as page:
            for step, label in (("watch", "live"), ("speak", None), ("clear", None)):
                await page.click(f"#{step}")
                if label is not None:
                    await page.wait_for_function(
                        _STATE_IS.format(label), timeout=_WITHIN_MILLISECONDS
                    )
            await page.wait_for_function(_NOTE_IS.format("Abgeschnitten"))
            await page.click("#end")
            await page.wait_for_function(_STATE_IS.format("ended"))

    asyncio.run(scenario())

    status, _ = command.ended()
    assert status == 0
    assert command.sentences()


@pytest.mark.browser
def test_a_page_whose_tab_closed_ends_after_its_idle_time(
    live: Callable[..., Live], tmp_path: Path
) -> None:
    command = live("--package", str(PACKAGE_BUILD), "--idle-seconds", "5")
    address = command.address

    async def scenario() -> None:
        async with opened(address, tmp_path / "profile") as page:
            await page.click("#watch")
            await page.wait_for_function(
                _STATE_IS.format("live"), timeout=_WITHIN_MILLISECONDS
            )

    asyncio.run(scenario())

    status, _ = command.ended()
    assert status == 0
    assert "ended: no viewer for 5 seconds" in command.run.printed()


class _EndingConnection:
    """A connection whose session ends while a press is sent and the page reads.

    Like a websockets connection it refuses a second `recv` while one is in progress,
    and a send to an ended session raises `ConnectionClosed`.
    """

    def __init__(self) -> None:
        self.sending = asyncio.Event()
        self.reading = asyncio.Event()
        self._send_ended = asyncio.get_running_loop().create_future()
        self._event = asyncio.get_running_loop().create_future()
        self._reading = False
        self._delivered = False

    async def send(self, _message: str | bytes) -> None:
        self.sending.set()
        await self._send_ended
        raise ConnectionClosedOK(None, None)

    async def recv(self) -> str:
        if self._reading:
            message = "cannot call recv while another coroutine is already running recv"
            raise ConcurrencyError(message)
        if self._delivered:
            raise ConnectionClosedOK(None, None)
        self._reading = True
        self.reading.set()
        try:
            return await self._event
        finally:
            self._reading = False
            self._delivered = True

    def __aiter__(self) -> _EndingConnection:
        return self

    async def __anext__(self) -> str:
        try:
            return await self.recv()
        except ConnectionClosedOK:
            raise StopAsyncIteration from None

    def end(self, event: str) -> None:
        """End the session: the press wakes first, then the reader gets `event`."""
        self._send_ended.set_result(None)
        self._event.set_result(event)


def test_a_press_while_the_session_ends_answers_503(tmp_path: Path) -> None:
    error = ErrorEvent(
        error=SessionFailure(
            message="the session ended", type="error", code=RefusalCode.BUSY, param=None
        )
    ).model_dump_json()

    async def scenario() -> int:
        connection = _EndingConnection()
        session = Session(connection, SessionCreated.model_construct())
        ends: list[str] = []

        def over(reason: str, *, failed: bool = False) -> None:  # noqa: ARG001 - the Over signature
            ends.append(reason)

        page = watch_live.LivePage(
            httpx2.AsyncClient(), cast("Identity", None), session, {}, over
        )
        application = watch_live.application(page, tmp_path)
        transport = httpx2.ASGITransport(application, raise_app_exceptions=False)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as browser:
            reader = asyncio.create_task(watch_live.end_with_session(session, over))
            await connection.reading.wait()
            pressing = asyncio.create_task(browser.post("/clear"))
            await connection.sending.wait()
            connection.end(error)
            answered = await pressing
            await reader
        assert ends
        return answered.status_code

    assert asyncio.run(scenario()) == httpx2.codes.SERVICE_UNAVAILABLE
