"""One session: the sentences a client queues, their playback in order, and the clear.

The receive loop takes the client's events and audio; the player plays the queued
sentences one after another on the frame clock; the drain watcher ends the session
once a drain has let the playing sentence finish. The first of them to end decides
how the session ends. Every call into the provider session runs on one thread of
the session's own, so the provider is never entered twice and a call that a clear
or an ending abandons still finishes before the next one starts. A call that has
not returned within the lag limit ends the session `unavailable`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from concurrent.futures import Future

    from starlette.types import Message
    from starlette.websockets import WebSocket
    from video_provider_contract import Frame

    from video_client.session_wire import IceConfiguration
    from video_service.runtime import Place

logger = logging.getLogger(__name__)

_NORMAL_CLOSE: Final = 1000
_BYTES_PER_SAMPLE: Final = 2
_MS_PER_SECOND: Final = 1000
_PARAM_TYPE: Final = "type"
_PARAM_SENTENCE: Final = "sentence"
_PARAM_AUDIO: Final = "audio"


class _Failure(Exception):  # noqa: N818 - an ending of the session, not a defect
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


@dataclass(frozen=True, slots=True)
class _End:
    """How a session ended: the close it sends, if the client is still there."""

    close_code: int | None
    failure: _Failure | None = None


@dataclass(frozen=True, slots=True)
class _Chunk:
    arrived_at: float
    pcm: bytes


@dataclass(slots=True)
class _Sentence:
    id: str
    rate: int
    traceparent: str | None
    first_audio_at: float = 0.0
    chunks: deque[_Chunk] = field(default_factory=deque)
    samples: int = 0
    ended: bool = False
    ended_at: float = 0.0
    dropped: bool = False
    started: bool = False
    frames_left: int = 0
    late_frames: int = 0

    @property
    def audio_ms(self) -> int:
        return self.samples * _MS_PER_SECOND // self.rate


@dataclass(slots=True)
class _Schedule:
    """When the next frame is due on the audio's clock, and how late the last one left.

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


class Session:
    """The state machine of one accepted session; `run` serves it to its end."""

    def __init__(
        self,
        *,
        session_id: str,
        look: str,
        provider: str,
        socket: WebSocket,
        place: Place,
    ) -> None:
        """Serve `socket` on `place`; the session's line names `look` and `provider`."""
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
        self._sentence_count = 0
        self._ice: IceConfiguration | None = None

    async def run(self) -> None:
        """Serve the session from its accepted upgrade to its close; write its line.

        The client's `session.update` names the trace the rest of the session,
        its line included, is written in.
        """
        try:
            with (
                contextlib.ExitStack() as trace,
                StepTimer(
                    "session.ended", logger, look=self._look, provider=self._provider
                ) as step,
            ):
                end = await self._serve(trace)
                await self._finish(step, end)
        finally:
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
            self._ice = update.session
            await self._call(self._place.session.idle_frame)
            await self._send(SessionUpdated())
            return await self._converse()
        except _Failure as failure:
            return _End(_NORMAL_CLOSE, failure)
        except ProviderFailedError:
            return _End(_NORMAL_CLOSE, _Failure(RefusalCode.UNAVAILABLE))

    async def _finish(self, step: StepTimer, end: _End) -> None:
        failure = end.failure
        step.add(sentence_count=self._sentence_count)
        if end.close_code is not None:
            step.add(close_code=end.close_code)
        if failure is not None:
            if failure.code is RefusalCode.UNAVAILABLE:
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
            raise _Failure(RefusalCode.INVALID_REQUEST, _PARAM_TYPE)
        return cast("SessionUpdate", _parse(text, session_update=True))

    async def _converse(self) -> _End:
        return await self._first_to_end(self._receive(), self._play())

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
            raise _Failure(RefusalCode.INVALID_REQUEST, _PARAM_TYPE, event.event_id)
        if any(queued.id == event.sentence for queued in self._queue):
            raise _Failure(RefusalCode.INVALID_REQUEST, _PARAM_SENTENCE, event.event_id)
        sentence = _Sentence(event.sentence, event.format.rate, event.traceparent)
        self._queue.append(sentence)
        self._open = sentence
        self._sentence_count += 1
        self._woken.set()

    def _on_audio(self, pcm: bytes) -> None:
        sentence = self._open
        if sentence is None or len(pcm) % _BYTES_PER_SAMPLE:
            raise _Failure(RefusalCode.INVALID_REQUEST, _PARAM_AUDIO)
        if sentence.samples == 0:
            # logline reads the start of the sentence's line off its own clock.
            sentence.first_audio_at = time.perf_counter()
        sentence.chunks.append(_Chunk(self._place.pacing.now(), pcm))
        sentence.samples += len(pcm) // _BYTES_PER_SAMPLE
        if self._queued_seconds() > self._place.limits.max_queued_audio_seconds:
            raise _Failure(RefusalCode.TOO_LARGE)
        self._woken.set()

    def _end_sentence(self, event: SentenceEnd) -> None:
        sentence = self._open
        if sentence is None or sentence.id != event.sentence or sentence.samples == 0:
            raise _Failure(RefusalCode.INVALID_REQUEST, _PARAM_SENTENCE, event.event_id)
        sentence.ended = True
        sentence.ended_at = self._place.pacing.now()
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
            self._open = None
            self._playing = None
            for sentence in dropped:
                sentence.dropped = True
            for sentence in dropped:
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

    async def _play(self) -> Never:
        while True:
            while not self._queue:
                await self._wait()
            await self._play_sentence(self._queue[0])

    async def _wait(self) -> None:
        await self._woken.wait()
        self._woken.clear()

    async def _play_sentence(self, sentence: _Sentence) -> None:
        with unit_trace(sentence.traceparent):
            chunk = await self._next_chunk(sentence)
            if chunk is not None:
                with StepTimer(
                    "sentence.ended", logger, started_at=sentence.first_audio_at
                ) as step:
                    await self._render(sentence, step, chunk)
            if sentence.dropped:
                await self._call(self._place.session.clear)
                await self._call(self._place.session.idle_frame)

    async def _next_chunk(self, sentence: _Sentence) -> bytes | None:
        """The next audio of `sentence`, or `None` once it is complete or dropped."""
        while not sentence.dropped:
            if sentence.chunks:
                chunk = sentence.chunks.popleft()
                self._schedule.input_arrived(chunk.arrived_at)
                return chunk.pcm
            if sentence.ended:
                self._schedule.input_arrived(sentence.ended_at)
                return None
            await self._wait()
        return None

    async def _render(self, sentence: _Sentence, step: StepTimer, chunk: bytes) -> None:
        provider = self._place.session
        while chunk is not None:
            frames = await self._call(partial(provider.feed, chunk, sentence.rate))
            await self._send_frames(sentence, step, frames)
            chunk = await self._next_chunk(sentence)
        if not sentence.dropped:
            await self._send_frames(
                sentence, step, await self._call(provider.finish_sentence)
            )
            await self._stop(sentence)
        step.add(
            audio_ms=sentence.audio_ms,
            frame_count=sentence.frames_left,
            late_frame_count=sentence.late_frames,
        )
        if sentence.dropped:
            step.end_cancelled()

    async def _send_frames(
        self, sentence: _Sentence, step: StepTimer, frames: Iterator[Frame]
    ) -> None:
        """Release each frame of `frames` at its time on the audio's clock."""
        while (
            not sentence.dropped
            and await self._call(partial(next, frames, None)) is not None
        ):
            lateness = self._schedule.release(self._place.pacing.now())
            if lateness > self._lag_limit:
                raise _Failure(RefusalCode.UNAVAILABLE)
            if lateness > 0:
                sentence.late_frames += 1
            else:
                await self._place.pacing.sleep(-lateness)
            await self._leave(sentence, step)

    async def _leave(self, sentence: _Sentence, step: StepTimer) -> None:
        """A frame leaves for the viewers; the sentence's first one starts it."""
        async with self._lock:
            if sentence.dropped:
                return
            sentence.frames_left += 1
            if not sentence.started:
                sentence.started = True
                self._playing = sentence
                step.first_output()
                await self._send(OutputAudioBufferStarted(response_id=sentence.id))

    async def _stop(self, sentence: _Sentence) -> None:
        async with self._lock:
            if sentence.dropped:
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

    async def _send(self, event: ServerEvent) -> None:
        """Send `event`; a client that has gone ends the session through `receive`."""
        with contextlib.suppress(WebSocketDisconnect):
            await self._socket.send_text(event.model_dump_json())

    @property
    def _lag_limit(self) -> float:
        return self._place.limits.max_lag_ms / _MS_PER_SECOND

    async def _call[Result](self, work: Callable[[], Result]) -> Result:
        """Run `work` on the provider thread; not back within the lag limit, fail."""
        try:
            async with asyncio.timeout(self._lag_limit):
                return await asyncio.wrap_future(self._place.worker.submit(work))
        except TimeoutError:
            raise _Failure(RefusalCode.UNAVAILABLE) from None


def _parse(text: str, *, session_update: bool) -> ClientEvent:
    """The event in `text`, which must be a `session.update` if `session_update` is set.

    The type is judged before any other field, so an event out of order ends the
    session naming its type, whatever else is wrong with it.
    """
    if len(text.encode()) > MAX_EVENT_BYTES:
        raise _Failure(RefusalCode.TOO_LARGE)
    if is_session_update(text) is not session_update:
        raise _Failure(RefusalCode.INVALID_REQUEST, _PARAM_TYPE, client_event_id(text))
    try:
        return parse_client_event(text)
    except ClientEventError as error:
        raise _Failure(
            RefusalCode.INVALID_REQUEST, error.param, error.event_id
        ) from None
