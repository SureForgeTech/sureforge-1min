# Changelog

## Unreleased

- [FEATURE] A streamed answer can carry the token usage: a request with `stream_options.include_usage` now gets a last chunk with the token counts, as the OpenAI API does. The counts are made of a word, a group of digits or a mark of punctuation each, which needs no model file and no network; the token counts of a non-streamed answer change with them, because they were counted as a quarter of the characters before.
- [FIX] The token counts are closer to what a tokeniser of a model provider produces: a character of the Chinese, Japanese or Korean writing is a token of its own, and a run of digits is counted in groups of three. Measured on 2026-11-19 against the `cl100k_base` tokeniser, built offline from the vocabulary of the `tiktoken-offline` wheel, over the samples of the pull request, the error on Chinese text falls from 76% to 9% and on Japanese text from 81% to 3%, and the estimate on English text is closer as well. The count still needs no model file and no network.

- [FIX] A request the upstream declined with 429 or 503 is sent again, with a wait that grows with every attempt, or the longer wait the upstream asks for in its Retry-After header, whether it holds a number of seconds or a date, up to the longest wait the gateway allows; a file that can be rewound is sent again whole, and a request whose file cannot be rewound is not sent again, because it would upload nothing; a request that failed in the network is not sent again either, because the upstream may have handled it.

- [SECURITY] Request bodies are bounded: a body larger than `MAX_REQUEST_BYTES` (32 MB by default) is refused with `413`, instead of being held in memory, and an inline base64 image over the image limit is refused; a payload far over the limit is refused before it is decoded at all. The budget is for the whole request, so the inline images of a request share it: two images of the image limit fit, three do not. A body of one megabyte more than the limit is refused by the server in front, with a plain-text `413`.

- [FIX] The image endpoint checks the request before the catalogue, so a request without a prompt is refused as such even while the catalogue is unavailable.
- [FIX] A failing model catalog no longer empties the model list for the whole cache lifetime: the last catalog that was fetched is served while the upstream is down, a failure is not cached as an empty catalog (a catalog that holds no models is, but one whose entries the gateway cannot read is a failure), one feature is fetched at a time instead of all of them behind one lock, and the model list, the image endpoint and the image path of the chat endpoint report an unavailable catalog (503) instead of blaming the caller's model choice (400 `model_not_supported`). How long a failed fetch is remembered is set by `CATALOG_RETRY_SECONDS`.

- [FIX] Streamed chat completions keep the upstream text byte for byte: the crawl-status filter now applies to complete lines only, whitespace is no longer stripped at a flush boundary, and the upstream response is closed on every exit path. A line in a stream ends only at a newline, so a crawl-status line preceded by a carriage return is no longer dropped, and the streaming path keeps characters that the non-streaming path rewrites. A crawl-status line is dropped whole while it is still undecided and fits in the holdback of 8192 characters; a longer line that is still undecided may be streamed as text.
- [INTERNAL] Pull requests are now welcome with a DCO sign-off (`git commit -s`): the organization contributing guide says so, and a new `dco` workflow checks the sign-off on pull requests from outside contributors.
- [INTERNAL] The licence changes from MIT to Apache-2.0, with a NOTICE file. Releases published before this change keep the MIT licence they shipped with; only new releases carry Apache-2.0.
- [INTERNAL] The copyright holder in `LICENSE` and the author in `pyproject.toml` are now SureForgeTech; the MIT licence text is unchanged.
- [INTERNAL] Declare `urllib3>=2.0,<3.0` as a direct dependency: the image-fetch transport uses the urllib3 2.x API (`requests>=2.31` accepts urllib3 2.x).
- [SECURITY] Harden the outbound host check for fetched image URLs: any address that is not publicly routable is refused (including the shared address range and IPv6 forms that carry an IPv4 address), every resolved address is checked, a failed lookup refuses the host, image fetches connect only to an address from the same lookup that was checked (TLS still verifies the URL's hostname), URLs with backslashes, whitespace or control characters or with a host the two URL parsers read differently are refused on every redirect hop, image fetches ignore proxy settings from the environment, and a response whose Content-Type is not an image is refused.
- [INTERNAL] Lint keeps the rule set ruff applied by default through 0.15 (`select = ["E4", "E7", "E9", "F"]`), so the widened `ruff<0.18` and `mypy<3.0` ranges do not fail CI on 17 new default-rule findings.
- [SECURITY] Error responses no longer echo exception text: the global handler returns
  the status's standard reason phrase and image-fetch failures a fixed message (the
  detail stays in the log), and every response carries `X-Content-Type-Options: nosniff`.
- [INTERNAL] Regenerate the fleet-core block in `AGENTS.md` to the current standard.
- [INTERNAL] Refresh the generated fleet core; preserve repository-specific instructions.
- [INTERNAL] CI hygiene: both `ci.yml` jobs have a `timeout-minutes` sized from their recorded run times, a superseded pull-request run is cancelled (push and tag runs never are), and the runner fallback is the hosted label the repository variable already holds instead of a self-hosted ARM64 pool that a public repository cannot use.

- [INTERNAL] Route CI jobs to the organization's self-hosted Linux runners
  (`LINUX_RUNNER_LABELS`) and stop persisting checkout credentials.
- [INTERNAL] Adopt the fleet agent-file standard: `AGENTS.md` with the shared core and
  repository rules, and a `CLAUDE.md` that only imports it.

## 0.1.1

- Hardened image fetch path against private-network/localhost hosts.
- Rejected invalid chat payloads (non-array `messages`, malformed message items) with
  4xx errors.
- Improved streaming response behavior to emit SSE chunks incrementally instead of
  buffering full responses.
- Guarded model catalog fetch failures so gateway endpoints return gracefully on
  upstream data errors.

## 0.1.0

- Initial public release.
- OpenAI-compatible `/v1/models`, `/v1/chat/completions`, and
  `/v1/images/generations` endpoints for 1min.ai.
- Multi-architecture GHCR image publishing for `linux/amd64` and
  `linux/arm64`.
