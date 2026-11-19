## Summary

The gateway read whole request bodies into memory without a limit. Flask's `MAX_CONTENT_LENGTH` was never set, and Waitress accepts a body of one gibibyte by default, so a handful of concurrent requests could exhaust the container's memory. The image limit did not help: it was only checked after the body had been parsed, and an inline base64 image was decoded before it was measured, so a 900 MB data URL was turned into bytes in memory before the 10 MB limit refused it.

A new `MAX_REQUEST_BYTES` setting (16 MB by default) now bounds the body. The body is refused with `413` in the OpenAI error shape, and an inline base64 image over the image limit is refused.

## Linked issue

Closes #14

## Changes

- `server.py`: a new `MAX_REQUEST_BYTES` setting (16 MB by default) bounds the body the gateway accepts.
- `server.py`: `request_json()` refuses a body whose declared length is over the limit before reading it, and otherwise reads the body from the raw WSGI input with a limit of its own. The framework cuts a body whose length is not declared short without a word, so the excess would otherwise go unnoticed and the request would be answered from the truncated text.
- `server.py`: `main()` passes a limit a little larger than the application's to Waitress, so the application refuses the body and the answer carries the OpenAI error shape. The server in front is a last resort; the application decides the boundary.
- `server.py`: an error handler answers an oversized body with the OpenAI error shape: status `413`, code `request_too_large`, type `invalid_request_error`.
- `server.py`: `decode_data_image()` measures the encoded text before it is decoded, so a payload far over the limit is never turned into bytes in memory. The bound covers every encoded length a payload within the limit can have, because a payload of one byte more can have the same text length, so the exact size is known after the decode. The check after the decode is kept.
- `tests/test_server.py`: thirteen regression tests (an oversized body is refused with `413` in the OpenAI error shape, a body whose length is not declared is refused, a body under the limit is still handled, an oversized inline image is refused on its exact size, a payload far over the limit is refused before it is decoded, the limit is configured on the application, and the server in front gets the larger limit).
- `README.md`: documents the new `MAX_REQUEST_BYTES` setting.
- `CHANGELOG.md`: one `[SECURITY]` line under `## Unreleased`.

## Acceptance criteria mirror

- [x] 1. A request body over the limit is refused with `413` before it is read into memory.
- [x] 2. The limit is enforced twice: by the application and by the server in front of it.
- [x] 3. An inline base64 image over the image limit is refused, and a payload far over the limit is refused before it is decoded.
- [x] 4. The refusal carries the OpenAI error shape.
- [x] 5. `ruff check .`, `ruff format --check .`, `mypy .` and `pytest` pass.

## Test plan

The repository's declared validation, run in a throwaway virtualenv outside the checkout (`python -m pip install -e ".[test]"`: ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, Python 3.12.3):

- `ruff format --check .` — 9 files already formatted
- `ruff check .` — All checks passed!
- `mypy .` — Success: no issues found in 1 source file
- `pytest` — 116 passed

The new tests were also run against the `server.py` of `main` before this change (55d83ec): eleven of them fail there and pass on this branch, so they pin the defect rather than the implementation.

Two independent reviews drove the change. The second one found that the limit did not reach a request that declares no length, that the server in front refused a body of exactly the limit and answered with plain text rather than the OpenAI shape, and that an image of one byte over the limit shared its encoded length with one exactly at the limit. Each of those now has a test.

## Changelog entry

- [SECURITY] Request bodies are bounded: a body larger than `MAX_REQUEST_BYTES` (16 MB by default) is refused with `413` before it is read, instead of being held in memory, and an inline base64 image over the image limit is refused; a payload far over the limit is refused before it is decoded at all.
