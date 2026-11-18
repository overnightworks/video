"""A client socket that plays a script, read at once rather than over time.

A step of the script that is a coroutine function runs before the next message is
handed out, so a test can wait for the session or move its clock in between.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import TYPE_CHECKING

from video_client.session_wire import (
    OutputAudioBufferStarted,
    OutputAudioBufferStopped,
    parse_server_event,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import Message

    from video_client.session_wire import ServerEvent

type Step = Callable[[ScriptedSocket], Awaitable[None]]


class ScriptedSocket:
    """Hands the session its `script`, then stays until the sentences have stopped."""

    def __init__(
        self, script: list[dict[str, object] | bytes | Step], sentences: int
    ) -> None:
        """Take the client events, audio and steps to play, in order, of `sentences`."""
        self.sent: list[ServerEvent] = []
        self.close_code: int | None = None
        self.started = asyncio.Event()
        self._script = deque(script)
        self._sentences_left = sentences
        self._all_stopped = asyncio.Event()
        if not sentences:
            self._all_stopped.set()

    async def receive(self) -> Message:
        """The next scripted message, which waits only for the steps before it."""
        while self._script and callable(step := self._script[0]):
            self._script.popleft()
            await step(self)
        if not self._script:
            await self._all_stopped.wait()
            return {"type": "websocket.disconnect"}
        event = self._script.popleft()
        if isinstance(event, bytes):
            return {"type": "websocket.receive", "bytes": event}
        return {"type": "websocket.receive", "text": json.dumps(event)}

    async def send_text(self, text: str) -> None:
        """Keep the server event that arrives."""
        event = parse_server_event(text)
        self.sent.append(event)
        if isinstance(event, OutputAudioBufferStarted):
            self.started.set()
        if isinstance(event, OutputAudioBufferStopped):
            self._sentences_left -= 1
            if not self._sentences_left:
                self._all_stopped.set()

    async def close(self, code: int) -> None:
        """Note the close."""
        self.close_code = code
