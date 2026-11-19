# Run record — sureforge-1min

Delivery of seven improvements to the gateway, each on its own pull request, each
with an independent fresh-context review of its exact head.

## Merged

| PR | change | head reviewed | verdict | merged as |
|---|---|---|---|---|
| #42 | streamed content byte for byte, crawl lines dropped whole, upstream closed on every exit | `38c90ea2` | APPROVE | `125aa280` |
| #43 | catalogue survives an upstream outage, `503 catalog_unavailable` | `a61a641` | APPROVE WITH NITS | `55d83ec` |
| #44 | request bodies bounded (`MAX_REQUEST_BYTES`) | `b59b141` | APPROVE | `b231035` |
| #45 | a request the upstream declined with 429/503 is sent again | `61e3d2d` | APPROVE | `80e5cff` |
| #46 | a streamed answer carries its token usage | `1d36eeb` | APPROVE | `52d384a` |
| #48 | catalogue read from the names the upstream may give an identifier | `27fd8d0` | APPROVE | `920b130` |
| #47 | the characters of the Chinese, Japanese and Korean writing counted one by one | `587fe8e` | six rounds, the last head not reviewed before the owner merged it | `cb2b03b` |

## Reviewer sessions

- #42 `ses_e0f2...` (MiniMax and Kimi), #43 `ses_e11c9d9aeffeUjWxLEBpoqE13P`, #44 `ses_e0e7...`
- #45 `ses_e0ec...`, #46 `ses_e0ec...`, #47 six rounds `ses_e0ecf67e...` through `ses_e0e7dfd7...`
- #48 `ses_e0ec49296ffeiF4q70gKZRYW0v` and `ses_e0ecf49c0ffeVekvu1ZA4L5haH`

## Validation

The repository's declared validation (`ruff check . && ruff format --check . && mypy . && pytest`)
green on every head and on `main`: 167 passed at `cb2b03b`.

## Not verified

- The behaviour against the live `api.1min.ai` upstream: there is no key in this
  environment and AGENTS.md forbids calling it. PR #48 removes the two assumptions
  that the upstream could not be checked against.
- The token estimate stays an estimate: a word that no tokeniser holds in one piece
  is counted as one token. A length rule was measured and made the estimate worse.

## Artefacts

- The pull request bodies and the review records: `/workspace/.pr-bodies/*.md` and `record-*.json`
- The conflict resolver used when `main` was merged into a branch: `resolve-conflicts.py`
