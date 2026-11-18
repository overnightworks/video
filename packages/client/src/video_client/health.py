"""The health fetch: one blocking `GET /health` (WIRE-12), bounded in time and size.

`fetch_health` takes every input from its call site and returns one
`HealthOutcome`, a closed union a caller matches with `assert_never`:

- `Health`: a 200 whose body validates as `wire.Health`; unknown keys are
  ignored. The content type is not checked, the body decides.
- `UnexpectedStatus`: any other status.
- `Malformed`: a 200 whose body is not a valid `Health`.
- `Cut`, `PastDeadline`, `OverCap` and `Unreachable` (`video_client.outcomes`).

A defect of the caller raises and is no outcome: a deadline or cap of 0 or
below, and a base URL that is not `http`, has no host or carries a path, query or
fragment (`ValueError`).

The call sends no token, blocks and holds no process-global state, so it runs
in a worker thread (`asyncio.to_thread`); the thread ends by the deadline. It
logs nothing. The name lookup is bounded by the system resolver, not by the
deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from pydantic import ValidationError

from video_client._transport import Answer, checked_origin, get
from video_client.outcomes import (
    Cut,
    Malformed,
    OverCap,
    PastDeadline,
    Unreachable,
)
from video_client.wire import HEALTH_PATH, Health


@dataclass(frozen=True, slots=True)
class UnexpectedStatus:
    """The service answered a status other than 200."""

    status: int


type HealthOutcome = (
    Health | UnexpectedStatus | Malformed | OverCap | PastDeadline | Cut | Unreachable
)


def fetch_health(
    base_url: str, *, deadline_seconds: float, cap_bytes: int
) -> HealthOutcome:
    """Read the health of the service on `base_url`, within the bounds.

    `deadline_seconds` bounds the whole call, `cap_bytes` the body. See the module
    for the outcomes and for what raises.
    """
    origin = checked_origin(base_url, deadline_seconds, cap_bytes)
    result = get(
        origin, HEALTH_PATH, deadline_seconds=deadline_seconds, cap_bytes=cap_bytes
    )
    if not isinstance(result, Answer):
        return result
    if result.status != HTTPStatus.OK:
        return UnexpectedStatus(result.status)
    try:
        return Health.model_validate_json(result.body)
    except ValidationError:
        return Malformed()
