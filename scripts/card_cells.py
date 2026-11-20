"""The card proof's cells: what a group drives through one service, and the groups."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from card_drive import (
    Audio,
    SessionEndedError,
    Window,
    opened_session,
    timed,
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


@dataclass(frozen=True)
class Drive:
    """What a cell drives: the service, its identity and the audio to speak."""

    service: Service
    identity: Identity
    audio: Audio
    dry_run: bool
    record: Path
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

    A session the service ends `unavailable` is starved: the provider did not answer
    a call within the lag. The sentences that completed before are recorded.
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
}
CELLS: Final = tuple(cell for group in GROUPS.values() for cell in group)
