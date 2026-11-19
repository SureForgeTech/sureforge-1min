## Summary

The gateway passed a 429 or 503 from the upstream straight to the caller, although such a response means the upstream did not handle the request — so sending it again cannot bill the caller twice. A request that failed in the network was left alone too, because there the upstream may have handled it.

The chat, streaming, image and asset requests are now sent again when the upstream declines them, with a wait that grows with every attempt and that follows the upstream's `Retry-After` header. The number of attempts is bounded.

## Linked issue

Relates to #15

## Changes

- `server.py`: a new `post_upstream()` sends a request to the upstream and sends it again when the upstream declines it with 429 or 503. The wait grows with every attempt and follows the upstream's `Retry-After` header, up to a cap.
- `server.py`: the chat, streaming chat, image generation and asset upload requests go through it. A request that failed in the network is not retried, because the upstream may have handled it and sending it again could bill the caller twice.
- `server.py`: a streaming request is retried only before its first byte is read, because the reply is checked for its status before the body is streamed.
- `server.py`: the log line names the endpoint by a short label and the status code. It carries no key, no header and no request body.
- `tests/test_server.py`: twenty-eight regression tests (a declined request is sent again, a rate-limited request waits for its `Retry-After`, a rejected request is not sent again, a request that keeps being declined gives up after a bounded number of attempts, a request that failed in the network is not sent again, the wait grows with every attempt, the wait is capped, a `Retry-After` that cannot be read does not break the request, a file that is sent again arrives whole in every shape the request library accepts, a file that cannot be rewound is not sent again, and the retry settings are bounded).
- `CHANGELOG.md`: one `[FIX]` line under `## Unreleased`.

## Notes and limits

- The wait happens in the request thread, so a burst of requests that the upstream declines holds the workers for as long as the waits are. The waits are bounded: every wait is at most `UPSTREAM_RETRY_MAX_SECONDS`, and the number of attempts is at most `MAX_UPSTREAM_RETRIES`.
- A 503 usually means the upstream did not handle the request. A 503 from a load balancer that had already passed the request on would mean it was handled, and sending it again would bill the caller twice. That is the premise of the change, and it is why only 429 and 503 are retried, and never a timeout or a broken connection.

## Acceptance criteria mirror

- [x] 1. A request the upstream declined with 429 or 503 is sent again, and the caller sees the eventual answer.
- [x] 2. The wait before the next attempt grows, and follows a `Retry-After` header when the upstream sends one.
- [x] 3. The number of attempts is bounded.
- [x] 4. A request that failed in the network is not sent again.
- [x] 5. `ruff check .`, `ruff format --check .`, `mypy .` and `pytest` pass.

## Test plan

The repository's declared validation, run in a throwaway virtualenv outside the checkout (`python -m pip install -e ".[test]"`: ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, Python 3.12.3):

- `ruff format --check .` — 9 files already formatted
- `ruff check .` — All checks passed!
- `mypy .` — Success: no issues found in 1 source file
- `pytest` — 144 passed

The new tests were also run against the `server.py` of `main` before this change (b231035). They use the helpers this change adds, so the suite does not run against that code at all; the count of twenty was measured before the tests pinned the retry settings themselves.

Two independent reviews drove the change. The first found that a retried asset upload sent a zero-byte image, because the request library reads the file while it builds the body and the pointer was already at its end, and that a `Retry-After` with a negative or a not-a-number value made the wait raise and turned the request into a 500. The second found that the wait grew without a limit, so the largest settings could hold a request for seventeen hours, and that a file which cannot be rewound was sent again empty. Each of those now has a test, and the author re-ran the mutations the reviews used.

## Changelog entry

- [FIX] A request the upstream declined with 429 or 503 is sent again, with a wait that grows with every attempt and follows the upstream's Retry-After header; a request that failed in the network is not sent again, because the upstream may have handled it.
