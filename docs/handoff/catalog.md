## Summary

A single transient failure while fetching the model catalog was cached as an **empty** catalog for the whole TTL. During that window `/v1/models` returned an empty list, and `/v1/images/generations` and the vision upload path rejected **every** model with `400 model_not_supported` — blaming the caller's model choice for what was really an upstream outage. One 30-second blip degraded three endpoints for up to five minutes.

The catalog cache now keeps the last catalog that was fetched successfully and serves it while the upstream is down, never caches a failure as an empty catalog, retries after a short backoff, and fetches one feature at a time instead of making every feature wait behind a single global lock. When the catalog is genuinely unavailable and no good copy was ever fetched, the endpoints say so with `503 catalog_unavailable`.

## Linked issue

Closes #15

## Changes

- `server.py`: replaced the `lru_cache`-and-bucket catalog cache with a per-feature cache (`_CatalogState`) that holds the last catalog fetched successfully, when it was fetched, and when the last attempt failed. The copy is `None` until one was fetched, so a catalog that was fetched and holds no models is still cached.
- `server.py`: `fetch_catalog()` no longer memoizes a failure. On an upstream error it serves the last good copy, and remembers the failure for `CATALOG_RETRY_SECONDS` so a burst of requests does not hammer the upstream. The wait is checked again after the lock is taken, so the callers that waited for a failed attempt do not each start one. It returns `None` only when the catalog is unavailable and no good copy was ever fetched.
- `server.py`: a response whose shape the gateway does not understand — a body that is not an object, or one whose `models` is not a list — is a failure, so it never replaces a good copy.
- `server.py`: the global `_catalog_mutex` is gone. Each feature has its own lock, so one slow feature no longer serialises the others. A caller that already has a copy to serve does not wait for a fetch that is running; a caller with no copy waits, so a cold start causes one upstream call, not one per request.
- `server.py`: a catalog TTL of zero or less means the catalog is served for a second, instead of for every request.
- `server.py`: `chat_models()`, `vision_model_ids()` and `image_model_ids()` now report an unavailable catalog as `None`, so the endpoints can tell "the catalog is down" from "this model is not supported".
- `server.py`: `/v1/models` and `/v1/images/generations` (and the vision upload path) answer `503 catalog_unavailable` when the catalog is unavailable, and keep `400 model_not_supported` for a model that really is not supported. The image endpoint checks the prompt before the catalog.
- `server.py`: the chat path is unchanged in behaviour — there the catalog only drives a warning, so a chat request works while the catalog is down.
- `tests/test_server.py`: twenty-two regression tests (the last good copy is served after a failure, a failure is not cached as an empty catalog, a failure is retried after a short backoff, one feature is fetched once under concurrent requests, a failing upstream is asked once for a burst of requests, a slow feature does not block another, an empty catalog is still cached, a TTL of zero still caches, a malformed response never replaces a good copy, and the model list, image endpoint and chat endpoint each behave correctly while the catalog is down).
- `README.md`: documents the new `CATALOG_RETRY_SECONDS` setting.
- `CHANGELOG.md`: one `[FIX]` line under `## Unreleased`.

## Acceptance criteria mirror

- [x] 1. A failure while fetching the catalog is not cached as an empty catalog, and the catalog recovers as soon as the upstream does.
- [x] 2. While the upstream is down, the last catalog that was fetched is still served.
- [x] 3. When no good copy exists, the endpoints report an upstream availability problem (503) instead of `400 model_not_supported`.
- [x] 4. One feature is fetched at a time; a burst of requests causes one upstream call per feature, and one slow feature does not block the others.
- [x] 5. `ruff check .`, `ruff format --check .`, `mypy .` and `pytest` pass.

## Test plan

The repository's declared validation, run in a throwaway virtualenv outside the checkout (`python -m pip install -e ".[test]"`: ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, Python 3.12.3):

- `ruff format --check .` — 9 files already formatted
- `ruff check .` — All checks passed!
- `mypy .` — Success: no issues found in 1 source file
- `pytest` — 90 passed

The new tests were also run against the pre-change `server.py` from `main` (7b759a8): twenty-one of them fail there and pass on this branch, so they pin the defect rather than the implementation.

The defect was reproduced end to end before the change: with a single upstream failure inside one TTL bucket, `POST /v1/images/generations` answered `400 model_not_supported` and stayed that way after the upstream recovered. It now answers `200` while the last good catalog is served, and `503 catalog_unavailable` when there is no good copy.

Two independent reviews drove the change, and the mutations they asked for are caught by the tests: removing the per-feature lock, removing the under-lock wait, making the TTL of zero fetch every time, and making a malformed response look like an empty catalog each break a test.

## Changelog entry

- [FIX] The image endpoint checks the request before the catalogue, so a request without a prompt is refused as such even while the catalogue is unavailable.
- [FIX] A failing model catalog no longer empties the model list for the whole cache lifetime: the last catalog that was fetched is served while the upstream is down, a failure is not cached as an empty catalog (a catalog that holds no models is), one feature is fetched at a time instead of all of them behind one lock, and the model list and image endpoints report an unavailable catalog (503) instead of blaming the caller's model choice (400 `model_not_supported`). How long a failed fetch is remembered is set by `CATALOG_RETRY_SECONDS`.
