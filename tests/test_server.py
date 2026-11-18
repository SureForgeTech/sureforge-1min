import base64
import io
import json
import threading
import time
from typing import Any, Generator
import pytest
import server


# 18. Consolidate Mock HTTP Responses in Tests
class FakeHTTPResponse:
    """Unified HTTP Response mock for requests.Session calls."""

    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise server.requests.HTTPError(f"HTTP Error {self.status_code}")

    def close(self) -> None:
        pass

    def iter_content(self, chunk_size: int = 1024) -> Generator[bytes, None, None]:
        yield self.text.encode("utf-8")


def test_models_accepts_model_id(monkeypatch):
    monkeypatch.setattr(
        server,
        "chat_models",
        lambda: [{"modelId": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"}],
    )

    client = server.app.test_client()
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json["data"][0]["id"] == "gemini-2.5-flash"


def test_missing_bearer_token_returns_401():
    client = server.app.test_client()

    response = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}
    )

    assert response.status_code == 401
    assert response.json["error"]["code"] == "invalid_api_key"


def test_healthz_returns_version():
    client = server.app.test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json["ok"] is True
    assert response.json["version"]


def test_invalid_json_returns_openai_error():
    client = server.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        },
        data="{",
    )

    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_json"


def test_build_payload_rejects_non_array_messages():
    client = server.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={"messages": "bad"},
    )

    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_request_error"


def test_build_payload_rejects_invalid_message_item():
    client = server.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={"messages": ["bad"]},
    )

    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_request_error"


def test_parse_tool_call_tag():
    tool_call = server.parse_tool_text(
        '<tool_call>{"name":"glob","arguments":{"pattern":"*"}}</tool_call>',
        [{"function": {"name": "glob"}}],
    )

    assert tool_call == {"name": "glob", "arguments": {"pattern": "*"}}


def test_parse_tool_code_tag():
    tool_call = server.parse_tool_text(
        '<tool_code>{"name":"bash","arguments":{"command":"pwd"}}</tool_code>',
        [{"function": {"name": "bash"}}],
    )

    assert tool_call == {"name": "bash", "arguments": {"command": "pwd"}}


def test_clean_text_removes_fake_crawl_lines():
    assert server.clean_text("🌐 Crawling site https://example.com\nok") == "ok"


def test_remote_image_rejects_non_http_url():
    with server.app.test_request_context():
        result = server.fetch_remote_image("file:///etc/passwd")

    assert isinstance(result, tuple)
    response, status = result
    assert status == 400
    assert response.json["error"]["code"] == "invalid_image_url"


def test_remote_image_fetch_failure_returns_openai_error(monkeypatch):
    def fail_fetch(*args, **kwargs):
        raise server.requests.Timeout("slow")

    monkeypatch.setattr(server.requests.Session, "get", fail_fetch)

    with server.app.test_request_context():
        result = server.fetch_remote_image("https://example.com/image.png")

    assert isinstance(result, tuple)
    response, status = result
    assert status == 400
    assert response.json["error"]["code"] == "invalid_image_url"


def test_data_image_rejects_invalid_base64():
    with server.app.test_request_context():
        result = server.decode_data_image("data:image/png;base64,not-valid")

    assert isinstance(result, tuple)
    response, status = result
    assert status == 400
    assert response.json["error"]["code"] == "invalid_image"


def test_extract_result_text_handles_unexpected_upstream_shape():
    assert server.extract_result_text({"unexpected": True}) is None


def test_response_json_rejects_non_object():
    class FakeResponse:
        def json(self):
            return ["not", "an", "object"]

    assert server.response_json(FakeResponse()) is None


def test_non_stream_chat_translates_tool_call(monkeypatch):
    monkeypatch.setattr(server, "upload_images", lambda api_key, model, image_urls: [])
    monkeypatch.setattr(
        server.requests.Session,
        "post",
        lambda *args, **kwargs: FakeHTTPResponse(
            status_code=200,
            text='{"aiRecord": {"aiRecordDetail": {"resultObject": ["<tool_call>{\\"name\\":\\"glob\\",\\"arguments\\":{\\"pattern\\":\\"*\\"}}</tool_call>"]}}}',
        ),
    )

    client = server.app.test_client()
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer test-key"},
        json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [{"function": {"name": "glob", "parameters": {}}}],
        },
    )

    assert response.status_code == 200
    choice = response.json["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "glob"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {
        "pattern": "*"
    }


def test_stream_response_returns_tool_call_chunks(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def iter_lines(self, decode_unicode=False):
            return [
                b"event: content",
                b'data: {"delta":{"content":"<tool_call>{\\"name\\":\\"glob\\",\\"arguments\\":{\\"pattern\\":\\"*\\"}}</tool_call>"}}',
                b"",
                b"data: [DONE]",
            ]

    output = list(
        server.stream_response(
            FakeResponse(),
            {"tools": [{"function": {"name": "glob"}}]},
            "gemini-2.5-flash",
        )
    )

    assert any("tool_calls" in chunk for chunk in output)
    assert output[-1] == "data: [DONE]\n\n"


def test_fetch_remote_image_rejects_private_host(monkeypatch):
    monkeypatch.setattr(server, "is_disallowed_host", lambda hostname: True)
    with server.app.test_request_context():
        result = server.fetch_remote_image("https://127.0.0.1/image.png")

    assert isinstance(result, tuple)
    response, status = result
    assert status == 400
    assert response.json["error"]["code"] == "invalid_image_url"


def test_fetch_remote_image_rejects_disallowed_dns(monkeypatch):
    def fake_getaddrinfo(hostname, *_args, **_kwargs):
        return [(0, 0, 0, "", ("10.0.0.1", 0))]

    def fail_request(*args, **kwargs):
        raise AssertionError("request should not happen")

    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(server.requests.Session, "get", fail_request)

    with server.app.test_request_context():
        result = server.fetch_remote_image("https://private-host.internal/image.png")

    assert isinstance(result, tuple)
    response, status = result
    assert status == 400
    assert response.json["error"]["code"] == "invalid_image_url"


def test_fetch_remote_image_allows_public_host_when_dns_safe(monkeypatch):
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda hostname, *_args, **_kwargs: [(0, 0, 0, "", ("8.8.8.8", 0))],
    )
    monkeypatch.setattr(
        server.requests.Session,
        "get",
        lambda *_, **__: FakeHTTPResponse(
            status_code=200, text="abcde", headers={"Content-Length": "5"}
        ),
    )

    with server.app.test_request_context():
        result = server.fetch_remote_image("https://cdn.example.org/image.png")

    assert not isinstance(result, tuple)
    assert result.read() == b"abcde"


def test_global_options_cors_preflight():
    """Verify that OPTIONS CORS preflight requests receive consistent 204 CORS responses."""
    client = server.app.test_client()

    for path in [
        "/v1/chat/completions",
        "/v1/images/generations",
        "/some-random-route",
    ]:
        response = client.options(path)
        assert response.status_code == 204
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in response.headers["Access-Control-Allow-Headers"]
        assert "Authorization" in response.headers["Access-Control-Allow-Headers"]


def test_stream_response_large_tool_call_no_truncation():
    """Verify that a tool call exceeding the old 200-character truncation threshold is fully parsed."""
    long_argument_value = "A" * 300
    long_tool_call_text = f'<tool_call>{{"name":"glob","arguments":{{"pattern":"{long_argument_value}"}}}}</tool_call>'

    class FakeResponse:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def iter_lines(self, decode_unicode=False):
            return [
                b"event: content",
                f"data: {json.dumps({'delta': {'content': long_tool_call_text[:100]}})}".encode(
                    "utf-8"
                ),
                f"data: {json.dumps({'delta': {'content': long_tool_call_text[100:200]}})}".encode(
                    "utf-8"
                ),
                f"data: {json.dumps({'delta': {'content': long_tool_call_text[200:]}})}".encode(
                    "utf-8"
                ),
                b"",
                b"data: [DONE]",
            ]

    output = list(
        server.stream_response(
            FakeResponse(),
            {"tools": [{"function": {"name": "glob"}}]},
            "gemini-2.5-flash",
        )
    )

    tool_call_events = [chunk for chunk in output if "tool_calls" in chunk]
    assert len(tool_call_events) > 0
    assert long_argument_value in "".join(tool_call_events)
    assert output[-1] == "data: [DONE]\n\n"


def test_global_json_error_handler():
    """Verify that unhandled exceptions are caught globally and returned in standard OpenAI JSON format."""
    client = server.app.test_client()

    def crash_models():
        raise Exception("Database failure")

    original_chat_models = server.chat_models
    try:
        server.chat_models = crash_models
        response = client.get("/v1/models")
        assert response.status_code == 500
        assert response.json["error"]["type"] == "server_error"
        # The exception text is logged, not returned: it can carry internals.
        assert response.json["error"]["message"] == "Internal Server Error"
        assert "Database failure" not in response.get_data(as_text=True)
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    finally:
        server.chat_models = original_chat_models


def test_http_exception_keeps_status_and_reason_phrase_only():
    client = server.app.test_client()
    response = client.get("/no-such-route")
    assert response.status_code == 404
    assert response.json["error"]["message"] == "Not Found"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_upstream_error_detailed_parsing(monkeypatch):
    """Verify that nested error fields in upstream JSON payloads are cleanly parsed and returned."""

    class FakeResponse:
        status_code = 400
        text = '{"error": {"message": "Custom deeply nested upstream validation error message"}}'

        def json(self):
            return json.loads(self.text)

    with server.app.test_request_context():
        response, status = server.upstream_error(FakeResponse())
    assert status == 400
    assert (
        "Custom deeply nested upstream validation error message"
        in response.json["error"]["message"]
    )


# --- NEW TEST CASES FOR ROUND 2 TECH DEBT IMPROVEMENTS ---


def test_secure_fetch_ssrf_dns_rebinding(monkeypatch):
    """Verify that secure_fetch correctly blocks disallowed/private domains."""
    # Mock is_disallowed_host to fail for a target
    monkeypatch.setattr(
        server, "is_disallowed_host", lambda host: host == "malicious.private"
    )

    session = server.get_session()
    with pytest.raises(
        ValueError, match="Access to the requested host address is restricted"
    ):
        server.secure_fetch("https://malicious.private/leak", session)


def test_secure_fetch_safe_redirects(monkeypatch):
    """Verify that secure_fetch manually follows safe redirects, stops after limit, and handles relative URLs."""

    class MockRedirectSession:
        def __init__(self) -> None:
            self.requests_made = []

        def get(self, url, *args, **kwargs):
            self.requests_made.append(url)
            if url == "https://cdn.example.org/start":
                return FakeHTTPResponse(
                    status_code=302, headers={"Location": "/redirect-1"}
                )
            elif url == "https://cdn.example.org/redirect-1":
                return FakeHTTPResponse(
                    status_code=301, headers={"Location": "https://cdn.example.org/end"}
                )
            elif url == "https://cdn.example.org/end":
                return FakeHTTPResponse(status_code=200, text="final-payload")
            return FakeHTTPResponse(status_code=404)

    monkeypatch.setattr(server, "is_disallowed_host", lambda host: False)
    session = MockRedirectSession()

    response = server.secure_fetch("https://cdn.example.org/start", session)
    assert response.status_code == 200
    assert response.text == "final-payload"
    assert session.requests_made == [
        "https://cdn.example.org/start",
        "https://cdn.example.org/redirect-1",
        "https://cdn.example.org/end",
    ]


def test_secure_fetch_redirects_limit(monkeypatch):
    """Verify that secure_fetch throws ValueError if redirect limit is exceeded."""

    class InfiniteRedirectSession:
        def get(self, url, *args, **kwargs):
            return FakeHTTPResponse(status_code=302, headers={"Location": url})

    monkeypatch.setattr(server, "is_disallowed_host", lambda host: False)
    session = InfiniteRedirectSession()

    with pytest.raises(ValueError, match="Too many redirect hops detected"):
        server.secure_fetch("https://cdn.example.org/loop", session, max_redirects=3)


def test_vision_flexible_payload_formats():
    """Verify robust vision processing of both dict and direct string image URLs."""
    # Test case 1: Dict payload (OpenAI standard)
    content_dict = [
        {"type": "text", "text": "Describe this"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img1.png"}},
    ]
    txt, imgs = server.content_to_text(content_dict)
    assert txt == "Describe this"
    assert imgs == ["https://example.com/img1.png"]

    # Test case 2: Direct string payload (Alternate client format)
    content_str = [
        {"type": "text", "text": "Describe this too"},
        {"type": "image_url", "image_url": "https://example.com/img2.png"},
    ]
    txt, imgs = server.content_to_text(content_str)
    assert txt == "Describe this too"
    assert imgs == ["https://example.com/img2.png"]


def test_tiktoken_accurate_counting(monkeypatch):
    """Verify that token_count handles tiktoken encoding safely or falls back."""
    # If tiktoken is loaded, it should count accurately
    cnt = server.token_count("Hello world! How are you?")
    assert cnt > 0


def test_descriptive_empty_request_body():
    """Verify empty requests body returns a descriptive 400 error response instead of generic object message."""
    client = server.app.test_client()
    response = client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer test-key"}, data=""
    )
    assert response.status_code == 400
    assert "Empty request body" in response.json["error"]["message"]
    assert response.json["error"]["code"] == "empty_request_body"


def test_strict_slashes_disabled():
    """Verify routes resolve correctly regardless of trailing slashes."""
    client = server.app.test_client()

    # Check that root with slash works
    res1 = client.get("/")
    assert res1.status_code == 200
    assert "one-min-ai-gateway" in res1.text

    # Check that healthz with trailing slash resolved successfully
    res2 = client.get("/healthz/")
    assert res2.status_code == 200
    assert res2.json["ok"] is True


def test_custom_header_propagation(monkeypatch):
    """Verify custom headers configured via PROPAGATE_HEADERS are correctly forwarded."""
    # Configure config and headers
    monkeypatch.setattr(
        server.config, "PROPAGATE_HEADERS", "X-Gateway-Trace, X-Client-Id"
    )
    monkeypatch.setattr(server, "upload_images", lambda api_key, model, image_urls: [])

    captured_headers = {}

    def fake_post(*args, **kwargs):
        nonlocal captured_headers
        captured_headers = kwargs.get("headers") or {}
        return FakeHTTPResponse(
            status_code=200,
            text='{"aiRecord": {"aiRecordDetail": {"resultObject": ["success-response"]}}}',
        )

    monkeypatch.setattr(server.requests.Session, "post", fake_post)

    client = server.app.test_client()
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-key",
            "X-Gateway-Trace": "123456",
            "X-Client-Id": "client-abc",
            "X-Ignored-Header": "shh",
        },
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    # Ensure custom headers were propagated
    assert captured_headers.get("X-Gateway-Trace") == "123456"
    assert captured_headers.get("X-Client-Id") == "client-abc"
    # Ensure ignored headers were NOT propagated
    assert "X-Ignored-Header" not in captured_headers


class FakeCatalogSession:
    """Session mock that serves a model catalog, or fails on demand."""

    def __init__(self, models: list[dict[str, Any]] | None = None) -> None:
        self.models = models if models is not None else []
        self.failing = False
        self.calls = 0

    def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
        self.calls += 1
        if self.failing:
            # The failure takes long enough for the callers that are waiting
            # for the lock to reach it.
            time.sleep(0.05)
            raise server.requests.ConnectionError("upstream blip")
        # The answer takes long enough for the other callers to wait for it.
        time.sleep(0.05)
        return FakeHTTPResponse(text=json.dumps({"models": self.models}))


@pytest.fixture(autouse=True)
def reset_catalog_state():
    """Give every test a catalog cache of its own.

    The cache is looked up defensively, so that this test file can also be
    run against a `server.py` that has no catalog cache, to see which of the
    tests the change makes fail.
    """
    states = getattr(server, "_catalog_states", None)
    if states is not None:
        states.clear()
    yield
    if states is not None:
        states.clear()


def test_catalog_serves_the_last_good_copy_after_an_upstream_failure(monkeypatch):
    """One upstream blip must not empty the catalog for the whole TTL."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]

    session.failing = True
    server._catalog_state("IMAGE_GENERATOR").fetched_at = 0.0

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]


def test_catalog_is_not_cached_as_empty_after_a_failure(monkeypatch):
    """A failure is remembered briefly, not for the whole TTL."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    session.failing = True
    assert server.fetch_catalog("IMAGE_GENERATOR") is None

    # The upstream comes back; the next attempt picks the catalog up again.
    session.failing = False
    state = server._catalog_state("IMAGE_GENERATOR")
    state.failed_at = 0.0

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]


def test_catalog_failure_is_retried_after_a_short_backoff(monkeypatch):
    """A failing upstream is not asked again for every request."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    session.failing = True
    assert server.fetch_catalog("IMAGE_GENERATOR") is None
    assert session.calls == 1

    # Inside the backoff window the upstream is left alone.
    assert server.fetch_catalog("IMAGE_GENERATOR") is None
    assert session.calls == 1


def test_catalog_fetches_a_feature_once_under_concurrent_requests(monkeypatch):
    """Concurrent requests for one feature cause one upstream call."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    results: list[Any] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        results.append(server.fetch_catalog("IMAGE_GENERATOR"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert session.calls == 1
    assert results == [[{"id": "gpt-4o"}]] * 4


def test_image_generations_reports_an_unavailable_catalog(monkeypatch):
    """An upstream outage is not reported as an unsupported model."""
    session = FakeCatalogSession()
    monkeypatch.setattr(server, "get_session", lambda: session)
    session.failing = True

    client = server.app.test_client()
    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-1", "prompt": "cat"},
        headers={"Authorization": "Bearer fake-key"},
    )

    assert response.status_code == 503
    assert response.json["error"]["code"] == "catalog_unavailable"


def test_models_reports_an_unavailable_catalog(monkeypatch):
    """The model list reports an unavailable catalog instead of an empty list."""
    session = FakeCatalogSession()
    monkeypatch.setattr(server, "get_session", lambda: session)
    session.failing = True

    client = server.app.test_client()
    response = client.get("/v1/models")

    assert response.status_code == 503
    assert response.json["error"]["code"] == "catalog_unavailable"


def test_chat_completions_work_while_the_catalog_is_unavailable(monkeypatch):
    """The catalog only drives a warning on the chat path, never a rejection."""

    class FakeSession:
        def post(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                text=json.dumps(
                    {"aiRecord": {"aiRecordDetail": {"resultObject": "hi"}}}
                )
            )

    monkeypatch.setattr(server, "get_session", lambda: FakeSession())
    monkeypatch.setattr(server, "chat_models", lambda: None)

    client = server.app.test_client()
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer fake-key"},
    )

    assert response.status_code == 200


def test_one_feature_is_fetched_by_one_thread_at_a_time():
    """Verify a feature is fetched by one thread at a time."""
    state = server._catalog_state("UNIFY_CHAT_WITH_AI")
    assert not state.lock.locked()
    with state.lock:
        assert state.lock.locked()
    assert not state.lock.locked()


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.1",
        "100.100.1.1",
        "100.127.255.254",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:100.64.0.1",
        "[::ffff:192.168.1.1]",
        "192.0.2.10",
    ],
)
def test_is_disallowed_host_refuses_non_global_literals(address):
    assert server.is_disallowed_host(address) is True


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "2001:4860:4860::8888", "::ffff:8.8.8.8"]
)
def test_is_disallowed_host_allows_global_literals(address):
    assert server.is_disallowed_host(address) is False


@pytest.mark.parametrize(
    "resolved",
    [
        ["8.8.8.8", "100.64.0.1"],
        ["100.100.100.100"],
        ["2001:4860:4860::8888", "::ffff:10.0.0.1"],
    ],
)
def test_is_disallowed_host_checks_every_resolved_address(monkeypatch, resolved):
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda hostname, *_args, **_kwargs: [(0, 0, 0, "", (ip, 0)) for ip in resolved],
    )
    assert server.is_disallowed_host("images.example.org") is True


def test_is_disallowed_host_allows_all_global_resolved_addresses(monkeypatch):
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda hostname, *_args, **_kwargs: [
            (0, 0, 0, "", ("8.8.8.8", 0)),
            (0, 0, 0, "", ("2001:4860:4860::8888", 0, 0, 0)),
        ],
    )
    assert server.is_disallowed_host("images.example.org") is False


# --- Outbound fetch tests against a local HTTP server ---


@pytest.fixture
def local_http_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("Host")))
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "http://inner.test/next")
                self.end_headers()
                return
            body = b"\x89PNG-test"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], hits
    finally:
        httpd.shutdown()
        httpd.server_close()


def _fake_lookup(table):
    real = server.socket.getaddrinfo

    def lookup(host, port=None, *args, **kwargs):
        if host in table:
            answer = table[host]
            ips = answer() if callable(answer) else answer
            return [
                (
                    server.socket.AF_INET6 if ":" in ip else server.socket.AF_INET,
                    server.socket.SOCK_STREAM,
                    6,
                    "",
                    (ip, port or 0),
                )
                for ip in ips
            ]
        return real(host, port, *args, **kwargs)

    return lookup


def test_secure_fetch_refuses_backslash_in_authority(monkeypatch, local_http_server):
    port, hits = local_http_server
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        _fake_lookup({"public.test": ["8.8.8.8"]}),
    )
    url = f"http://127.0.0.1:{port}\\@public.test/"
    with pytest.raises(ValueError):
        server.secure_fetch(url, server.get_session(), timeout=3)
    assert hits == []


@pytest.mark.parametrize(
    "url",
    ["http://public.test/a b", "http://public.test/\tx", "http://public.test\x00/"],
)
def test_secure_fetch_refuses_whitespace_and_control_characters(monkeypatch, url):
    monkeypatch.setattr(
        server.socket, "getaddrinfo", _fake_lookup({"public.test": ["8.8.8.8"]})
    )

    class NoRequestSession:
        def get(self, *_args, **_kwargs):
            raise AssertionError("request should not happen")

    with pytest.raises(ValueError):
        server.secure_fetch(url, NoRequestSession())


def test_fetch_remote_image_connects_only_to_the_checked_address(
    monkeypatch, local_http_server
):
    port, hits = local_http_server
    answers = iter([["8.8.8.8"]])
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        _fake_lookup({"rebind.test": lambda: next(answers, ["127.0.0.1"])}),
    )
    with server.app.test_request_context():
        result = server.fetch_remote_image(f"http://rebind.test:{port}/image.png")
    assert isinstance(result, tuple)
    assert result[1] == 400
    assert hits == []


def test_fetch_remote_image_rechecks_redirect_target(monkeypatch, local_http_server):
    port, hits = local_http_server
    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        _fake_lookup({"start.test": ["127.0.0.1"], "inner.test": ["10.0.0.1"]}),
    )
    allowed = server.ipaddress.ip_address("127.0.0.1")
    original = server.is_disallowed_address
    monkeypatch.setattr(
        server,
        "is_disallowed_address",
        lambda ip: False if ip == allowed else original(ip),
    )
    with server.app.test_request_context():
        result = server.fetch_remote_image(f"http://start.test:{port}/redirect")
    assert isinstance(result, tuple)
    assert result[1] == 400
    assert [path for path, _host in hits] == ["/redirect"]


def test_guarded_fetch_keeps_host_header(monkeypatch, local_http_server):
    port, hits = local_http_server
    monkeypatch.setattr(
        server.socket, "getaddrinfo", _fake_lookup({"img.test": ["127.0.0.1"]})
    )
    allowed = server.ipaddress.ip_address("127.0.0.1")
    original = server.is_disallowed_address
    monkeypatch.setattr(
        server,
        "is_disallowed_address",
        lambda ip: False if ip == allowed else original(ip),
    )
    with server.app.test_request_context():
        result = server.fetch_remote_image(f"http://img.test:{port}/image.png")
    assert not isinstance(result, tuple)
    assert result.read() == b"\x89PNG-test"
    assert hits == [("/image.png", f"img.test:{port}")]


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::7f00:1",
        "64:ff9b::a00:1",
        "::7f00:1",
        "::a00:1",
        "::ffff:0:7f00:1",
        "::ffff:0:a00:1",
        "2002:a00:1::",
        "2002:6440:1::",
    ],
)
def test_is_disallowed_host_refuses_embedded_non_global_ipv4(address):
    assert server.is_disallowed_host(address) is True


def test_is_disallowed_host_allows_embedded_global_ipv4():
    assert server.is_disallowed_host("64:ff9b::808:808") is False


def test_is_disallowed_host_refuses_when_lookup_fails(monkeypatch):
    def fail_lookup(*_args, **_kwargs):
        raise server.socket.gaierror("lookup failed")

    monkeypatch.setattr(server.socket, "getaddrinfo", fail_lookup)
    assert server.is_disallowed_host("unresolvable.test") is True


def test_fetch_remote_image_rejects_non_image_content_type(monkeypatch):
    monkeypatch.setattr(server, "is_disallowed_host", lambda hostname: False)
    monkeypatch.setattr(
        server.requests.Session,
        "get",
        lambda *_, **__: FakeHTTPResponse(
            status_code=200,
            text='{"a": 1}',
            headers={"Content-Type": "application/json"},
        ),
    )
    with server.app.test_request_context():
        result = server.fetch_remote_image("https://cdn.example.org/image.png")
    assert isinstance(result, tuple)
    assert result[1] == 400
    assert result[0].json["error"]["code"] == "invalid_image_url"


def test_urllib3_major_version_matches_declared_dependency():
    import urllib3

    assert int(urllib3.__version__.split(".")[0]) >= 2
    assert server.NameResolutionError is urllib3.exceptions.NameResolutionError


def test_oversized_request_body_returns_413(monkeypatch):
    """A request body over the limit is refused instead of being read."""
    monkeypatch.setattr(server.config, "MAX_REQUEST_BYTES", 1024)
    app = server.create_app()
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "x" * 8192}],
        },
        headers={"Authorization": "Bearer fake-key"},
    )

    assert response.status_code == 413
    assert response.json["error"]["code"] == "request_too_large"
    assert response.json["error"]["type"] == "invalid_request_error"


def test_request_body_within_the_limit_is_accepted(monkeypatch):
    """A body under the limit is still read and handled."""
    monkeypatch.setattr(server.config, "MAX_REQUEST_BYTES", 1024 * 1024)

    class FakeSession:
        def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            return FakeHTTPResponse(text=json.dumps({"models": [{"id": "gpt-4o"}]}))

        def post(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                text=json.dumps(
                    {"aiRecord": {"aiRecordDetail": {"resultObject": "hi"}}}
                )
            )

    monkeypatch.setattr(server, "get_session", lambda: FakeSession())
    app = server.create_app()
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer fake-key"},
    )

    assert response.status_code == 200


def test_oversized_data_image_is_rejected_before_it_is_decoded(monkeypatch):
    """An oversized inline image is refused without being decoded."""
    monkeypatch.setattr(server.config, "MAX_IMAGE_BYTES", 16)
    encoded = base64.b64encode(b"x" * 100).decode()

    def fail(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("the payload must not be decoded")

    monkeypatch.setattr(server.base64, "b64decode", fail)

    with server.app.app_context():
        result = server.decode_data_image(f"data:image/png;base64,{encoded}")

    assert isinstance(result, tuple)
    assert result[1] == 413


def test_the_body_limit_is_configured_on_the_application():
    """Flask is told the same limit the gateway advertises."""
    assert server.app.config["MAX_CONTENT_LENGTH"] == server.config.MAX_REQUEST_BYTES


def test_main_passes_the_body_limit_to_waitress(monkeypatch):
    """Waitress refuses a body over the limit before it reaches the app."""
    captured: dict[str, Any] = {}

    def fake_serve(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(server, "serve", fake_serve)
    server.main()

    # The server in front is a last resort: the application refuses the body
    # first, so that the answer carries the OpenAI error shape. The margin
    # holds the body the application reads.
    assert (
        captured["max_request_body_size"]
        >= server.config.MAX_REQUEST_BYTES + 1024 * 1024
    )


def test_a_body_without_a_declared_length_is_refused(monkeypatch):
    """A request that declares no length is refused when it is too large."""
    monkeypatch.setattr(server.config, "MAX_REQUEST_BYTES", 1024)

    class Stream:
        """A request body whose length is not declared."""

        def __init__(self) -> None:
            self.data = b"x" * 4096
            self.position = 0

        def read(self, size: int = -1) -> bytes:
            if size is None or size < 0:
                size = len(self.data) - self.position
            chunk = self.data[self.position : self.position + size]
            self.position += len(chunk)
            return chunk

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/v1/chat/completions",
        "CONTENT_TYPE": "application/json",
        "HTTP_AUTHORIZATION": "Bearer fake-key",
        "wsgi.input": Stream(),
        "wsgi.url_scheme": "http",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.errors": io.StringIO(),
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    captured: dict[str, str] = {}

    def start_response(status: str, headers: Any, exc_info: Any = None) -> Any:
        captured["status"] = status
        return lambda data: None

    body = b"".join(server.create_app()(environ, start_response))

    assert "413" in captured["status"]
    assert b"request_too_large" in body


def test_an_image_of_the_limit_plus_one_is_refused(monkeypatch):
    """An image of one byte over the limit is refused, and the size is exact."""
    monkeypatch.setattr(server.config, "MAX_IMAGE_BYTES", 16)
    encoded = base64.b64encode(b"x" * 17).decode()
    decoded = []
    real_decode = server.base64.b64decode

    def spy(*args: Any, **kwargs: Any) -> bytes:
        decoded.append(True)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(server.base64, "b64decode", spy)

    with server.app.app_context():
        result = server.decode_data_image(f"data:image/png;base64,{encoded}")

    assert isinstance(result, tuple)
    assert result[1] == 413
    # A payload of one byte more can have the same text length, so it is
    # decoded to learn its size.
    assert decoded


def test_a_far_too_large_image_is_refused_without_being_decoded(monkeypatch):
    """An image that is far over the limit is refused before it is decoded."""
    monkeypatch.setattr(server.config, "MAX_IMAGE_BYTES", 16)
    encoded = base64.b64encode(b"x" * 4096).decode()

    def fail(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("the payload must not be decoded")

    monkeypatch.setattr(server.base64, "b64decode", fail)

    with server.app.app_context():
        result = server.decode_data_image(f"data:image/png;base64,{encoded}")

    assert isinstance(result, tuple)
    assert result[1] == 413


def test_the_body_is_read_with_a_limit_of_its_own(monkeypatch):
    """The body is read with a limit, so it cannot be read whole into memory."""
    monkeypatch.setattr(server.config, "MAX_REQUEST_BYTES", 1024)

    class Stream:
        """A request body that records how much was asked for."""

        def __init__(self) -> None:
            self.data = b"x" * 4096
            self.position = 0
            self.requested: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.requested.append(size)
            if size is None or size < 0:
                size = len(self.data) - self.position
            chunk = self.data[self.position : self.position + size]
            self.position += len(chunk)
            return chunk

    stream = Stream()
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/v1/chat/completions",
        "CONTENT_TYPE": "application/json",
        "HTTP_AUTHORIZATION": "Bearer fake-key",
        "wsgi.input": stream,
        "wsgi.url_scheme": "http",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.errors": io.StringIO(),
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    captured: dict[str, str] = {}

    def start_response(status: str, headers: Any, exc_info: Any = None) -> Any:
        captured["status"] = status
        return lambda data: None

    b"".join(server.create_app()(environ, start_response))

    # The body was asked for with the limit and nothing more.
    assert stream.requested
    assert max(stream.requested) <= 1025
    assert max(stream.requested) > 0


def test_a_declared_body_over_the_limit_is_refused_without_reading_it(monkeypatch):
    """A body that declares a length over the limit is refused unread."""
    monkeypatch.setattr(server.config, "MAX_REQUEST_BYTES", 1024)

    class Stream:
        def __init__(self) -> None:
            self.reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            return b"x" * 4096

    stream = Stream()
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/v1/chat/completions",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": "4096",
        "HTTP_AUTHORIZATION": "Bearer fake-key",
        "wsgi.input": stream,
        "wsgi.url_scheme": "http",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.errors": io.StringIO(),
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    captured: dict[str, str] = {}

    def start_response(status: str, headers: Any, exc_info: Any = None) -> Any:
        captured["status"] = status
        return lambda data: None

    body = b"".join(server.create_app()(environ, start_response))

    assert "413" in captured["status"]
    assert b"request_too_large" in body
    # The body was never read.
    assert stream.reads == 0


def test_a_body_of_exactly_the_limit_is_accepted(monkeypatch):
    """A body of exactly the limit is not over the limit."""
    monkeypatch.setattr(server.config, "MAX_REQUEST_BYTES", 1024)

    class FakeSession:
        def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            return FakeHTTPResponse(text=json.dumps({"models": [{"id": "gpt-4o"}]}))

        def post(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            return FakeHTTPResponse(
                text=json.dumps(
                    {"aiRecord": {"aiRecordDetail": {"resultObject": "hi"}}}
                )
            )

    monkeypatch.setattr(server, "get_session", lambda: FakeSession())
    # A body of exactly the limit is not over the limit.
    template = json.dumps(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": ""}]}
    )
    filler = "x" * (1024 - len(template))
    payload = json.dumps(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": filler}]}
    ).encode("utf-8")
    assert len(payload) == 1024

    class Stream:
        def __init__(self) -> None:
            self.data = payload
            self.position = 0

        def read(self, size: int = -1) -> bytes:
            if size is None or size < 0:
                size = len(self.data) - self.position
            chunk = self.data[self.position : self.position + size]
            self.position += len(chunk)
            return chunk

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/v1/chat/completions",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(payload)),
        "HTTP_AUTHORIZATION": "Bearer fake-key",
        "wsgi.input": Stream(),
        "wsgi.url_scheme": "http",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.errors": io.StringIO(),
        "wsgi.version": (1, 0),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    captured: dict[str, str] = {}

    def start_response(status: str, headers: Any, exc_info: Any = None) -> Any:
        captured["status"] = status
        return lambda data: None

    b"".join(server.create_app()(environ, start_response))

    assert "200" in captured["status"]


def test_two_images_of_the_limit_still_fit_in_a_request():
    """Two images of the image limit fit in the budget of a request.

    The shipped defaults are used on purpose: the budget of a request has to
    hold the images the per-image limit allows together.
    """
    image = base64.b64encode(b"x" * server.config.MAX_IMAGE_BYTES).decode()
    body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image}"},
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image}"},
                        },
                    ],
                }
            ],
        }
    ).encode("utf-8")

    assert len(body) < server.config.MAX_REQUEST_BYTES
    # The shipped budget holds two images of the shipped image limit.
    assert server.config.MAX_REQUEST_BYTES >= 2 * 4 * (
        (server.config.MAX_IMAGE_BYTES + 2) // 3
    )


def test_an_image_of_exactly_the_limit_is_accepted(monkeypatch):
    """An image of exactly the image limit is accepted."""
    monkeypatch.setattr(server.config, "MAX_IMAGE_BYTES", 16)
    encoded = base64.b64encode(b"x" * 16).decode()

    with server.app.app_context():
        result = server.decode_data_image(f"data:image/png;base64,{encoded}")

    assert not isinstance(result, tuple)


def test_an_empty_catalog_that_was_fetched_is_cached(monkeypatch):
    """An upstream that answers without models is not asked for every request."""
    session = FakeCatalogSession([])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == []
    for _ in range(5):
        assert server.fetch_catalog("IMAGE_GENERATOR") == []
    assert session.calls == 1


def test_a_failing_upstream_is_asked_once_for_a_burst_of_requests(monkeypatch):
    """A burst against a failing upstream causes one upstream call."""
    session = FakeCatalogSession([])
    monkeypatch.setattr(server, "get_session", lambda: session)
    session.failing = True

    results: list[Any] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(server.fetch_catalog("IMAGE_GENERATOR"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert session.calls == 1
    assert results == [None] * 8


def test_one_slow_feature_does_not_block_another(monkeypatch):
    """A feature that is slow to fetch does not hold up the other features."""
    holding = threading.Event()

    class SlowSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            self.calls += 1
            feature = kwargs.get("params", {}).get("feature")
            if feature == "SLOW":
                # The slow fetch holds the lock of its feature while it waits.
                holding.set()
                import time as time_module

                time_module.sleep(0.5)
            return FakeHTTPResponse(text=json.dumps({"models": [{"id": "m"}]}))

    session = SlowSession()
    monkeypatch.setattr(server, "get_session", lambda: session)

    slow: list[Any] = []

    def slow_worker() -> None:
        slow.append(server.fetch_catalog("SLOW"))

    thread = threading.Thread(target=slow_worker)
    thread.start()
    assert holding.wait(5)
    began = time.monotonic()
    assert server.fetch_catalog("FAST") == [{"id": "m"}]
    elapsed = time.monotonic() - began
    thread.join()
    assert slow == [[{"id": "m"}]]
    # The fast feature did not wait for the slow one.
    assert elapsed < 0.25


def test_the_catalog_is_asked_again_after_the_backoff(monkeypatch):
    """A failed attempt is tried again once the backoff has passed."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    session.failing = True
    assert server.fetch_catalog("IMAGE_GENERATOR") is None
    assert session.calls == 1

    state = server._catalog_state("IMAGE_GENERATOR")
    state.failed_at = time.time() - server.config.CATALOG_RETRY_SECONDS - 1
    session.failing = False

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]
    assert session.calls == 2


def test_a_catalog_ttl_of_zero_still_caches_the_catalog(monkeypatch):
    """A TTL of zero means one fetch a second, not one fetch a request."""
    monkeypatch.setattr(server.config, "CATALOG_TTL_SECONDS", 0)
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]
    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]
    assert session.calls == 1


def test_the_image_endpoint_checks_the_prompt_before_the_catalog(monkeypatch):
    """A request without a prompt is refused even while the catalog is down."""
    session = FakeCatalogSession()
    monkeypatch.setattr(server, "get_session", lambda: session)
    session.failing = True

    client = server.app.test_client()
    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-1"},
        headers={"Authorization": "Bearer fake-key"},
    )

    assert response.status_code == 400
    assert response.json["error"]["message"] == "No prompt provided."


def test_a_malformed_catalog_response_does_not_replace_a_good_copy(monkeypatch):
    """A response the gateway does not understand is a failure, not a catalogue."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]

    # The upstream answers with a shape the gateway does not know.
    class MalformedSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            self.calls += 1
            return FakeHTTPResponse(text=json.dumps({}))

    monkeypatch.setattr(server, "get_session", lambda: MalformedSession())
    state = server._catalog_state("IMAGE_GENERATOR")
    state.fetched_at = 0.0
    state.failed_at = 0.0

    # The good copy is still served.
    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]


def test_a_malformed_catalog_response_is_reported_when_there_is_no_copy(monkeypatch):
    """A response the gateway does not understand is reported as unavailable."""

    class MalformedSession:
        def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            return FakeHTTPResponse(text=json.dumps({"models": "not a list"}))

    monkeypatch.setattr(server, "get_session", lambda: MalformedSession())

    assert server.fetch_catalog("IMAGE_GENERATOR") is None

    client = server.app.test_client()
    response = client.get("/v1/models")
    assert response.status_code == 503
    assert response.json["error"]["code"] == "catalog_unavailable"


def test_the_catalog_is_asked_again_once_it_expires(monkeypatch):
    """A catalog that has expired is fetched again, not frozen."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]
    assert session.calls == 1

    # The catalog has expired.
    server._catalog_state("IMAGE_GENERATOR").fetched_at = 0.0

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]
    assert session.calls == 2


def test_a_caller_with_a_copy_does_not_wait_for_a_running_fetch(monkeypatch):
    """A caller that has a copy is served while the upstream is being asked."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]

    state = server._catalog_state("IMAGE_GENERATOR")
    state.fetched_at = 0.0
    holding = threading.Event()
    release = threading.Event()

    class SlowSession:
        def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            holding.set()
            release.wait(5)
            return FakeHTTPResponse(text=json.dumps({"models": [{"id": "gpt-4o"}]}))

    monkeypatch.setattr(server, "get_session", lambda: SlowSession())

    answered: list[Any] = []

    def caller() -> None:
        answered.append(server.fetch_catalog("IMAGE_GENERATOR"))

    thread = threading.Thread(target=caller)
    thread.start()
    assert holding.wait(5)

    # The copy is served while the fetch is still running.
    quick = threading.Thread(
        target=lambda: answered.append(server.fetch_catalog("IMAGE_GENERATOR"))
    )
    quick.start()
    quick.join(2)
    assert not quick.is_alive(), "a caller with a copy waited for the fetch"
    assert answered[-1] == [{"id": "gpt-4o"}]

    release.set()
    thread.join(5)


def test_a_failed_fetch_is_not_asked_again_within_the_backoff(monkeypatch):
    """A recent failure is remembered, so the upstream is not asked again."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    session.failing = True
    assert server.fetch_catalog("IMAGE_GENERATOR") is None
    assert session.calls == 1

    # Inside the backoff window the upstream is left alone.
    assert server.fetch_catalog("IMAGE_GENERATOR") is None
    assert session.calls == 1


def test_a_retry_interval_of_zero_means_one_second(monkeypatch):
    """A backoff of zero or less means one second, not every request."""
    monkeypatch.setattr(server.config, "CATALOG_RETRY_SECONDS", 0)
    assert server._catalog_retry() == 1.0

    monkeypatch.setattr(server.config, "CATALOG_RETRY_SECONDS", -5)
    assert server._catalog_retry() == 1.0

    monkeypatch.setattr(server.config, "CATALOG_RETRY_SECONDS", 10)
    assert server._catalog_retry() == 10.0


def test_a_caller_with_a_fresh_copy_does_not_wait_for_a_running_fetch(monkeypatch):
    """A caller with a fresh copy is served without taking the lock."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]

    state = server._catalog_state("IMAGE_GENERATOR")

    # The fetch of the feature is running and holds its lock.
    holding = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        with state.lock:
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    assert holding.wait(5)
    answered: list[Any] = []
    quick = threading.Thread(
        target=lambda: answered.append(server.fetch_catalog("IMAGE_GENERATOR"))
    )
    try:
        quick.start()
        quick.join(2)
        # The copy is fresh, so the caller did not wait for the lock.
        assert not quick.is_alive(), "a caller with a fresh copy waited for the lock"
        assert answered == [[{"id": "gpt-4o"}]]
    finally:
        release.set()
        holder.join(5)
        quick.join(5)


def test_a_caller_after_a_failure_does_not_wait_for_a_running_fetch(monkeypatch):
    """A recent failure is answered without taking the lock."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    session.failing = True
    assert server.fetch_catalog("IMAGE_GENERATOR") is None

    state = server._catalog_state("IMAGE_GENERATOR")
    holding = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        with state.lock:
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    assert holding.wait(5)
    answered: list[Any] = []
    quick = threading.Thread(
        target=lambda: answered.append(server.fetch_catalog("IMAGE_GENERATOR"))
    )
    try:
        quick.start()
        quick.join(2)
        # The failure is still remembered, so the caller did not wait.
        assert not quick.is_alive(), "a caller after a failure waited for the lock"
        assert answered == [None]
    finally:
        release.set()
        holder.join(5)
        quick.join(5)


def test_a_catalog_with_entries_the_gateway_cannot_read_is_a_failure(monkeypatch):
    """A catalogue whose entries cannot be read never replaces a good one."""
    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]

    class UnreadableSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args: Any, **kwargs: Any) -> FakeHTTPResponse:
            self.calls += 1
            return FakeHTTPResponse(text=json.dumps({"models": [{"name": "gpt-4o"}]}))

    monkeypatch.setattr(server, "get_session", lambda: UnreadableSession())
    state = server._catalog_state("IMAGE_GENERATOR")
    state.fetched_at = 0.0
    state.failed_at = 0.0

    # The good copy is still served.
    assert server.fetch_catalog("IMAGE_GENERATOR") == [{"id": "gpt-4o"}]


def test_a_caller_without_a_copy_waits_for_the_running_fetch(monkeypatch):
    """A caller without a copy waits for the fetch that is running."""
    import threading

    session = FakeCatalogSession([{"id": "gpt-4o"}])
    monkeypatch.setattr(server, "get_session", lambda: session)

    answers: list[Any] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        answers.append(server.fetch_catalog("IMAGE_GENERATOR"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    # One upstream call, and every caller is answered with the catalogue.
    assert session.calls == 1
    assert answers == [[{"id": "gpt-4o"}]] * 4


def _streamed_content(chunks):
    """Collect the content deltas a stream_response() generator emitted."""
    out = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            body = json.loads(chunk[len("data: ") :].strip())
            out.append(body["choices"][0].get("delta", {}).get("content") or "")
    return "".join(out)


def _delta_stream(text, size):
    """Split text into upstream SSE deltas of `size` characters."""
    return [
        f"data: {json.dumps({'delta': {'content': text[i : i + size]}})}".encode(
            "utf-8"
        )
        for i in range(0, len(text), size)
    ]


def test_stream_response_preserves_whitespace_across_flush_boundary():
    """Streamed content must reach the client byte for byte, spaces and newlines included."""
    text = "The quick brown fox jumps over the lazy dog. " * 12
    text += "a paragraph\nwith two lines\nand a trailing newline\n"

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return _delta_stream(text, 120)

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))
    assert received == text


def test_stream_response_drops_crawl_line_split_across_deltas():
    """A crawl-status line split across deltas is dropped whole, not leaked."""
    crawl = "\U0001f310 Crawling site https://example.com\n"
    body = "F" * 180 + "after\n"

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [
                b"data: "
                + json.dumps({"delta": {"content": crawl[:29]}}).encode("utf-8"),
                b"data: "
                + json.dumps({"delta": {"content": crawl[29:]}}).encode("utf-8"),
                b"data: " + json.dumps({"delta": {"content": body}}).encode("utf-8"),
            ]

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))
    assert received == body
    assert "Crawling" not in received
    assert "example.com" not in received


def test_stream_response_preserves_trailing_newline():
    """The final tail is not stripped of its trailing newline."""

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [
                b"data: " + json.dumps({"delta": {"content": "done\n"}}).encode("utf-8")
            ]

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))
    assert received == "done\n"


def test_stream_response_closes_upstream_on_tool_call_exit():
    """The upstream response is closed when the tool-call path returns early."""

    class FakeResponse:
        def __init__(self):
            self.closed = False

        def iter_lines(self, decode_unicode=False):
            return [
                b"event: content",
                b'data: {"delta":{"content":"<tool_call>{\\"name\\":\\"glob\\",\\"arguments\\":{\\"pattern\\":\\"*\\"}}</tool_call>"}}',
                b"",
                b"data: [DONE]",
            ]

        def close(self):
            self.closed = True

    upstream = FakeResponse()
    stream = server.stream_response(
        upstream, {"tools": [{"function": {"name": "glob"}}]}, "m"
    )
    events = []
    for event in stream:
        events.append(event)
        if event == "data: [DONE]\n\n":
            # The gateway stops reading the upstream at the tool call, so the
            # response is released before the done marker is sent.
            assert upstream.closed is True
            break
    assert any("tool_calls" in event for event in events)


def test_stream_response_closes_upstream_before_the_first_chunk():
    """The upstream response is closed even when the stream is closed unread."""

    class FakeResponse:
        def __init__(self):
            self.closed = False

        def iter_lines(self, decode_unicode=False):
            yield b'data: {"delta":{"content":"never read"}}'

        def close(self):
            self.closed = True

    upstream = FakeResponse()
    stream = server.stream_response(upstream, {}, "m")
    stream.close()
    assert upstream.closed is True


def test_stream_response_keeps_content_when_crawl_marker_is_mid_line():
    """A crawl marker in the middle of a line is ordinary text, not a status line."""
    marker = "\U0001f310 Crawling site "
    text = "A" * 100 + marker + "https://example.com/" + "B" * 400

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [
                f"data: {json.dumps({'delta': {'content': text[i : i + 4]}})}".encode(
                    "utf-8"
                )
                for i in range(0, len(text), 4)
            ]

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))
    assert received == text


def test_stream_response_keeps_a_tool_call_on_a_crawl_status_line():
    """A tool call is delivered even when it shares a line with a status line."""
    text = (
        "\U0001f310 Crawling site https://x"
        '<tool_call>{"name":"glob","arguments":{"pattern":"*"}}</tool_call>'
    )

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [
                f"data: {json.dumps({'delta': {'content': text[i : i + 1]}})}".encode(
                    "utf-8"
                )
                for i in range(0, len(text))
            ]

        def close(self):
            pass

    chunks = list(
        server.stream_response(
            FakeResponse(), {"tools": [{"function": {"name": "glob"}}]}, "m"
        )
    )
    assert any("tool_calls" in chunk for chunk in chunks)
    # The crawl-status text is still not sent to the client.
    assert not any("Crawling" in chunk for chunk in chunks)


def test_stream_response_streams_a_long_line_incrementally():
    """A long line without a newline is streamed instead of buffered whole."""
    chunk = " " * 50
    total = 400

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [
                f"data: {json.dumps({'delta': {'content': chunk}})}".encode("utf-8")
                for _ in range(total)
            ]

        def close(self):
            pass

    parts = []
    for event in server.stream_response(FakeResponse(), {}, "m"):
        if event.startswith("data: ") and not event.startswith("data: [DONE]"):
            body = json.loads(event[len("data: ") :].strip())
            content = body["choices"][0].get("delta", {}).get("content") or ""
            if content:
                parts.append(content)
    assert len(parts) > 1
    assert "".join(parts) == chunk * total


def test_stream_response_closes_upstream_when_consumer_stops():
    """The upstream response is closed when the client disconnects mid-stream."""

    class FakeResponse:
        def __init__(self):
            self.closed = False

        def iter_lines(self, decode_unicode=False):
            yield b'data: {"delta":{"content":"partial"}}'

        def close(self):
            self.closed = True

    upstream = FakeResponse()
    stream = server.stream_response(upstream, {}, "m")
    next(stream)
    stream.close()
    assert upstream.closed is True


def test_a_crawl_status_line_in_an_unfinished_tool_call_is_dropped():
    """A tool call that never ends does not let a status line through."""
    text = "<tool_call\nbefore\n\U0001f310 Crawling site https://x\nafter\n"

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [f"data: {json.dumps({'delta': {'content': text}})}".encode("utf-8")]

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))

    assert "Crawling" not in received
    assert "before" in received
    assert "after" in received


def test_a_long_crawl_status_line_is_dropped_whole():
    """A crawl-status line that is longer than a short one is still dropped."""
    line = "\U0001f310 Crawling site " + " " * 5000 + "https://x\n"

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [
                f"data: {json.dumps({'delta': {'content': line[i : i + 200]}})}".encode(
                    "utf-8"
                )
                for i in range(0, len(line), 200)
            ]

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))

    assert "Crawling" not in received
    assert "uuuu" not in received


def test_a_crawl_status_line_after_a_long_run_of_spaces_is_dropped():
    """A status line that follows a long run of spaces is still dropped."""
    line = " " * 4100 + "\U0001f310 Crawling site hidden\n"

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [
                f"data: {json.dumps({'delta': {'content': line[i : i + 137]}})}".encode(
                    "utf-8"
                )
                for i in range(0, len(line), 137)
            ]

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))

    assert "Crawling" not in received
    assert "hidden" not in received


def test_a_crawl_status_line_at_the_end_without_a_newline_is_dropped():
    """A crawl-status line that ends the answer without a newline is dropped."""
    text = "<tool_call\n\U0001f310 Crawling site https://x"

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            return [f"data: {json.dumps({'delta': {'content': text}})}".encode("utf-8")]

        def close(self):
            pass

    received = _streamed_content(server.stream_response(FakeResponse(), {}, "m"))

    assert received == "<tool_call\n"
