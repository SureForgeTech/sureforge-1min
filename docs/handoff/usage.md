## Summary

A streamed answer carried no token counts at all, so a client that shows the tokens of a conversation had nothing to show for a streamed request. A request with `stream_options.include_usage` now gets a last chunk with the counts, in the OpenAI shape, naming the same completion as the answer it reports on.

The counts themselves are made of a word, a number or a mark of punctuation each. The `tiktoken` branch that was in the code was never taken — tiktoken is not a dependency — and it downloads its vocabulary on first use, so it could not have worked in a container without network access. The count is now the same in every deployment and needs no model file.

## Linked issue

Relates to #15

## Changes

- `server.py`: a streamed answer carries a usage chunk when the caller asked for it with `stream_options.include_usage`. The chunk has the OpenAI shape: an empty `choices` list and a `usage` object with the prompt, completion and total token counts, sent just before the last chunk.
- `server.py`: the usage chunk takes the id and the model from the chunks of the answer, so it names the completion it reports on.
- `server.py`: the token count is now a word, a number or a mark of punctuation each, counted without a model file and without a network call. The `tiktoken` branch, which could never run because tiktoken is not a dependency and would fail at import time when it could not download its vocabulary, is gone. The token counts of a **non-streamed** answer change with it, because they were counted as a quarter of the characters before.
- `server.py`: the completion tokens of a stream are counted while the answer arrives, so the whole answer does not have to be kept in memory. A word that was cut in half by the end of a chunk is counted with the next chunk, so the count does not depend on where the upstream split the text.
- `server.py`: the stream options are read defensively: a `stream_options` that is not an object, or an `include_usage` that is not exactly `true`, is ignored.
- `tests/test_server.py`: twelve regression tests (a stream that was asked for carries the token counts, a stream that was not asked for carries none, the usage chunk names the same completion as the answer and carries the time of that answer, the options of the wrong type are ignored, the count counts words and marks, the count is never zero, the count does not depend on where the text was split, the carried text stays bounded, and the wrapper closes the chunks it wraps).
- `CHANGELOG.md`: one `[FEATURE]` line under `## Unreleased`.

## Acceptance criteria mirror

- [x] 1. A streamed request with `stream_options.include_usage` gets a last chunk with the token counts, in the OpenAI shape.
- [x] 2. A streamed request without it gets no usage chunk.
- [x] 3. The token count needs no model file and no network, so it is the same in every deployment.
- [x] 4. The count of a streamed answer does not depend on where the upstream split the text.
- [x] 5. `ruff check .`, `ruff format --check .`, `mypy .` and `pytest` pass.

## Depends on #42

This change **must land after #42** ("keep streamed content byte for byte"). The token count is made of the text the client receives, and before #42 the streamed text lost whitespace at every flush boundary, so the count depended on where the upstream split the answer: the same 999-character answer reported between 40 and 199 tokens depending on the splitting.

With #42 merged, the same answer reports the same count for every splitting. The author verified that by merging the two heads in a scratch tree: `ruff format --check .`, `ruff check .`, `mypy .` and 85 tests are green, and the count is stable at 222 tokens for one piece, 20-character pieces, 7-character pieces and one character at a time. The end-to-end test that pins the count across two different upstream splittings is added when this pull request is opened, after #42 has landed.

## Test plan

The repository's declared validation, run in a throwaway virtualenv outside the checkout (`python -m pip install -e ".[test]"`: ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, Python 3.12.3):

- `ruff format --check .` — 9 files already formatted
- `ruff check .` — All checks passed!
- `mypy .` — Success: no issues found in 1 source file
- `pytest` — 155 passed

The new tests were also run against the `server.py` of `main` before this change (80e5cff): eight of them fail there and pass on this branch, so they pin the defect rather than the implementation.

Two independent reviews drove the change. The first found that the usage chunk carried an id of its own and an empty model, so it did not belong to the completion it reported on, and that the count of a streamed answer depended on where the upstream split the text. The second found a shape of event-stream chunk the reader did not handle. Each of those now has a test.

## Changelog entry

- [FEATURE] A streamed answer can carry the token usage: a request with `stream_options.include_usage` now gets a last chunk with the token counts, as the OpenAI API does. The counts are made of a word, a number or a mark of punctuation each, which needs no model file and no network; the token counts of a non-streamed answer change with them, because they were counted as a quarter of the characters before.
