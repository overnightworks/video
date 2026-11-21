"""Pick out the changed paths the image is built from.

The image's inputs are what its build context holds: the `.dockerignore` allowlist
and the two files Docker always sends, `Dockerfile` and `.dockerignore`. The CI job
that decides whether a pull request builds the image pipes the paths it changed
through this command, so the allowlist stays the one owner of that set.

Docker reads a pattern as anchored at the context root, where `*` stays inside one
path segment and `**` crosses segments, and the last pattern that matches a path or
one of its parent directories decides.
"""

# ruff: noqa: T201 - the CI job reads this command's output

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final, NamedTuple

REPOSITORY_ROOT: Final = Path(__file__).resolve().parent.parent
_ALWAYS_SENT: Final = frozenset({"Dockerfile", ".dockerignore"})
_WILDCARDS: Final = re.compile(r"\*\*/|\*\*|\*|\?")
_REGULAR_EXPRESSIONS: Final = {
    "**/": "(?:.*/)?",
    "**": ".*",
    "*": "[^/]*",
    "?": "[^/]",
}


def _pattern_expression(pattern: str) -> re.Pattern[str]:
    """The expression a path must match completely to match the pattern."""
    expression = ""
    position = 0
    for wildcard in _WILDCARDS.finditer(pattern):
        expression += re.escape(pattern[position : wildcard.start()])
        expression += _REGULAR_EXPRESSIONS[wildcard.group()]
        position = wildcard.end()
    return re.compile(expression + re.escape(pattern[position:]))


class _Rule(NamedTuple):
    included: bool
    pattern: str
    expression: re.Pattern[str]


class BuildContext:
    """The paths a `.dockerignore` lets into the build context."""

    def __init__(self, dockerignore: str) -> None:
        """Read the rules the file's lines give, in order."""
        lines = (line.strip() for line in dockerignore.splitlines())
        self._rules = [
            _Rule(
                line.startswith("!"),
                pattern := line.removeprefix("!").strip("/"),
                _pattern_expression(pattern),
            )
            for line in lines
            if line and not line.startswith("#")
        ]

    def holds(self, path: str) -> bool:
        """Whether the build context holds the path, or files beneath it."""
        if path in _ALWAYS_SENT:
            return True
        parts = path.split("/")
        parents = ["/".join(parts[:count]) for count in range(1, len(parts) + 1)]
        held = False
        for rule in self._rules:
            if any(rule.expression.fullmatch(parent) for parent in parents):
                held = rule.included
            elif rule.included and rule.pattern.startswith(f"{path}/"):
                # An allowlisted pattern under the path, as under a submodule.
                held = True
        return held


def main(argv: list[str]) -> int:
    """Print the paths on standard input that the image is built from.

    Return 0 when there is one and 1 when there is none, as grep does.
    """
    context = BuildContext(Path(argv[0]).read_text(encoding="utf-8"))
    held = [path for path in map(str.strip, sys.stdin) if path and context.holds(path)]
    for path in held:
        print(path)
    return 0 if held else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or [str(REPOSITORY_ROOT / ".dockerignore")]))
