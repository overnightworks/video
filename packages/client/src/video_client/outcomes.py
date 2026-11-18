"""The outcomes the bounded health fetch can end in besides the health itself.

A caller matches one closed union:

- `Cut`: the connection was up and the answer ended, closed or was reset
  before it was whole.
- `PastDeadline`: the total deadline passed, at send or any read.
- `OverCap`: the body would pass the byte cap; reading stops there.
- `Malformed`: the answer breaks the wire.
- `Unreachable`: the name did not resolve or no address accepted the connection in time.

A timeout is `PastDeadline` before anything else; then a closed or reset
connection is `Cut`; only then a parse failure is `Malformed`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Cut:
    """The answer ended, closed or was reset before it was whole."""


@dataclass(frozen=True, slots=True)
class PastDeadline:
    """The total deadline passed."""


@dataclass(frozen=True, slots=True)
class OverCap:
    """The body would pass the byte cap."""


@dataclass(frozen=True, slots=True)
class Malformed:
    """The answer breaks the wire."""


@dataclass(frozen=True, slots=True)
class Unreachable:
    """The connection never came up in time."""
