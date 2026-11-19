## Summary

The gateway reads the model catalogue from the upstream and uses it to answer `/v1/models` and to tell whether the model a caller asked for is one the upstream serves. Two things about that reading were decided against the upstream itself and cannot be checked from here, because the upstream may not be called.

The first is the name an entry carries its identifier under. Only `id` and `modelId` were read. If the upstream used another name, every entry would have looked unreadable, and the gateway would have answered `503 catalog_unavailable` for everything until the cache expired.

The second is what one unreadable entry does. One entry the gateway could not read made the whole catalogue a failure, so a single odd entry took `/v1/models` and the image endpoint down for the length of the cache lifetime.

Both are loosened here. The identifier is read from `id`, `modelId` or `model`, in that order, so a rename upstream does not take the catalogue down. An entry that carries none of them is left out of the catalogue. A catalogue of which not one entry can be read is still a failure, so the last catalogue that was read is served instead, and the endpoints still answer `503 catalog_unavailable` rather than blaming the caller's model.

## Changes

- `server.py`: the identifier of an entry is read from `id`, `modelId` or `model`, in that order. A name the upstream gave a model for display is not one of them, because a model the caller asked for must not be looked for under it.
- `server.py`: an entry the gateway cannot read is left out of the catalogue instead of making the whole catalogue a failure.
- `server.py`: a catalogue of which not one entry can be read is still a failure, so the last catalogue that was read is served instead.
- `tests/test_server.py`: three regression tests (a catalogue that cannot be read at all never replaces a good one, an entry that cannot be read is left out of the catalogue, and the identifier is read from the names the upstream may give it), and one that holds that a display name is not read as an identifier.
- `README.md`: says which names the identifier is read from.
- `CHANGELOG.md`: one `[FIX]` line under `## Unreleased`.

## Acceptance criteria mirror

- [x] 1. The identifier of an entry is read from `id`, `modelId` or `model`.
- [x] 2. An entry the gateway cannot read is left out of the catalogue; the catalogue is still used.
- [x] 3. A catalogue of which not one entry can be read is a failure, so the last catalogue that was read is served and the endpoints answer `503 catalog_unavailable`.
- [x] 4. A display name is not read as an identifier.
- [x] 5. `ruff check .`, `ruff format --check .`, `mypy .` and `pytest` pass.

## Test plan

The repository's declared validation, in the pinned virtualenv (ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, Python 3.12.3):

- `ruff format --check .` — 9 files already formatted
- `ruff check .` — All checks passed!
- `mypy .` — Success: no issues found in 1 source file
- `pytest` — 159 passed

The three behaviours were checked against copies of the tree with each of them removed, and each time a test failed:

| removed | result |
|---|---|
| the rule that a catalogue the gateway cannot read at all is a failure | 1 failed, 158 passed |
| the `model` name of the identifier | 2 failed, 157 passed |
| the rule that one unreadable entry is left out | 1 failed, 158 passed |

## What is still not verified

The shape of the answer the upstream gives and the name it gives the identifier of a model cannot be checked from here, because the upstream may not be called without being asked. This change makes the gateway survive both a rename of the identifier and an entry it cannot read; it cannot make the gateway right about a shape it has never seen.
