# one-min-ai-gateway

OpenAI-compatible gateway for [1min.ai](https://1min.ai/).

1min.ai exposes a feature-oriented API. This gateway provides a small
OpenAI-compatible surface for clients that expect `/v1/models`,
`/v1/chat/completions`, and `/v1/images/generations`.

## Run

Use the published container image:

```sh
docker run --rm -p 5001:5001 ghcr.io/abd-enterprises/one-min-ai-gateway:latest
```

Or build locally with Docker Compose:

```sh
docker compose up -d --build
```

The gateway listens at:

```text
http://localhost:5001/v1
```

Clients pass the 1min.ai API key as an OpenAI-style bearer token. The
gateway does not store API keys.

Useful environment variables:

- `PORT`: listen port inside the container, default `5001`
- `HOST`: listen host, default `0.0.0.0`
- `CATALOG_TTL_SECONDS`: model catalog cache TTL in seconds, default `300`
- `MAX_UPSTREAM_RETRIES`: how many times a request the upstream declined with 429 or 503 is sent again, default `2`, at most `10`
- `UPSTREAM_RETRY_BACKOFF_SECONDS`: the wait before the first retry, which doubles with every attempt unless the upstream asks for a longer one, default `0.25`, at most `60`
- `UPSTREAM_RETRY_MAX_SECONDS`: the longest wait any attempt may take, default `5`, at most `60`. A `Retry-After` header of the upstream never shortens the wait and never makes it longer than this
- `CATALOG_RETRY_SECONDS`: how long a failed model catalog fetch is remembered before the upstream is asked again, default `10`; a value of zero or less means one second
- When the model catalog cannot be fetched and no earlier copy was kept, `/v1/models`, `/v1/images/generations` and the image path of `/v1/chat/completions` answer `503` with the code `catalog_unavailable`. A catalogue the gateway cannot read -- one whose entries are not objects, or of which not one entry carries an identifier -- is a failure too, so the last catalogue that was read is served instead. The identifier of an entry is read from `id`, `modelId` or `model`, in that order; a name the upstream gave a model for display is not one of them
- `MAX_IMAGE_BYTES`: maximum image upload/fetch payload in bytes, default `10485760` (10 MB)
- `MAX_REQUEST_BYTES`: the budget for a whole request in bytes, default `33554432` (32 MB); a larger body is refused with `413`. The budget holds the inline images of a request together: two images of the image limit need about 27 MB and fit, three need about 40 MB and are refused. A body of one megabyte more than the budget is refused by the server in front of the gateway, with a plain-text `413`, because it cannot answer with the error shape of the gateway. A body at the budget is held about four times over while it is read, parsed and forwarded, so with the default eight workers a busy gateway can hold around a gigabyte of bodies
- `CORS_ALLOW_ORIGIN`: CORS origin value, default `*`
- `DEFAULT_CHAT_MODEL`: default fallback chat model, default `gpt-4o`
- `DEFAULT_IMAGE_MODEL`: default fallback image generator model, default `black-forest-labs/flux-schnell`
- `ONE_MIN_API_BASE_URL`: upstream 1min.ai API base endpoint, default `https://api.1min.ai`
- `GATEWAY_VERIFY_SSL`: enforce SSL/TLS verification on upstream HTTP calls, default `true` (set to `false` only in local test environments)
- `WAITRESS_THREADS`: internal Waitress server worker thread concurrency, default `8`
- `LOG_LEVEL`: console log severity filter (`DEBUG`, `INFO`, `WARNING`, `ERROR`), default `INFO`
- `GATEWAY_POOL_SIZE`: maximum connection pool size inside HTTP persistent adapters, default `20`
- `GATEWAY_POOL_CONNECTIONS`: persistent pool connections count inside HTTP persistent adapters, default `20`
- `PROPAGATE_HEADERS`: comma-separated list of HTTP request headers to forward from incoming clients to upstream 1min.ai (e.g. `X-Gateway-Trace,X-Client-Id`), default `""`

## Plain Python

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
python server.py
```

## OpenCode

Example provider config:

```jsonc
{
  "provider": {
    "1min-ai": {
      "name": "1min.ai Gateway",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://localhost:5001/v1"
      },
      "models": {
        "gemini-2.5-flash": {
          "name": "Gemini 2.5 Flash"
        }
      }
    }
  }
}
```

Store the credential with OpenCode or pass it as the client API key.

## Endpoints

- `GET /healthz`
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`

## Token counts

The token counts the gateway reports in `usage` are an estimate, not the count
of a model provider's tokeniser. The estimate needs no model file and no
network, so it is the same in every deployment: a word is a token, a run of
digits is counted in groups of up to three, a mark of punctuation is a token,
and a character of the Chinese, Japanese or Korean writing is counted on its
own.

The estimate was measured on 2026-11-18 against the `cl100k_base` tokeniser,
built offline from the vocabulary of the `tiktoken-offline` wheel, over the
samples below. The table gives the count of the tokeniser, the count of the
gateway before the change and the count of the gateway now. The last four
rows are the files of the repository as they were before the change.

| sample | `cl100k_base` | before | now |
|---|---|---|---|
| `你好世界，今天天气很好，我们一起去公园散步吧。这是一个测试。` | 33 | 8 | 30 |
| `こんにちは世界、今日はいい天気ですね。一起去公園に行きましょう。` | 31 | 6 | 32 |
| `안녕하세요 세계, 오늘 날씨가 좋네요. 같이 공원에 가요.` | 34 | 11 | 25 |
| `README.md` | 1301 | 1264 | 1297 |
| `CHANGELOG.md` | 1397 | 1357 | 1375 |
| `server.py` | 18674 | 17368 | 17488 |
| `tests/test_server.py` | 23235 | 22865 | 23126 |

A word that no tokeniser holds in one piece is counted as one token, while a
real tokeniser splits it into several. No rule of that kind can be had without
the vocabulary of the model, so the count stays an estimate.

## Tool Calling

1min.ai models do not consistently return native OpenAI `tool_calls`.
The gateway includes a best-effort compatibility shim for common
tool-call-shaped text patterns and converts those into OpenAI-compatible
`tool_calls`.

This is intentionally conservative. It should not be treated as a
guarantee of native tool calling support from every model.

## Security

- API keys are read from request `Authorization: Bearer ...` headers.
- API keys are not written to disk.
- Prompts and keys are not logged by default.

## Image Publishing

Pushes to `main` publish `ghcr.io/abd-enterprises/one-min-ai-gateway:latest`
and a `sha-<commit>` tag; pushing a `v*` tag also publishes `X.Y.Z` and `X.Y`
tags. Images are built for `linux/amd64` and `linux/arm64`.

The image verifies build and runtime dependency hashes from
`requirements-container.txt`, then builds the local package offline.
To refresh the lock after a dependency change, run from the repository root:

```sh
python3.12 -c 'import tomllib; print(*tomllib.load(open("pyproject.toml", "rb"))["build-system"]["requires"], sep="\n")' | \
  uv pip compile pyproject.toml - --generate-hashes --python-version 3.12 --only-binary :all: \
    --no-header --default-index https://pypi.org/simple -o requirements-container.txt
```
