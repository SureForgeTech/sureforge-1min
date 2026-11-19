#!/usr/bin/env python3
"""Resolve a merge or rebase conflict in CHANGELOG.md and tests/test_server.py.

Both branches of this repository add a line at the top of the `## Unreleased`
section of the changelog and append their tests at the end of the test file, so
the two conflict with each other. Both sides are wanted: the result keeps the
lines of both, and the test file keeps the tests of both.

A markdown file keeps one blank line between the sides and never more, so the
entries stay one list. A python file keeps two blank lines, as the style of the
test file asks for.

Usage: resolve-conflicts.py <path> [<path> ...]
"""

import re
import sys
from pathlib import Path

CONFLICT = re.compile(
    r"^<<<<<<< [^\n]*\n(?P<ours>.*?)^=======\n(?P<theirs>.*?)^>>>>>>> [^\n]*\n",
    re.S | re.M,
)
BLANK_LINES = re.compile(r"\n{3,}")


def resolve(text: str, path: Path) -> str:
    """Keep both sides of every conflict in the text.

    Args:
        text: The text with the conflict markers in it.
        path: The path the text came from, which decides the spacing.

    Returns:
        str: The text with both sides of every conflict kept.
    """
    separator = "\n\n" if path.suffix == ".md" else "\n\n\n"
    merged = CONFLICT.sub(
        lambda m: m.group("ours").rstrip("\n")
        + separator
        + m.group("theirs").lstrip("\n"),
        text,
    )
    if path.suffix == ".md":
        merged = BLANK_LINES.sub("\n\n", merged)
    return merged


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    for name in argv[1:]:
        path = Path(name)
        text = path.read_text()
        if "<<<<<<<" not in text:
            print(f"{name}: no conflict")
            continue
        path.write_text(resolve(text, path))
        print(f"{name}: kept both sides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
