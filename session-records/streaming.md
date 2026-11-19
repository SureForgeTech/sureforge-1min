## Summary

Streamed chat completions were corrupted at flush boundaries: `stream_response()` ran every flush through `clean_text()`, which ends in `.strip()`, so whitespace that ended a flush was deleted before it reached the client and one newline per flush was lost. A crawl-status line split across two flushes matched neither half of the `startswith` check and leaked, and the upstream response was never closed when the generator exited early.

The streaming path now emits complete lines only, filters crawl-status lines against those complete lines, and passes every other byte through unchanged. The non-streaming endpoint keeps `clean_text()`.

## Linked issue

Closes #12

## Changes

- `server.py`: filter crawl-status lines at line boundaries only. Added `CRAWL_MARKER` (the marker literal, now shared with `clean_text()`), `_is_crawl_line()`, `_crawl_decision()` (classifies a line that is still arriving as a crawl-status line, clear, or undecided), `_find_line_end()` and `_drop_crawl_lines()`.
- `server.py`: the buffer tracks whether it starts at a line beginning. The crawl filter only ever classifies a line whose start is a genuine line start, so a crawl marker in the middle of a line is ordinary text and the line is no longer cut off at a flush boundary.
- `server.py`: text held back while a line is undecided is capped by `CRAWL_HOLDBACK_MAX` (8192 characters), so the buffer stays bounded and a long line is streamed instead of buffered whole.
- `server.py`: the final tail is emitted verbatim instead of being stripped. The lines the buffer holds are filtered as well, which matters when a tool call was never finished and the line splitting was skipped; the last line, which has no terminator, is looked at too.
- `server.py`: the last part of a dropped crawl-status line is kept, so a tool call sharing a line with one is still delivered however the upstream splits the text.
- `server.py`: `stream_response()` returns a `_ClosingStream` wrapper that closes the upstream response on every exit path: normal completion, the tool-call short-circuit, a client disconnect, an exception inside the generator, and a stream closed before the first chunk is read.
- `tests/test_server.py`: nine regression tests (whitespace fidelity across a flush boundary, a crawl-status line split across deltas, a crawl marker in mid-line text, a crawl-status line after a long run of spaces, a long crawl-status line, a crawl-status line at the end of an answer without a newline, a tool call sharing a line with a crawl-status line, the preserved trailing newline, incremental delivery of a long line, and upstream closure on every exit path); the two existing streaming fakes gained the `close()` method the new contract requires.
- `CHANGELOG.md`: one `[FIX]` line under `## Unreleased`.

## Acceptance criteria mirror

- [x] 1. A streamed chat completion whose upstream text is longer than the flush threshold reaches the client byte for byte, including the spaces and newlines that fall on a flush boundary.
- [x] 2. The crawl-status filter runs on complete lines only: a crawl-status line split across deltas is removed in full and never leaks partially, and no other byte of the text is rewritten.
- [x] 3. The final streamed tail keeps its trailing whitespace; the non-streaming endpoint keeps its existing whole-text cleaning.
- [x] 4. The upstream response is closed on every exit path of the streaming generator, including the tool-call short-circuit and a client disconnect.
- [x] 5. `ruff check .`, `ruff format --check .`, `mypy .` and `pytest` pass.

## Test plan

The repository's declared validation, run in a throwaway virtualenv outside the checkout (`python -m pip install -e ".[test]"`: ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, Python 3.12.3):

- `ruff format --check .` — 9 files already formatted
- `ruff check .` — All checks passed!
- `mypy .` — Success: no issues found in 1 source file
- `pytest` — 76 passed

The nine new tests were also run against the pre-change `server.py` from `main` (7b759a8): eight of them fail there and pass on this branch, so they pin the defect rather than the implementation. The ninth pins the tool-call fix and fails against the intermediate head 4f77f93.

Two independent fresh-context reviews, by models of different families, covered this head and drove three rounds of fixes: a line whose middle held the crawl marker was truncated and the rest of it dropped; a tool call on a line that was dropped was lost with it; and a crawl-status line at the end of an answer without a newline reached the client. Each fix has a regression test that fails against the pre-change code.

A differential fuzz over 24000 randomised trials and a separate 1000-case seeded chunk-boundary sweep, both against independently written models of the documented rule, found no mismatch.

Two bounds are stated in the changelog rather than hidden: a line in a stream ends only at a newline, so a crawl-status line preceded by a carriage return is no longer dropped and the streaming path keeps characters the non-streaming path rewrites; and a crawl-status line is dropped whole while it is still undecided and fits in the holdback of 8192 characters, while a longer line that is still undecided may be streamed as text.

## Changelog entry

- [FIX] Streamed chat completions keep the upstream text byte for byte: the crawl-status filter now applies to complete lines only, whitespace is no longer stripped at a flush boundary, and the upstream response is closed on every exit path. A line in a stream ends only at a newline, so a crawl-status line preceded by a carriage return is no longer dropped, and the streaming path keeps characters that the non-streaming path rewrites. A crawl-status line is dropped whole while it is still undecided and fits in the holdback of 8192 characters; a longer line that is still undecided may be streamed as text.
