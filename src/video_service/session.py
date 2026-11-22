"""One session: the sentences a client queues, their playback in order, and the clear.

The receive loop takes the client's events and audio; the producer draws frames
ahead, one provider call at a time, up to what the lag limit allows; the pacer lets
each frame leave at its time on the frame clock, the face at rest included, so the
stream never stops while a chunked provider renders; the drain watcher ends the
session once a drain has let the playing sentence finish. The first of them to end
decides how the session ends. Every call into the provider session runs on one
thread of the session's own, so the provider is never entered twice and a call that
a clear or an ending abandons still finishes before the next one starts. A call that
has not returned within the lag limit ends the session `unavailable`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Final, Never, cast

from logline import StepTimer, unit_trace
from starlette.websockets import WebSocketDisconnect
from video_provider_contract import ProviderFailedError

from video_client.session_wire import (
    MAX_EVENT_BYTES,
    ClientEvent,
    ClientEventError,
    OutputAudioBufferCleared,
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
    SentenceEnd,
    SentenceStart,
    ServerEvent,
    SessionCreated,
    SessionInfo,
    SessionUpdate,
    SessionUpdated,
    client_event_id,
    error_event,
    is_session_update,
    parse_client_event,
)
from video_client.wire import DRAIN_CLOSE_CODE, RefusalCode
from video_service.media import Media

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from concurrent.futures import Future

    from starlette.types import Message
    from starlette.websockets import WebSocket
    from video_provider_contract import Frame

    from video_service.callers import Opener
    from video_service.runtime import Place

logger = logging.getLogger(__name__)

_NORMAL_CLOSE: Final = 1000
_BYTES_PER_SAMPLE: Final = 2
_MS_PER_SECOND: Final = 1000
_PARAM_TYPE: Final = "type"
_PARAM_SENTENCE: Final = "sentence"
_PARAM_AUDIO: Final = "audio"


class EndedBy(StrEnum):
    """The path that ended a session `unavailable`, named in its line."""

    LATE_FRAME = "late_frame"
    CALL_TIMEOUT = "call_timeout"
    PROVIDER_FAILED = "provider_failed"


class _FailureError(Exception):
    """The session ends with an `error` event."""

    def __init__(
        self,
        code: RefusalCode,
        param: str | None = None,
        event_id: str | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.param = param
        self.event_id = event_id


class _UnavailableError(_FailureError):
    """The session ends `unavailable`, by the path it names."""

    def __init__(self, ended_by: EndedBy) -> None:
        super().__init__(RefusalCode.UNAVAILABLE)
        self.ended_by = ended_by


@dataclass(frozen=True, slots=True)
class _End:
    """How a session ended: the close it sends, if the client is still there."""

    close_code: int | None
    failure: _FailureError | None = None


@dataclass(slots=True)
class _Chunk:
    arrived_at: float
    pcm: memoryview

    def take(self, size: int) -> bytes:
        """The first `size` bytes of the audio left, which they leave."""
        taken = bytes(self.pcm[:size])
        self.pcm = self.pcm[size:]
        return taken


@dataclass(slots=True)
class _Sentence:
    id: str
    rate: int
    traceparent: str | None
    generation: int
    first_audio_at: float = 0.0
    chunks: deque[_Chunk] = field(default_factory=deque)
    samples: int = 0
    ended: bool = False
    ended_at: float = 0.0
    step: StepTimer | None = None
    frames_drawn: int = 0
    drawn: bool = False
    started: bool = False
    frames_left: int = 0
    late_frames: int = 0
    heard: bytearray = field(default_factory=bytearray)

    @property
    def audio_ms(self) -> int:
        return self.samples * _MS_PER_SECOND // self.rate

    @property
    def awaiting_audio(self) -> bool:
        """Whether no audio waits to be fed and the client may still send some."""
        return not self.chunks and not self.ended

    def take_audio(self, frame_rate: int) -> tuple[float, bytes]:
        """The next frame period of audio at most, and when its message arrived."""
        chunk = self.chunks[0]
        pcm = chunk.take(max(1, self.rate // frame_rate) * _BYTES_PER_SAMPLE)
        if not chunk.pcm:
            self.chunks.popleft()
        return chunk.arrived_at, pcm

    def take_period(self, frame_rate: int) -> bytes:
        """The audio of the next frame to leave, off the audio the provider heard."""
        first = self.frames_left * self.rate // frame_rate
        last = (self.frames_left + 1) * self.rate // frame_rate
        size = (last - first) * _BYTES_PER_SAMPLE
        period = bytes(self.heard[:size])
        del self.heard[:size]
        return period


@dataclass(frozen=True, slots=True)
class _Drawn:
    """A frame the provider drew, until the pacer lets it leave or a clear drops it."""

    generation: int
    frame: Frame
    sentence: _Sentence | None
    arrived_at: float


@dataclass(frozen=True, slots=True)
class _Finished:
    """The mark behind a sentence's last frame."""

    sentence: _Sentence


@dataclass(slots=True)
class _Schedule:
    """When the next frame is due on the frame clock, and how late the last one left.

    Input that arrives after the last frame has left restarts the clock, as the wait
    for it is the client's, yet keeps the lateness that frame had; for input that was
    already there, `max` keeps the earlier due time and with it the lag the frames
    before it built up.
    """

    frame_seconds: float
    due: float = 0.0
    behind: float = 0.0

    def input_arrived(self, at: float) -> None:
        """Note input that arrived at `at`."""
        self.due = max(self.due, at - self.behind)

    def release(self, now: float) -> float:
        """Let the next frame leave at `now`; how late it is, early when negative."""
        lateness = now - self.due
        self.behind = max(lateness, 0.0)
        self.due += self.frame_seconds
        return lateness

    def give_back(self) -> None:
        """Take back the frame period of a frame that never left."""
        self.due -= self.frame_seconds


class Session:
    """The state machine of one session; `run` accepts its upgrade and serves it."""

    def __init__(  # noqa: PLR0913 - the parts the route hands each session
        self,
        *,
        session_id: str,
        look: str,
        provider: str,
        socket: WebSocket,
        place: Place,
        opener: Opener,
    ) -> None:
        """Serve `socket` on `place`; the session's line names `look` and `provider`.

        Only `opener`, the caller who opened the session, may add viewers to it.
        """
        self._id = session_id
        self._look = look
        self._provider = provider
        self._socket = socket
        self._place = place
        self._frame_rate = place.video.frame_rate
        self._lock = asyncio.Lock()
        self._woken = asyncio.Event()
        self._drained = asyncio.Event()
        self._queue: deque[_Sentence] = deque()
        self._open: _Sentence | None = None
        self._playing: _Sentence | None = None
        self._draining = False
        self._schedule = _Schedule(1 / self._frame_rate)
        self._look_ahead = max(1, self._lag_ms * self._frame_rate // _MS_PER_SECOND)
        self._generation = 0
        self._input_at = 0.0
        self._buffer: deque[_Drawn | _Finished] = deque()
        self._drawn = asyncio.Event()
        self._fed_generation: int | None = None
        self._sentence_count = 0
        self._max_behind = 0.0
        self._max_idle_call = 0.0
        self._media = Media(place.video, place.limits.max_viewers, place.peers)
        self.opener = opener

    @property
    def media(self) -> Media:
        """The viewers of this session."""
        return self._media

    async def run(self) -> None:
        """Accept the upgrade, serve the session to its close, and write its line.

        The client's `session.update` names the trace the rest of the session,
        its line included, is written in. An upgrade the client left before it was
        accepted still closes the provider session and gives the place back.
        """
        try:
            await self._socket.accept()
            with (
                contextlib.ExitStack() as trace,
                StepTimer(
                    "session.ended", logger, look=self._look, provider=self._provider
                ) as step,
            ):
                end = await self._serve(trace)
                if end.close_code is None:
                    # The client has left, so the session has ended: it counts open
                    # no more while its viewers wind down.
                    self._place.end()
                await self._media.close()
                await self._finish(step, end)
            self._place.end()
        finally:
            await self._media.close()
            closing = self._place.worker.submit(self._place.session.close)
            self._place.worker.stop()
            task = asyncio.current_task()
            try:
                # Cancelled, the session is cut at the process's exit, which a close
                # queued behind a stuck call must not hold back.
                if task is None or not task.cancelling():
                    await self._close_provider_session(closing)
            finally:
                self._place.release()

    async def _close_provider_session(self, closing: Future[None]) -> None:
        """Wait for the provider session's close no longer than the lag limit."""
        try:
            async with asyncio.timeout(self._lag_limit):
                # Shielded: the timeout never drops the close still queued.
                await asyncio.shield(asyncio.wrap_future(closing))
        except TimeoutError:
            logger.warning("provider_session.close_stuck")

    async def _serve(self, trace: contextlib.ExitStack) -> _End:
        try:
            await self._send(
                SessionCreated(
                    session=SessionInfo(
                        id=self._id, look=self._look, video=self._place.video
                    )
                )
            )
            update = await self._first_to_end(self._first_update())
            if isinstance(update, _End):
                return update
            trace.enter_context(unit_trace(update.traceparent))
            self._media.configure(update.session)
            await self._first_idle_frame()
            await self._send(SessionUpdated())
            return await self._converse()
        except _FailureError as failure:
            return _End(_NORMAL_CLOSE, failure)
        except ProviderFailedError:
            return _End(
                _NORMAL_CLOSE,
                _UnavailableError(EndedBy.PROVIDER_FAILED),
            )

    async def _finish(self, step: StepTimer, end: _End) -> None:
        failure = end.failure
        step.add(
            sentence_count=self._sentence_count,
            viewer_count=self._media.admitted,
            max_behind_ms=_milliseconds(self._max_behind),
            max_idle_call_ms=_milliseconds(self._max_idle_call),
        )
        if end.close_code is not None:
            step.add(close_code=end.close_code)
        if failure is not None:
            if isinstance(failure, _UnavailableError):
                step.add(ended_by=failure.ended_by.value)
                step.fail(failure.code.value)
            else:
                step.refuse(failure.code.value)
            await self._send(error_event(failure.code, failure.param, failure.event_id))
        if end.close_code is not None:
            with contextlib.suppress(WebSocketDisconnect):
                await self._socket.close(end.close_code)

    async def _first_update(self) -> SessionUpdate | _End:
        message = await self._socket.receive()
        if message["type"] == "websocket.disconnect":
            return _End(None)
        text = message.get("text")
        if text is None:
            raise _FailureError(RefusalCode.INVALID_REQUEST, _PARAM_TYPE)
        return cast("SessionUpdate", _parse(text, session_update=True))

    async def _converse(self) -> _End:
        try:
            return await self._first_to_end(
                self._receive(), self._produce(), self._pace()
            )
        finally:
            for sentence in self._queue:
                self._end_line(sentence, cancelled=True)

    async def _first_to_end[Outcome](self, *work: Awaitable[Outcome]) -> Outcome | _End:
        """The first of `work` to end, or the drain's end, whichever is first."""
        tasks = [asyncio.ensure_future(step) for step in work]
        tasks.append(asyncio.create_task(self._until_drained()))
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            return next(task for task in tasks if task in done).result()
        finally:
            for task in tasks:
                task.cancel()
            # Shielded, so a cancellation of this task that arrives while the steps
            # wind down raises as itself, which its canceller recognises, and not as
            # the cancellation of a step.
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))

    async def _until_drained(self) -> _End:
        await self._place.draining.wait()
        async with self._lock:
            self._draining = True
            if self._playing is None:
                self._drained.set()
        await self._drained.wait()
        return _End(DRAIN_CLOSE_CODE)

    async def _receive(self) -> _End:
        while True:
            message = await self._socket.receive()
            if message["type"] == "websocket.disconnect":
                return _End(None)
            await self._on_message(message)

    async def _on_message(self, message: Message) -> None:
        text = message.get("text")
        if text is None:
            self._on_audio(message["bytes"])
            return
        match _parse(text, session_update=False):
            case SentenceStart() as event:
                self._start_sentence(event)
            case SentenceEnd() as event:
                self._end_sentence(event)
            case _:
                await self._clear()

    def _start_sentence(self, event: SentenceStart) -> None:
        if self._open is not None:
            raise _FailureError(
                RefusalCode.INVALID_REQUEST, _PARAM_TYPE, event.event_id
            )
        if any(queued.id == event.sentence for queued in self._queue):
            raise _FailureError(
                RefusalCode.INVALID_REQUEST, _PARAM_SENTENCE, event.event_id
            )
        sentence = _Sentence(
            event.sentence, event.format.rate, event.traceparent, self._generation
        )
        self._queue.append(sentence)
        self._open = sentence
        self._sentence_count += 1
        self._woken.set()

    def _on_audio(self, pcm: bytes) -> None:
        sentence = self._open
        if sentence is None or len(pcm) % _BYTES_PER_SAMPLE:
            raise _FailureError(RefusalCode.INVALID_REQUEST, _PARAM_AUDIO)
        if sentence.samples == 0:
            # logline reads the start of the sentence's line off its own clock.
            sentence.first_audio_at = time.perf_counter()
        self._input_at = self._place.pacing.now()
        sentence.chunks.append(_Chunk(self._input_at, memoryview(pcm)))
        sentence.samples += len(pcm) // _BYTES_PER_SAMPLE
        if self._queued_seconds() > self._place.limits.max_queued_audio_seconds:
            raise _FailureError(RefusalCode.TOO_LARGE)
        self._woken.set()

    def _end_sentence(self, event: SentenceEnd) -> None:
        sentence = self._open
        if sentence is None or sentence.id != event.sentence or sentence.samples == 0:
            raise _FailureError(
                RefusalCode.INVALID_REQUEST, _PARAM_SENTENCE, event.event_id
            )
        self._input_at = sentence.ended_at = self._place.pacing.now()
        sentence.ended = True
        self._open = None
        self._woken.set()

    def _queued_seconds(self) -> float:
        received = sum(queued.samples / queued.rate for queued in self._queue)
        played = sum(queued.frames_left for queued in self._queue) / self._frame_rate
        return received - played

    async def _clear(self) -> None:
        async with self._lock:
            dropped = list(self._queue)
            self._queue.clear()
            self._buffer.clear()
            self._generation += 1
            self._input_at = self._place.pacing.now()
            self._open = None
            self._playing = None
            for sentence in dropped:
                self._end_line(sentence, cancelled=True)
                await self._send(
                    OutputAudioBufferCleared(
                        response_id=sentence.id, audio_end_ms=self._played_ms(sentence)
                    )
                )
            if self._draining:
                self._drained.set()
        self._woken.set()

    def _played_ms(self, sentence: _Sentence) -> int:
        frames_ms = sentence.frames_left * _MS_PER_SECOND // self._frame_rate
        return min(frames_ms, sentence.audio_ms)

    async def _first_idle_frame(self) -> None:
        """Draw the first frame of the face at rest; it starts the frame clock.

        Its render is no lag, which the call's own bound holds to `VIDEO_MAX_LAG_MS`.
        """
        frame = await self._idle_frame()
        self._schedule.input_arrived(self._place.pacing.now())
        await self._hold_to_due()
        self._media.send_idle(frame)

    async def _idle_frame(self) -> Frame:
        """Call the provider for a frame of the face at rest, timing the call.

        Most idle calls are served from frames already drawn, so the longest one
        is the render that fell on the session.
        """
        started = self._place.pacing.now()
        frame = await self._call(self._place.session.idle_frame)
        self._max_idle_call = max(
            self._max_idle_call, self._place.pacing.now() - started
        )
        return frame

    async def _wait(self) -> None:
        await self._woken.wait()
        self._woken.clear()

    async def _produce(self) -> Never:
        """Draw frames ahead of the pacer, the provider called one call at a time."""
        while True:
            await self._forget_cleared()
            draw = self._next_draw()
            if draw is None:
                await self._wait()
            else:
                await draw()

    async def _forget_cleared(self) -> None:
        """Have the provider drop the audio of a cleared sentence, after its call."""
        fed = self._fed_generation
        if fed is not None and fed != self._generation:
            await self._call(self._place.session.clear)
            self._fed_generation = None

    def _next_draw(self) -> Callable[[], Awaitable[None]] | None:
        """What to draw next, or `None` while the buffer is full or audio is awaited.

        Until the first frame of the next sentence is drawn, the face at rest is.
        """
        sentence = next((queued for queued in self._queue if not queued.drawn), None)
        if sentence is None or (sentence.awaiting_audio and not sentence.frames_drawn):
            return self._draw_idle if len(self._buffer) < self._look_ahead else None
        if sentence.awaiting_audio or self._buffered_frames() >= self._look_ahead:
            return None
        return partial(self._draw_sentence, sentence)

    def _buffered_frames(self) -> int:
        """The frames of sentences drawn ahead and not yet let go."""
        return sum(
            isinstance(entry, _Drawn) and entry.sentence is not None
            for entry in self._buffer
        )

    def _is_current(self, generation: int) -> bool:
        """Whether no interruption has begun a newer generation since `generation`."""
        return generation == self._generation

    async def _draw_idle(self) -> None:
        generation, arrived_at = self._generation, self._input_at
        frame = await self._idle_frame()
        if self._is_current(generation):
            self._buffer.append(_Drawn(generation, frame, None, arrived_at))
            self._drawn.set()

    async def _draw_sentence(self, sentence: _Sentence) -> None:
        """Feed `sentence` a frame period of its audio, or end it once it has none."""
        provider = self._place.session
        finishing = not sentence.chunks
        if finishing:
            arrived_at = sentence.ended_at
            draw = partial(_drawn_by, provider.finish_sentence)
        else:
            arrived_at, pcm = sentence.take_audio(self._frame_rate)
            if sentence.step is None:
                sentence.step = StepTimer(
                    "sentence.ended", logger, started_at=sentence.first_audio_at
                )
            sentence.heard += pcm
            self._fed_generation = sentence.generation
            draw = partial(_drawn_by, provider.feed, pcm, sentence.rate)
        frames = await self._call(draw)
        if finishing:
            self._fed_generation = None
        if sentence.generation != self._generation:
            return
        if frames and not sentence.frames_drawn:
            self._buffer = deque(
                entry
                for entry in self._buffer
                if not isinstance(entry, _Drawn) or entry.sentence is not None
            )
        sentence.frames_drawn += len(frames)
        self._buffer.extend(
            _Drawn(sentence.generation, frame, sentence, arrived_at) for frame in frames
        )
        if finishing:
            sentence.drawn = True
            self._buffer.append(_Finished(sentence))
        self._drawn.set()

    async def _pace(self) -> Never:
        """Let each frame the producer drew leave at its time on the frame clock."""
        while True:
            while not self._buffer:
                await self._drawn.wait()
                self._drawn.clear()
            entry = self._buffer.popleft()
            self._woken.set()
            if isinstance(entry, _Finished):
                await self._stop(entry.sentence)
                continue
            self._schedule.input_arrived(entry.arrived_at)
            late = await self._hold_to_due()
            await self._leave(entry, late=late)

    async def _hold_to_due(self) -> bool:
        """Hold the frame until it is due; whether it was late, too late fails."""
        lateness = self._schedule.release(self._place.pacing.now())
        self._max_behind = max(self._max_behind, lateness)
        if lateness > self._lag_limit:
            raise _UnavailableError(EndedBy.LATE_FRAME)
        if lateness > 0:
            return True
        await self._place.pacing.sleep(-lateness)
        return False

    async def _leave(self, entry: _Drawn, *, late: bool) -> None:
        """A frame leaves unless a clear dropped it; a sentence's first starts it."""
        async with self._lock:
            if entry.generation != self._generation:
                self._schedule.give_back()
                return
            sentence = entry.sentence
            if sentence is None:
                self._media.send_idle(entry.frame)
                return
            period = sentence.take_period(self._frame_rate)
            self._media.send(entry.frame, period, sentence.rate)
            sentence.frames_left += 1
            sentence.late_frames += late
            if not sentence.started:
                sentence.started = True
                self._playing = sentence
                cast("StepTimer", sentence.step).first_output()
                await self._send(OutputAudioBufferStarted(response_id=sentence.id))

    async def _stop(self, sentence: _Sentence) -> None:
        async with self._lock:
            if sentence.generation != self._generation:
                return
            self._queue.popleft()
            self._playing = None
            await self._send(
                OutputAudioBufferStopped(
                    response_id=sentence.id, audio_end_ms=sentence.audio_ms
                )
            )
            if self._draining:
                self._drained.set()
        self._end_line(sentence, cancelled=False)

    def _end_line(self, sentence: _Sentence, *, cancelled: bool) -> None:
        """Write the sentence's line, once, in the sentence's trace."""
        step = sentence.step
        if step is None:
            return
        step.add(
            audio_ms=sentence.audio_ms,
            frame_count=sentence.frames_left,
            late_frame_count=sentence.late_frames,
        )
        with unit_trace(sentence.traceparent):
            if cancelled:
                step.end_cancelled()
            else:
                step.end()

    async def _send(self, event: ServerEvent) -> None:
        """Send `event`; a client that has gone ends the session through `receive`."""
        with contextlib.suppress(WebSocketDisconnect):
            await self._socket.send_text(event.model_dump_json())

    @property
    def _lag_ms(self) -> int:
        return self._place.limits.max_lag_ms

    @property
    def _lag_limit(self) -> float:
        return self._lag_ms / _MS_PER_SECOND

    async def _call[Result](self, work: Callable[[], Result]) -> Result:
        """Run `work` on the provider thread; not back within the lag limit, fail."""
        try:
            async with asyncio.timeout(self._lag_limit):
                return await asyncio.wrap_future(self._place.worker.submit(work))
        except TimeoutError:
            raise _UnavailableError(EndedBy.CALL_TIMEOUT) from None


def _milliseconds(seconds: float) -> int:
    return round(seconds * _MS_PER_SECOND)


def _drawn_by(draw: Callable[..., Iterable[Frame]], *arguments: object) -> list[Frame]:
    """Every frame one provider call draws, drawn on the provider thread."""
    return list(draw(*arguments))


def _parse(text: str, *, session_update: bool) -> ClientEvent:
    """The event in `text`, which must be a `session.update` if `session_update` is set.

    The type is judged before any other field, so an event out of order ends the
    session naming its type, whatever else is wrong with it.
    """
    if len(text.encode()) > MAX_EVENT_BYTES:
        raise _FailureError(RefusalCode.TOO_LARGE)
    if is_session_update(text) is not session_update:
        raise _FailureError(
            RefusalCode.INVALID_REQUEST, _PARAM_TYPE, client_event_id(text)
        )
    try:
        return parse_client_event(text)
    except ClientEventError as error:
        raise _FailureError(
            RefusalCode.INVALID_REQUEST, error.param, error.event_id
        ) from None
