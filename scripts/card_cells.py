"""The card proof's cells: what a group drives through one service, and the groups."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, NamedTuple

from card_contention import (
    busy_workers,
    run_queue_delay_seconds,
    thread_count,
    waits,
)
from card_drive import (
    Audio,
    SessionEndedError,
    Window,
    opened_session,
    timed,
    worker_pids,
)
from card_identity import LOOK_NAMES
from card_record import (
    ENCODER_CORES_LINE,
    FIRST_OUTPUT_LINE,
    judged,
    value_of,
)
from card_viewer import Recording, attached, clear_figure, figures
from video_client.session_wire import (
    OutputAudioBufferCleared,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
)
from video_client.wire import RefusalCode

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from card_drive import Service, Session
    from card_identity import Identity

IDLE_SECONDS: Final = 0.5
# After a sentence ends its last frames still reach the peer.
SETTLE_SECONDS: Final = 0.5
STEADY_SECONDS: Final = 1.5
FIVE_VIEWERS: Final = 5
# A clear with nothing queued gets no confirmation, so the wait for the viewer's first
# loud frame ends well inside the sentence's playing time.
CLEAR_WAIT_SHARE: Final = 0.5
_SENTENCE_COLUMNS: Final = ("audio_ms", "frame_count", "late_frame_count", "outcome")
HOLD_SECONDS: Final = 300.0


class Hold(NamedTuple):
    """What the hold cell runs: how long, and beside how many busy workers."""

    seconds: float
    busy_workers: int


@dataclass(frozen=True)
class Drive:
    """What a cell drives: the service, its identity and the audio to speak."""

    service: Service
    identity: Identity
    audio: Audio
    dry_run: bool
    record: Path
    hold: Hold
    written: list[Path] = field(default_factory=list)

    def beside(self, suffix: str) -> Path:
        """A path beside the record, named after it."""
        return beside(self.record, suffix)

    def session(self, look: str) -> AbstractAsyncContextManager[Session]:
        """An opened session on `look`."""
        return opened_session(self.service, self.identity, look)


def beside(record: Path, suffix: str) -> Path:
    """A path beside `record`, named after it."""
    return record.with_name(f"{record.stem}.{suffix}")


def _sentence_figures(
    line: dict[str, object], frame_rate: int, *, dry_run: bool
) -> dict[str, object]:
    """What a sentence's own log line says, against its audio's length."""
    audio_ms = int(line["audio_ms"])
    return (
        {name: value_of(line[name]) for name in _SENTENCE_COLUMNS}
        | {"frames_the_audio_needs": value_of(audio_ms * frame_rate // 1000)}
        | {
            "first_output_ms": judged(
                float(line["first_output_ms"]), FIRST_OUTPUT_LINE, dry_run=dry_run
            )
        }
    )


async def _spoken(drive: Drive, session: Session, name: str) -> dict[str, object]:
    trace_id = await session.speak(name, drive.audio)
    await session.until(OutputAudioBufferStopped)
    line = await drive.service.line("sentence.ended", trace_id=trace_id)
    return {"sentence": name} | _sentence_figures(
        line, session.created.session.video.frame_rate, dry_run=drive.dry_run
    )


async def _two_sentences(
    drive: Drive, session: Session, spoken: list[dict[str, object]]
) -> None:
    """Two sentences of `session` with idle between them, each added to `spoken`."""
    spoken.append(await _spoken(drive, session, "first"))
    await asyncio.sleep(IDLE_SECONDS)
    spoken.append(await _spoken(drive, session, "second"))


async def _sentences(drive: Drive) -> object:
    """Two sentences of one session on a look, with idle between them."""
    spoken: list[dict[str, object]] = []
    async with drive.session(LOOK_NAMES[0]) as session:
        await _two_sentences(drive, session, spoken)
        return {
            "look": LOOK_NAMES[0],
            "opening_ms": value_of(session.opening_ms),
            "sentences": spoken,
        }


async def _clear(drive: Drive) -> object:
    """A clear inside a sentence, watched by a viewer: confirmation and silence.

    The clear waits for the viewer's first loud audio frame, since the viewer's audio
    arrives after the sentence has started, for at most `CLEAR_WAIT_SHARE` of the
    audio's length.
    """
    async with (
        drive.session(LOOK_NAMES[0]) as session,
        attached(drive.service, drive.identity, session.created) as viewer,
    ):
        trace_id = await session.speak("cleared", drive.audio)
        await session.until(OutputAudioBufferStarted)
        await viewer.heard_loud_audio(
            within_seconds=drive.audio.milliseconds * CLEAR_WAIT_SHARE / 1000
        )
        started = time.monotonic()
        await session.clear()
        cleared = await session.until(OutputAudioBufferCleared)
        confirmed_ms = (time.monotonic() - started) * 1000
        line = await drive.service.line("sentence.ended", trace_id=trace_id)
        await asyncio.sleep(SETTLE_SECONDS)
        return {
            "clear_to_confirmation_ms": value_of(confirmed_ms),
            "clear_to_last_loud_frame_ms": clear_figure(
                viewer, started, dry_run=drive.dry_run
            ),
            "audio_played_ms": value_of(cleared.audio_end_ms),
            "outcome": value_of(line["outcome"]),
        }


async def _steady(drive: Drive) -> Window:
    """The window of idle picture after a sentence, once its last frames are in."""
    await asyncio.sleep(SETTLE_SECONDS)
    window, _ = await timed(drive.service, asyncio.sleep(STEADY_SECONDS))
    return window


async def _viewer(drive: Drive) -> object:
    """One recording viewer through a sentence, the idle after it and another."""
    recording = Recording(drive.beside("viewer.mp4"), drive.beside("viewer-sheet"))
    async with drive.session(LOOK_NAMES[0]) as session:
        async with attached(
            drive.service, drive.identity, session.created, recording
        ) as viewer:
            sentence, _ = await timed(drive.service, _spoken(drive, session, "first"))
            idle = await _steady(drive)
            await _spoken(drive, session, "second")
        frame_rate = session.created.session.video.frame_rate
    drive.written.extend([recording.video, *viewer.sheets])
    return {
        "look": LOOK_NAMES[0],
        "viewer": figures(
            viewer,
            sentence=sentence,
            idle=idle,
            frame_rate=frame_rate,
            dry_run=drive.dry_run,
        ),
        "service_cpu_cores": {"one_viewer": value_of(idle.cores)},
        "files": {
            "video": value_of(recording.video.name),
            "sheets": value_of([sheet.name for sheet in viewer.sheets]),
        },
    }


async def _viewers(drive: Drive) -> object:
    """Five viewers of one session: each one's figures, the CPU against none."""
    async with drive.session(LOOK_NAMES[0]) as session:
        frame_rate = session.created.session.video.frame_rate
        none = await _steady(drive)
        async with AsyncExitStack() as stack:
            watchers = [
                await stack.enter_async_context(
                    attached(drive.service, drive.identity, session.created)
                )
                for _ in range(FIVE_VIEWERS)
            ]
            sentence, _ = await timed(drive.service, _spoken(drive, session, "first"))
            idle = await _steady(drive)
    return {
        "viewers": [
            figures(
                watcher,
                sentence=sentence,
                idle=idle,
                frame_rate=frame_rate,
                dry_run=drive.dry_run,
            )
            for watcher in watchers
        ],
        "service_cpu_cores": {
            "no_viewers": value_of(none.cores),
            "five_viewers": value_of(idle.cores),
        },
        "encoders_cores": judged(
            idle.cores - none.cores, ENCODER_CORES_LINE, dry_run=drive.dry_run
        ),
    }


async def _looks(drive: Drive) -> object:
    """One session on each look in sequence, each recorded under its look."""
    sessions = []
    for look in LOOK_NAMES:
        async with drive.session(look) as session:
            sessions.append(
                {"look": look, "opening_ms": value_of(session.opening_ms)}
                | await _spoken(drive, session, look)
            )
    return sessions


async def _lag(drive: Drive) -> object:
    """Two sentences of one session at the service's lag; whether the session starved.

    A session the service ends `unavailable` counts as starved, whatever made a call
    fail: a call that missed the lag, or any other failure of the worker. The sentences
    that completed before are recorded.
    """
    spoken: list[dict[str, object]] = []
    starved = False
    try:
        async with drive.session(LOOK_NAMES[0]) as session:
            await _two_sentences(drive, session, spoken)
    except SessionEndedError as ended:
        if ended.code != RefusalCode.UNAVAILABLE:
            raise
        starved = True
    return {"starved": value_of(starved), "sentences": spoken}


async def _speaking_and_idle(drive: Drive, session: Session) -> None:
    """Alternate a sentence and idle for the hold's seconds.

    A session the service ends `unavailable` ends the hold at that time.
    """
    begun = time.monotonic()
    try:
        while time.monotonic() - begun < drive.hold.seconds:
            await session.speak("held", drive.audio)
            await session.until(OutputAudioBufferStopped)
            await asyncio.sleep(IDLE_SECONDS)
    except SessionEndedError as ended:
        if ended.code != RefusalCode.UNAVAILABLE:
            raise


async def _hold(drive: Drive) -> object:
    """One session alternating speech and idle, beside busy workers on the group's CPUs.

    The group's CPU set is the one its process was pinned to; the busy workers, the
    service and its provider all run on it. The service's line gives how the session
    ended and how far it fell behind.
    """
    cpus = sorted(os.sched_getaffinity(0))
    service_pids = [drive.service.process.pid]
    with busy_workers(drive.hold.busy_workers):
        async with (
            drive.session(LOOK_NAMES[0]) as session,
            attached(drive.service, drive.identity, session.created) as viewer,
        ):
            frame_rate = session.created.session.video.frame_rate
            provider_pids = worker_pids(service_pids[0])
            pids = [*service_pids, *provider_pids]
            service_before, provider_before = waits(service_pids), waits(provider_pids)
            window, _ = await timed(drive.service, _speaking_and_idle(drive, session))
            service_delay = run_queue_delay_seconds(service_before, waits(service_pids))
            provider_delay = run_queue_delay_seconds(
                provider_before, waits(provider_pids)
            )
            threads = thread_count(pids)
            frames = viewer.video_frames_in(window)
    line = await drive.service.line("session.ended")
    return {
        "held_seconds": value_of(window.seconds),
        "frame_rate": value_of(frame_rate),
        "fps": value_of(frames / window.seconds),
        "ended_by": value_of(line.get("ended_by", "none")),
        "max_behind_ms": value_of(line["max_behind_ms"]),
        "max_idle_call_ms": value_of(line["max_idle_call_ms"]),
        "threads": value_of(threads),
        "cpus": value_of(cpus),
        "busy_workers_per_cpu": value_of(drive.hold.busy_workers / len(cpus)),
        "service_run_queue_delay_seconds": value_of(service_delay),
        "provider_run_queue_delay_seconds": value_of(provider_delay),
    }


# A group is one service; each value of the lag sweep runs in a service of its own.
GROUPS: Final = {
    "wire": {
        "sentences": _sentences,
        "clear": _clear,
        "looks": _looks,
        "viewer": _viewer,
        "viewers": _viewers,
    },
    "lag": {"lag": _lag},
    "hold": {"hold": _hold},
}
CELLS: Final = tuple(cell for group in GROUPS.values() for cell in group)
# The hold runs for minutes, so a run names it.
DEFAULT_CELLS: Final = tuple(cell for cell in CELLS if cell != "hold")
