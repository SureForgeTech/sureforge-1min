# Handoff — sureforge-1min gateway improvements

## Goal

Deliver the most valuable improvements to the `sureforge-1min` gateway (an
OpenAI-compatible gateway in front of the 1min.ai API), each tested and each
merged after an independent fresh-context review of its exact head.

## What is done

Seven pull requests are merged into `main` (`cb2b03b`), 0 open pull requests,
0 open issues:

| PR | change | head reviewed | verdict | merged as |
|---|---|---|---|---|
| #42 | streamed content byte for byte, crawl-status lines dropped whole, upstream response closed on every exit | `38c90ea2` | APPROVE | `125aa280` |
| #43 | model catalogue survives an upstream outage; `503 catalog_unavailable` | `a61a641` | APPROVE WITH NITS | `55d83ec` |
| #44 | request bodies bounded by `MAX_REQUEST_BYTES` (32 MB), `413` in the OpenAI error shape | `b59b141` | APPROVE | `b231035` |
| #45 | a request the upstream declined with 429 or 503 is sent again, with a bounded backoff | `61e3d2d` | APPROVE | `80e5cff` |
| #46 | a streamed answer carries its token usage (`stream_options.include_usage`) | `1d36eeb` | APPROVE | `52d384a` |
| #48 | the catalogue is read from the names the upstream may give an identifier | `27fd8d0` | APPROVE | `920b130` |
| #47 | the characters of the Chinese, Japanese and Korean writing are counted one by one | `587fe8e` | six rounds; the last head was not reviewed before the owner merged it | `cb2b03b` |

Tested: the repository's declared validation
(`ruff check . && ruff format --check . && mypy . && pytest`) passes on every
head and on `main` — 167 tests at `cb2b03b`. GitHub `test`, `sign-off`,
`publish-image`, CodeQL and the analyzers are green on the merge commit.

The two caveats reported at the end of the earlier session were addressed:

- The token estimate was 76–81% off on Chinese and Japanese text. It now
  counts each character of those writings on its own and counts digits in
  groups of up to three, of every script. Measured against `cl100k_base`
  (built offline from the vocabulary of the `tiktoken-offline` wheel), the
  error on Chinese text is −9.1% and on Japanese text +3.2%; English text got
  closer as well. The samples and the counts are in the README, so the figure
  is checkable from the repository.
- The catalogue no longer depends on one identifier key name, and a single
  entry the gateway cannot read no longer takes the whole catalogue down.

## What is half-done

The review of the exact head `587fe8e8b9462e174ea01998a18075d6ba0da53b` of
PR #47 was never run. `deffenda` merged that pull request at 2026-11-19
03:22 UTC with the squash commit `cb2b03b`. Six rounds of review drove the
change and every finding of every round was fixed with a regression test that
fails without it, but the head that was merged was not itself reviewed.

The state is recorded in the pull request itself: the comment with the table
of the six rounds, and the follow-up comment that names the merge and says
plainly that the review of that head was not run.

There is no unfinished edit in any checkout. `git status` is clean in
`/workspace/sureforge-1min` and in the worktree of the merged branch.

## Exact next step

Nothing is required for the delivery. If the merged head of PR #47 is to be
reviewed after the fact, the review is to be run against
`587fe8e8b9462e174ea01998a18075d6ba0da53b` in
`/workspace/sureforge-1min-wt-token-count`, and its verdict posted on PR #47.

## Branch, commit, pull request

- Branch of this session's work: `fix/token-count-accuracy`, head
  `587fe8e8b9462e174ea01998a18075d6ba0da53b`, pull request **#47**, merged
  (squash) as `cb2b03b`.
- The other branches of the session — `feature/catalog-resilience`,
  `feature/request-body-cap`, `feature/upstream-retry`, `feature/stream-usage`,
  `fix/catalog-entry-identifier`,
  `fix/streaming-whitespace-fidelity-attributed` — were merged and their
  remote copies deleted by the forge.
- This session's record branch: `wip/sureforge-gateway-improvements`
  (see below).

## Decisions made, and why

- **One bounded task per pull request, squash merge, DCO sign-off.** The
  repository's own rules and the organization ruleset require it; the ruleset
  allows only the squash method.
- **Only 429 and 503 are re-sent.** Such a response means the upstream did not
  handle the request, so sending it again cannot bill the caller twice. A
  failure in the network is never re-sent, because there the upstream may
  have handled it.
- **The last good catalogue is served while the upstream is down.** A failure
  is never cached as an empty catalogue, which was the defect that emptied
  `/v1/models` for the length of a cache lifetime.
- **The token count needs no model file and no network.** The `tiktoken` branch
  that was in the code was never taken — `tiktoken` is not a declared
  dependency and downloads its vocabulary on first use.
- **The `main` branch was merged into each feature branch before its merge.**
  A squash merge drops the branch commits from `main`'s history, so the
  following pull request conflicted with `main`. Merging `main` into the
  branch puts `main` into the branch's history and the pull request then
  merges without a conflict. Those merge commits were reviewed as well.
- **The token estimate stays an estimate.** A rule of the kind "a word longer
  than N letters is counted as a quarter of its letters" was measured against
  the same tokeniser and made the estimate worse on this repository's own
  files, so it was left out.

## Blockers

None open. Two things remain unverified and cannot be closed from this
environment:

- The behaviour against the live `api.1min.ai` upstream. There is no 1min.ai
  key in this environment, and `AGENTS.md` forbids calling `api.1min.ai`
  without being asked. Pull request #48 removes the two assumptions that the
  caveat exposed, but it cannot make the gateway right about a response shape
  it has never seen.
- The exact figures of the measurement in the README and the changelog were
  reproduced independently by a reviewer against the offline `cl100k_base`
  vocabulary (all 21 numbers); the `cl100k_base` file itself cannot be
  downloaded from this sandbox (the proxy answers `403 Forbidden` for
  `https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken`).

## Files that matter outside the repository

The session's records live outside the repository, in `/workspace/.pr-bodies/`,
and are copied into this branch under `docs/handoff/`, with their names, so
that a rebuild of the sandbox keeps them:

- `SESSION.md` — the run record: every pull request, head, verdict, reviewer
  session and the validation.
- `streaming.md`, `catalog.md`, `bodycap.md`, `retry.md`, `usage.md`,
  `catalog-id.md`, `token-count.md` — the pull request bodies.
- `record-42.json` … `record-48.json`, `record-47.json`,
  `record-47-merged.json` — the review records posted on the pull requests.
- `comment-43.json`, `comment-44.json`, `comment-45.json` — the earlier review
  comments.
- `resolve-conflicts.py` — the resolver used when `main` was merged into a
  branch; it keeps both sides of a conflict in `CHANGELOG.md` and in
  `tests/test_server.py`.

Three files in `/workspace/.pr-bodies/` — `jira-features.md`,
`jira-listing-honesty.md`, `jira-task1.md` — belong to a different session
(the Jira MCP project) and are deliberately not copied here.

## Ignored and untracked files

`git status --ignored --short` in `/workspace/sureforge-1min` lists only
caches, all of them ignored and none of them needed:

```
.mypy_cache/
.pytest_cache/
.ruff_cache/
__pycache__/
one_min_ai_gateway.egg-info/
tests/__pycache__/
```

The worktree `/workspace/sureforge-1min-wt-token-count` lists the same caches
without `one_min_ai_gateway.egg-info/`. The virtualenv
`/workspace/.venvs/sureforge-1min` (ruff 0.17.5, mypy 2.4.1, pytest 9.2.0)
holds the pinned tools; `bin/setup` recreates it, and it is outside the
repository.
