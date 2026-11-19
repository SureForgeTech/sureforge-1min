from __future__ import annotations

import atexit
import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
import socket
import re
import threading
import time
import uuid
import ipaddress
from importlib import metadata
from io import BytesIO
from typing import Any, Generator, Iterator
from urllib.parse import urlparse, urljoin

import requests
import urllib3
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util.connection import allowed_gai_family
from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    request,
    Blueprint,
    g,
    has_request_context,
)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.http import HTTP_STATUS_CODES
from waitress import serve


logger = logging.getLogger("one-min-ai-gateway")


def _int_setting(name: str, default: int) -> int:
    """Read an integer setting, falling back to its default.

    Args:
        name: The name of the environment variable.
        default: The value to use when the variable is missing or unreadable.

    Returns:
        int: The value of the setting.
    """
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("the %s setting is not a number; using %d", name, default)
        return default


def _float_setting(name: str, default: float) -> float:
    """Read a number setting, falling back to its default.

    Args:
        name: The name of the environment variable.
        default: The value to use when the variable is missing or unreadable.

    Returns:
        float: The value of the setting.
    """
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("the %s setting is not a number; using %s", name, default)
        return default
    if not math.isfinite(value):
        logger.warning("the %s setting is not a finite number; using %s", name, default)
        return default
    return value


# 13. Centralized GatewayConfig Class
@dataclass
class GatewayConfig:
    """Consolidated configuration holder for the gateway."""

    DEFAULT_CHAT_MODEL: str = os.getenv("DEFAULT_CHAT_MODEL", "gpt-4o")
    DEFAULT_IMAGE_MODEL: str = os.getenv(
        "DEFAULT_IMAGE_MODEL", "black-forest-labs/flux-schnell"
    )
    ONE_MIN_API_BASE_URL: str = os.getenv(
        "ONE_MIN_API_BASE_URL", "https://api.1min.ai"
    ).rstrip("/")
    GATEWAY_VERIFY_SSL: bool = os.getenv("GATEWAY_VERIFY_SSL", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    CATALOG_TTL_SECONDS: int = int(os.getenv("CATALOG_TTL_SECONDS", "300"))
    CATALOG_RETRY_SECONDS: int = int(os.getenv("CATALOG_RETRY_SECONDS", "10"))
    MAX_UPSTREAM_RETRIES: int = _int_setting("MAX_UPSTREAM_RETRIES", 2)
    UPSTREAM_RETRY_BACKOFF_SECONDS: float = _float_setting(
        "UPSTREAM_RETRY_BACKOFF_SECONDS", 0.25
    )
    UPSTREAM_RETRY_MAX_SECONDS: float = _float_setting("UPSTREAM_RETRY_MAX_SECONDS", 5)
    MAX_IMAGE_BYTES: int = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
    # The budget for a whole request. It holds the inline images of a request
    # together: two images of the image limit need about 27 MB.
    MAX_REQUEST_BYTES: int = int(os.getenv("MAX_REQUEST_BYTES", str(32 * 1024 * 1024)))
    CORS_ALLOW_ORIGIN: str = os.getenv("CORS_ALLOW_ORIGIN", "*")
    PORT: int = 5001
    HOST: str = os.getenv("HOST", "0.0.0.0")
    WAITRESS_THREADS: int = 8
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    GATEWAY_POOL_SIZE: int = int(os.getenv("GATEWAY_POOL_SIZE", "20"))
    GATEWAY_POOL_CONNECTIONS: int = int(os.getenv("GATEWAY_POOL_CONNECTIONS", "20"))
    PROPAGATE_HEADERS: str = os.getenv("PROPAGATE_HEADERS", "")

    def __post_init__(self) -> None:
        # Handle PORT conversion safely
        port_env = os.getenv("PORT", "5001")
        try:
            self.PORT = int(port_env)
        except ValueError:
            self.PORT = 5001

        # Handle WAITRESS_THREADS safely
        threads_env = os.getenv("WAITRESS_THREADS", "8")
        try:
            self.WAITRESS_THREADS = int(threads_env)
        except ValueError:
            self.WAITRESS_THREADS = 8

        # The retry settings may not be negative and may not be endless: a
        # negative wait would fail the request instead of waiting, and an
        # endless one would hold it for ever.
        self.MAX_UPSTREAM_RETRIES = max(0, min(self.MAX_UPSTREAM_RETRIES, 10))
        self.UPSTREAM_RETRY_BACKOFF_SECONDS = max(
            0.0, min(self.UPSTREAM_RETRY_BACKOFF_SECONDS, 60.0)
        )
        self.UPSTREAM_RETRY_MAX_SECONDS = max(
            0.0, min(self.UPSTREAM_RETRY_MAX_SECONDS, 60.0)
        )


# Instantiate global settings configuration
config = GatewayConfig()


# Build absolute API endpoints based on base URL setting
def model_api_url() -> str:
    return f"{config.ONE_MIN_API_BASE_URL}/models"


def chat_api_url() -> str:
    return f"{config.ONE_MIN_API_BASE_URL}/api/chat-with-ai"


def chat_stream_api_url() -> str:
    return f"{config.ONE_MIN_API_BASE_URL}/api/chat-with-ai?isStreaming=true"


def asset_api_url() -> str:
    return f"{config.ONE_MIN_API_BASE_URL}/api/assets"


def feature_api_url() -> str:
    return f"{config.ONE_MIN_API_BASE_URL}/api/features"


# 5. Persistent Connection Pooling
_session: requests.Session | None = None
_session_lock = threading.Lock()


def get_session() -> requests.Session:
    """Get or initialize the persistent requests Session for connection pooling.

    Returns:
        requests.Session: The configured requests Session object.
    """
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = requests.Session()
                # 6. Configurable Connection Pool Sizes
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=config.GATEWAY_POOL_CONNECTIONS,
                    pool_maxsize=config.GATEWAY_POOL_SIZE,
                )
                _session.mount("https://", adapter)
                _session.mount("http://", adapter)
    return _session


# 5. Clean Connection Pool Shutdown (atexit hook)
@atexit.register
def close_session() -> None:
    """Cleanly close the global persistent requests Session on shutdown."""
    global _session
    if _image_session is not None:
        try:
            _image_session.close()
        except Exception:
            pass
    if _session is not None:
        try:
            _session.close()
            # Note: logging inside atexit can fail if logging is already dismantled, so we handle it quietly
        except Exception:
            pass


# 17. Custom Header Propagation List
# The upstream statuses that mean the request was not handled and may be sent
# again. A timeout or a broken connection is not in this list: the upstream may
# have handled the request, and sending it again could bill the caller twice.
RETRY_STATUS_CODES = {429, 503}


def _retry_after_seconds(value: str) -> float | None:
    """Read a Retry-After header, which holds a number of seconds or a date.

    Args:
        value: The header value.

    Returns:
        float | None: The number of seconds the header asks for, or None when
        the value cannot be read.
    """
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


def _retry_delay(attempt: int, response: requests.Response | None) -> float:
    """Return how long to wait before the next attempt.

    The wait grows with every attempt. A Retry-After header of the upstream is
    followed, but never shortens the wait and never makes it longer than
    ``UPSTREAM_RETRY_MAX_SECONDS``.

    Args:
        attempt: The number of the attempt that just failed, from zero.
        response: The response that was declined, if there was one.

    Returns:
        float: The number of seconds to wait.
    """
    backoff = min(
        config.UPSTREAM_RETRY_BACKOFF_SECONDS * (2.0**attempt),
        config.UPSTREAM_RETRY_MAX_SECONDS,
    )
    if response is None:
        return backoff
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return backoff
    seconds = _retry_after_seconds(retry_after)
    if seconds is None:
        logger.warning("upstream sent an unreadable Retry-After header")
        return backoff
    if not math.isfinite(seconds):
        logger.warning("upstream sent a Retry-After header that is not a number")
        return backoff
    return min(max(seconds, backoff), config.UPSTREAM_RETRY_MAX_SECONDS)


def _file_stream(value: Any) -> Any:
    """Return the file object of a multipart value, if it carries one.

    The request library accepts a file on its own, a tuple of a name, the file
    and a content type, and a list of such values.

    Args:
        value: One value of the files argument.

    Returns:
        Any: The file object, or None when the value carries none.
    """
    if hasattr(value, "read"):
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _file_stream(item)
            if found is not None:
                return found
    return None


def _rewind_files(kwargs: dict[str, Any], quiet: bool = False) -> bool:
    """Move the streams of a multipart payload back to their beginning.

    The request library reads a file object while it builds the body, so a
    payload that is sent again has to start over. Without this the second
    attempt would upload an empty file.

    Args:
        kwargs: The arguments of the request.
        quiet: Whether to leave out the messages about a file that cannot be
        rewound. The first attempt is not one that is sent again.

    Returns:
        bool: False when a file cannot be rewound, so that the request must
        not be sent again.
    """
    files = kwargs.get("files")
    if isinstance(files, dict):
        values = list(files.values())
    elif isinstance(files, (list, tuple)):
        values = list(files)
    else:
        return True
    for value in values:
        stream = _file_stream(value)
        if stream is None:
            # The value carries no file, so there is nothing to rewind.
            continue
        seek = getattr(stream, "seek", None)
        if seek is None:
            if not quiet:
                logger.warning("a file that cannot be rewound is not sent again")
            return False
        try:
            seek(0)
        except (OSError, ValueError, TypeError):
            if not quiet:
                logger.warning("could not rewind a file that is sent again")
            return False
    return True


def post_upstream(label: str, url: str, **kwargs: Any) -> requests.Response:
    """Send a request to the upstream, retrying one the upstream declined.

    A 429 or 503 means the upstream did not handle the request, so sending it
    again cannot bill the caller twice. A request that failed in the network
    is not retried, because the upstream may have handled it.

    Args:
        label: A short name of the endpoint, used in the log.
        url: The upstream URL.
        **kwargs: Passed to the session.

    Returns:
        requests.Response: The response of the last attempt.
    """
    _rewind_files(kwargs, quiet=True)
    response = get_session().post(url, **kwargs)
    for attempt in range(config.MAX_UPSTREAM_RETRIES):
        if response.status_code not in RETRY_STATUS_CODES:
            break
        if not _rewind_files(kwargs):
            # The file cannot be sent again, so the decline is the answer
            # rather than an upload with nothing in it. The caller is not held
            # for a wait it would not use.
            logger.warning("the %s request is not sent again", label)
            break
        delay = _retry_delay(attempt, response)
        logger.warning(
            "upstream declined the %s request with status %d; retrying in %.2fs",
            label,
            response.status_code,
            delay,
        )
        time.sleep(delay)
        try:
            sent_again = get_session().post(url, **kwargs)
        except requests.RequestException:
            # The upstream may have handled the request, so the decline is a
            # better answer than a broken connection.
            logger.warning("the %s request failed while it was sent again", label)
            break
        response.close()
        response = sent_again
    return response


def propagated_headers() -> dict[str, str]:
    """Retrieve propagating headers from current Flask request context.

    Returns:
        dict[str, str]: Propagated header key/value pairs.
    """
    headers = {}
    if config.PROPAGATE_HEADERS and has_request_context():
        names = [
            name.strip() for name in config.PROPAGATE_HEADERS.split(",") if name.strip()
        ]
        for name in names:
            val = request.headers.get(name)
            if val is not None:
                headers[name] = val
    return headers


# 6. Observability: Request ID Correlation in Logs
class RequestIdFilter(logging.Filter):
    """Logging filter to inject request_id from Flask g into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if has_request_context():
            record.request_id = getattr(g, "request_id", "-")
        else:
            record.request_id = "-"
        return True


def configure_logging() -> None:
    """Configure global, waitress, and logger-specific logging with correlation IDs."""
    # 16. Safe LOG_LEVEL Customizability
    log_level_name = config.LOG_LEVEL
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(level=log_level)
    root = logging.getLogger()
    req_filter = RequestIdFilter()
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s [Request-ID: %(request_id)s] %(name)s: %(message)s"
    )

    # Configure root handlers
    for handler in root.handlers:
        handler.addFilter(req_filter)
        handler.setFormatter(formatter)

    # Configure gateway logger handlers
    for handler in logger.handlers:
        handler.addFilter(req_filter)
        handler.setFormatter(formatter)
    logger.setLevel(log_level)

    # 14. Intercept Waitress internal logger
    waitress_logger = logging.getLogger("waitress")
    for handler in waitress_logger.handlers:
        handler.addFilter(req_filter)
        handler.setFormatter(formatter)
    waitress_logger.setLevel(log_level)


def gateway_version() -> str:
    """Retrieve the installed package version or fallback version.

    Returns:
        str: Package version identifier.
    """
    try:
        return metadata.version("one-min-ai-gateway")
    except metadata.PackageNotFoundError:
        return "0.1.1"


@dataclass
class _CatalogState:
    """The last known model catalog of one feature.

    A copy of the catalog that was fetched successfully is kept, so that a
    failing upstream does not empty the catalog. The copy is None until one was
    fetched; a catalog that was fetched and holds no models is kept as an empty
    tuple, so that an upstream that answers without models is not asked again
    for every request. A response whose shape the gateway does not know, or
    whose entries it cannot read, is a failure, so it never replaces a good
    copy. The failure is remembered for a
    short while, so that a burst of requests does not hammer the upstream. The
    copy is served for as long as the upstream is down, which is the trade-off
    of keeping it.
    """

    entries: tuple[dict[str, Any], ...] | None = None
    fetched_at: float = 0.0
    failed_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


_catalog_states: dict[str, _CatalogState] = {}
_catalog_states_lock = threading.Lock()


def _catalog_state(feature: str) -> _CatalogState:
    """Return the cache state of one feature, creating it when needed.

    Args:
        feature: The feature identifier.

    Returns:
        _CatalogState: The cache state of the feature.
    """
    with _catalog_states_lock:
        state = _catalog_states.get(feature)
        if state is None:
            state = _CatalogState()
            _catalog_states[feature] = state
        return state


def _request_catalog(feature: str) -> tuple[dict[str, Any], ...]:
    """Ask the upstream for the model catalog of one feature.

    Args:
        feature: The feature identifier.

    Returns:
        tuple[dict[str, Any], ...]: The catalog entries.
    """
    response = get_session().get(
        model_api_url(),
        params={"feature": feature},
        timeout=30,
        verify=config.GATEWAY_VERIFY_SSL,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        # The upstream answered with a shape the gateway does not know. That
        # is a failure, not an empty catalogue: the last good one is kept.
        raise ValueError("the model catalog response has an unexpected shape")
    entries = tuple(entry for entry in data["models"] if isinstance(entry, dict))
    if len(entries) != len(data["models"]):
        raise ValueError(
            "the model catalog response holds an entry the gateway cannot read"
        )
    for entry in entries:
        if not model_id(entry):
            raise ValueError(
                "the model catalog response holds an entry without an identifier"
            )
    return entries


def _catalog_retry() -> float:
    """Return how long a failed fetch is remembered.

    An interval of zero or less means one second, so that the upstream is not
    asked again for every request.

    Returns:
        float: The number of seconds.
    """
    return float(
        config.CATALOG_RETRY_SECONDS if config.CATALOG_RETRY_SECONDS > 0 else 1
    )


def _catalog_ttl() -> float:
    """Return how long a fetched catalog is served.

    A TTL of zero or less means the catalog is served for a second, so that
    the upstream is not asked for it on every request.

    Returns:
        float: The number of seconds.
    """
    return float(config.CATALOG_TTL_SECONDS if config.CATALOG_TTL_SECONDS > 0 else 1)


def _catalog_copy(state: _CatalogState, feature: str) -> list[dict[str, Any]] | None:
    """Return the catalog that was fetched last, if there is one.

    Args:
        state: The cache state of a feature.
        feature: The feature identifier.

    Returns:
        list[dict[str, Any]] | None: The catalog entries, or None when no
        catalog was ever fetched.
    """
    if state.entries is None:
        return None
    logger.info("serving the last known model catalog for feature %s", feature)
    return list(state.entries)


def fetch_catalog(feature: str) -> list[dict[str, Any]] | None:
    """Fetch the catalog of one feature.

    The last catalog that was fetched successfully is served when the upstream
    fails, so that one blip does not empty the catalog for the whole TTL. A
    failure is never cached as an empty catalog.

    Args:
        feature: The feature identifier.

    Returns:
        list[dict[str, Any]] | None: The catalog entries, or None when the
        catalog is unavailable and no good copy was ever fetched.
    """
    state = _catalog_state(feature)
    now = time.time()
    if state.entries is not None and now - state.fetched_at < _catalog_ttl():
        return list(state.entries)
    if state.failed_at and now - state.failed_at < _catalog_retry():
        # A recent attempt failed; the upstream is not asked again yet.
        return _catalog_copy(state, feature)

    # Only one thread fetches a feature at a time, so that a burst of
    # requests causes one upstream call. A caller that has a copy to serve
    # does not wait for the fetch that is already running.
    if not state.lock.acquire(blocking=state.entries is None):
        return _catalog_copy(state, feature)
    try:
        now = time.time()
        if state.entries is not None and now - state.fetched_at < _catalog_ttl():
            return list(state.entries)
        if state.failed_at and now - state.failed_at < _catalog_retry():
            # The attempt that held the lock failed; do not start another one.
            return _catalog_copy(state, feature)
        try:
            entries = _request_catalog(feature)
        except requests.RequestException as exc:
            state.failed_at = time.time()
            logger.warning(
                "failed to fetch model catalog for feature %s: %s", feature, exc
            )
            return _catalog_copy(state, feature)
        except ValueError:
            state.failed_at = time.time()
            logger.warning(
                "failed to parse model catalog response for feature %s", feature
            )
            return _catalog_copy(state, feature)
        state.entries = entries
        state.fetched_at = time.time()
        state.failed_at = 0.0
        return list(entries)
    finally:
        state.lock.release()


def chat_models() -> list[dict[str, Any]] | None:
    """Fetch chat models from the model catalog.

    Returns:
        list[dict[str, Any]] | None: List of chat models, or None when the
        catalog is unavailable.
    """
    return fetch_catalog("UNIFY_CHAT_WITH_AI")


def model_id(entry: dict[str, Any]) -> str:
    """Safely extract model ID from a catalog entry.

    Args:
        entry: The catalog model entry.

    Returns:
        str: The identified model ID.
    """
    val = entry.get("id") or entry.get("modelId")
    if not val:
        logger.warning("Catalog model entry lacks a valid identifier: %s", entry)
        return ""
    return str(val)


# 7. Vision and Image Model ID Sets Cache Optimization (frozenset)
def vision_model_ids() -> frozenset[str] | None:
    """Retrieve the current set of vision model IDs.

    Returns:
        frozenset[str] | None: Set of model IDs supporting image inputs, or
        None when the catalog is unavailable.
    """
    catalog = fetch_catalog("CHAT_WITH_IMAGE")
    if catalog is None:
        return None
    return frozenset(model_id(entry) for entry in catalog if model_id(entry))


def image_model_ids() -> frozenset[str] | None:
    """Retrieve the current set of image model IDs.

    Returns:
        frozenset[str] | None: Set of model IDs supporting image generation,
        or None when the catalog is unavailable.
    """
    catalog = fetch_catalog("IMAGE_GENERATOR")
    if catalog is None:
        return None
    return frozenset(model_id(entry) for entry in catalog if model_id(entry))


def error_response(
    message: str, status: int, code: str | None = None
) -> tuple[Response, int]:
    """Build a standard OpenAI-compatible API error response.

    Args:
        message: Readable error message.
        status: HTTP status code.
        code: Specific error code.

    Returns:
        tuple[Response, int]: Flask response and status code.
    """
    return jsonify(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error" if status < 500 else "server_error",
                "param": None,
                "code": code,
            }
        }
    ), status


def bearer_key() -> str | None:
    """Extract bearer API key from Authorization header.

    Returns:
        str | None: The API key if found, else None.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header.split(" ", 1)[1].strip()


def add_headers(response: Response) -> Response:
    """Inject standard CORS and Request ID correlation headers into response.

    Args:
        response: Flask Response object.

    Returns:
        Response: The modified Response object.
    """
    response.headers["Access-Control-Allow-Origin"] = config.CORS_ALLOW_ORIGIN

    # 3. Strip Waitress server fingerprints
    response.headers["Server"] = "one-min-ai-gateway"
    # Every body is JSON; stop browsers from sniffing a response into another type.
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Propagate the request ID if available
    req_id = getattr(g, "request_id", None) or str(uuid.uuid4())
    response.headers["X-Request-ID"] = req_id
    return response


def request_json() -> dict[str, Any] | tuple[Response, int]:
    """Safely parse incoming JSON body.

    Returns:
        dict[str, Any] | tuple[Response, int]: Parsed JSON dict or Flask error response.
    """
    # 12. Improved Empty Request Body Parsing
    limit = config.MAX_REQUEST_BYTES
    # A body that declares its length is refused before it is read.
    if request.content_length is not None and request.content_length > limit:
        return error_response(
            f"The request body is larger than {limit} bytes.",
            413,
            "request_too_large",
        )
    # The body is read from the raw input with a limit of its own. The
    # framework does not limit a body whose length is not declared, so the
    # excess would go unnoticed without it.
    raw = request.environ.get("wsgi.input")
    body = raw.read(limit + 1) if raw is not None else b""
    if len(body) > limit:
        return error_response(
            f"The request body is larger than {limit} bytes.",
            413,
            "request_too_large",
        )
    if not body:
        return error_response(
            "Empty request body. Please provide a valid JSON object.",
            400,
            "empty_request_body",
        )
    try:
        data = json.loads(body)
    except ValueError:
        return error_response("Invalid JSON body.", 400, "invalid_json")
    if not isinstance(data, dict):
        return error_response(
            "JSON body must be an object.", 400, "invalid_request_error"
        )
    return data


# 9. Flexible Vision Message Payload Formats (String vs. Dict)
def content_to_text(content: Any) -> tuple[str, list[str]]:
    """Convert OpenAI message content to prompt text and remote image URLs.

    Args:
        content: OpenAI message content structure.

    Returns:
        tuple[str, list[str]]: Text content and image URLs.
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "" if content is None else str(content), []

    text: list[str] = []
    images: list[str] = []
    for part in content:
        if isinstance(part, str):
            text.append(part)
            continue
        if not isinstance(part, dict):
            continue

        # Extract text content if present
        if part.get("type") == "text" or "text" in part:
            text.append(str(part.get("text", "")))

        # Support both {"url": "..."} dict and direct "image_url" string formats
        image_url = part.get("image_url")
        url = None
        if isinstance(image_url, dict):
            url = image_url.get("url")
        elif isinstance(image_url, str):
            url = image_url

        if url:
            images.append(str(url))

    return "\n".join(text), images


def format_messages(messages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Format messages array into clean flat prompt and remote image URLs.

    Args:
        messages: List of chat messages.

    Returns:
        tuple[str, list[str]]: Consolidated prompt text and remote image URLs.
    """
    lines: list[str] = []
    images: list[str] = []
    for message in messages:
        role = str(message.get("role", "user")).upper()
        content, message_images = content_to_text(message.get("content"))
        images.extend(message_images)

        if role == "ASSISTANT" and message.get("tool_calls"):
            lines.append(
                f"ASSISTANT TOOL CALLS: {json.dumps(message['tool_calls'], ensure_ascii=False)}"
            )
        elif role == "TOOL":
            name = message.get("name") or message.get("tool_call_id") or "tool"
            lines.append(f"TOOL RESULT ({name}): {content}")
        elif content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines), images


def has_tool_result(messages: list[dict[str, Any]]) -> bool:
    """Check if tool execution output is present in the messages sequence.

    Args:
        messages: The chat messages.

    Returns:
        bool: True if there is a tool response.
    """
    return any(message.get("role") == "tool" for message in messages)


def tool_prompt(tools: list[dict[str, Any]], saw_tool_result: bool) -> str:
    """Format tool declarations into instructions for prompt injection.

    Args:
        tools: Tool declarations.
        saw_tool_result: If tool result was already provided.

    Returns:
        str: Instructions injection snippet.
    """
    rendered = []
    for tool in tools or []:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = function.get("name")
        if name:
            rendered.append(
                {
                    "name": name,
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                }
            )
    if not rendered:
        return ""

    lines = [
        "",
        "TOOLS:",
        json.dumps(rendered, ensure_ascii=False),
        "",
        "If a tool is needed, respond with exactly one line and no other text:",
        '<tool_call>{"name":"tool_name","arguments":{"arg":"value"}}</tool_call>',
        "Do not write fake progress lines, fake crawling status, Markdown tool blocks, or natural-language descriptions of a tool call.",
    ]
    if saw_tool_result:
        lines.append("Tool results are already present. Prefer answering the user now.")
    return "\n".join(lines)


def parse_tool_text(text: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Parse output text to discover structured tool calls.

    Args:
        text: Model output text.
        tools: Supported tools configuration.

    Returns:
        dict[str, Any] | None: Parse tool call dictionary, if found.
    """
    available = {
        tool.get("function", {}).get("name")
        for tool in tools or []
        if isinstance(tool, dict) and tool.get("function", {}).get("name")
    }

    match = re.search(
        r"<tool_(?:call|code)>\s*(\{.*?\})\s*</tool_(?:call|code)>", text, re.DOTALL
    )
    if match:
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            name = parsed.get("name") or parsed.get("tool")
            args = parsed.get("arguments") or parsed.get("input") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"input": args}
            if name and (not available or name in available):
                return {
                    "name": name,
                    "arguments": args if isinstance(args, dict) else {},
                }

    match = re.search(r"\[tool_code_bash_call\]:\s*(.+)", text, re.DOTALL)
    if match and (not available or "bash" in available):
        return {"name": "bash", "arguments": {"command": match.group(1).strip()}}

    match = re.search(
        r"\[tool_code:\s*([A-Za-z0-9_-]+)\s+for\s+([A-Za-z0-9_-]+)\s+(['\"])(.*?)\3\s*\]",
        text,
        re.DOTALL,
    )
    if match:
        name = match.group(1)
        if not available or name in available:
            return {"name": name, "arguments": {match.group(2): match.group(4)}}
    return None


CRAWL_MARKER = "🌐 Crawling site "

# The most text held back, in characters, while it is still undecided whether
# the current line is a crawl-status line. The holdback is what lets the
# filter see the marker after any leading space, so it is generous; a line
# that is still undecided when it is longer than this is let through rather
# than held.
CRAWL_HOLDBACK_MAX = 8192


def clean_text(text: str) -> str:
    """Clean specific crawl status lines from output text.

    This is the whole-text path, used where the complete response is
    available. The streaming path filters at line boundaries instead and
    keeps the text byte for byte.

    Args:
        text: The text to clean.

    Returns:
        str: Cleaned text content.
    """
    lines = [
        line for line in text.splitlines() if not line.strip().startswith(CRAWL_MARKER)
    ]
    return "\n".join(lines).strip()


def _is_crawl_line(text: str) -> bool:
    """Return True when a line is a crawl-status line that must be dropped.

    Args:
        text: One line, without its terminator.

    Returns:
        bool: True if the line must not reach the client.
    """
    return text.strip().startswith(CRAWL_MARKER)


def _crawl_decision(text: str) -> str:
    """Classify a line that is still being received.

    A line is a crawl-status line when its first non-space characters are the
    crawl marker. While the text seen so far still matches that marker, more
    text could turn it into a crawl-status line, so the answer stays
    undecided and the line is held back.

    Args:
        text: The part of the line received so far.

    Returns:
        str: "crawl" when the line is a crawl-status line, "clear" when it
        cannot be one, and "undecided" while more text could change that.
    """
    stripped = text.lstrip()
    if not stripped:
        return "undecided"
    if stripped.startswith(CRAWL_MARKER):
        # The marker is there. It counts only when non-space text follows it,
        # because the marker on its own is not a crawl-status line.
        return "crawl" if stripped[len(CRAWL_MARKER) :].strip() else "undecided"
    if CRAWL_MARKER.startswith(stripped):
        return "undecided"
    return "clear"


def _drop_crawl_lines(text: str, at_line_start: bool) -> str:
    """Drop the crawl-status lines among the complete lines of a text.

    The text may hold more than one line, which happens when a tool call was
    never finished and the line splitting was skipped. The first line is only
    a crawl-status line when it really begins a line.

    Args:
        text: Text that holds complete lines and a trailing partial line.
        at_line_start: Whether the first line of the text begins a line.

    Returns:
        str: The text without its crawl-status lines.
    """
    if "\n" not in text:
        if at_line_start and _is_crawl_line(text):
            return ""
        return text
    head, _, partial = text.rpartition("\n")
    kept: list[str] = []
    for index, line in enumerate(head.split("\n")):
        if _is_crawl_line(line) and (at_line_start or index > 0):
            continue
        kept.append(line)
    result = "\n".join(kept)
    if kept:
        result += "\n"
    # The last line has no terminator, but it still begins a line, so a
    # crawl-status line among them is dropped as well.
    if partial and not _is_crawl_line(partial):
        result += partial
    return result


def _find_line_end(text: str) -> tuple[int, int]:
    """Find the next line terminator in text.

    Only "\n" ends a line here. The other characters ``str.splitlines`` treats
    as line breaks are not terminators, so the text the client receives keeps
    them, and a line that starts with a carriage return is still the line it
    belongs to.

    Args:
        text: Text to search.

    Returns:
        tuple[int, int]: Where the terminator starts and how long it is, or
        (-1, 0) when the text holds no terminator.
    """
    newline = text.find("\n")
    if newline == -1:
        return -1, 0
    return newline, 1


def tool_message(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Build standard OpenAI assistant tool response.

    Args:
        tool_call: Decoded tool call details.

    Returns:
        dict[str, Any]: Formatted tool assistant response message.
    """
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": tool_call["name"],
                    "arguments": json.dumps(
                        tool_call.get("arguments", {}), ensure_ascii=False
                    ),
                },
            }
        ],
    }


def upstream_error(response: requests.Response) -> tuple[Response, int]:
    """Parse and translate errors from upstream 1min.ai APIs.

    Args:
        response: Upstream response.

    Returns:
        tuple[Response, int]: Translated API response and status.
    """
    if response.status_code in {401, 403}:
        return error_response("Invalid Authentication", 401, "invalid_api_key")
    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]

    error_msg = ""
    if isinstance(body, dict):
        if "error" in body:
            err = body["error"]
            if isinstance(err, dict):
                error_msg = err.get("message") or err.get("errorMessage") or ""
            elif isinstance(err, str):
                error_msg = err
        if not error_msg:
            error_msg = (
                body.get("message")
                or body.get("errorMessage")
                or body.get("error")
                or ""
            )

    if not error_msg:
        error_msg = str(body)

    logger.error("1min.ai upstream error %s: %s", response.status_code, error_msg)
    return error_response(
        f"1min.ai API error ({response.status_code}): {error_msg}", response.status_code
    )


def response_json(response: requests.Response) -> dict[str, Any] | None:
    """Parse response JSON safely checking for dictionary type compatibility.

    Args:
        response: requests Response.

    Returns:
        dict[str, Any] | None: Dictionary data structure, or None.
    """
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")
_IPV4_TRANSLATED_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")


def embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address an IPv6 address stands in for, if any.

    Covers IPv4-mapped, IPv4-compatible, IPv4-translated and NAT64
    (well-known prefix) forms, whose only meaning is the IPv4 address.

    Args:
        ip: Parsed IPv6 address.

    Returns:
        IPv4Address | None: The embedded IPv4 address, or None.
    """
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if (
        ip in _NAT64_PREFIX
        or ip in _IPV4_COMPATIBLE_PREFIX
        or ip in _IPV4_TRANSLATED_PREFIX
    ):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_disallowed_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True unless an address is publicly routable.

    Anything ``ipaddress`` does not report as global is refused, which covers
    loopback, private, link-local, shared (carrier-grade NAT), reserved and
    documentation ranges. IPv6 forms that stand in for an IPv4 address are
    judged by that IPv4 address; 6to4 and Teredo addresses must also carry a
    public IPv4 address.

    Args:
        ip: Parsed IPv4 or IPv6 address.

    Returns:
        bool: True if the address must not be fetched.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        inner = embedded_ipv4(ip)
        if inner is not None:
            return is_disallowed_address(inner)
    if not ip.is_global or ip.is_multicast or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.sixtofour is not None and is_disallowed_address(ip.sixtofour):
            return True
        if ip.teredo is not None and is_disallowed_address(ip.teredo[1]):
            return True
    return False


def is_disallowed_host(hostname: str) -> bool:
    """Validate remote hostname against DNS and non-public address bypass.

    Every address the hostname resolves to is checked; one non-public answer
    is enough to refuse the host.

    Args:
        hostname: Input host address string.

    Returns:
        bool: True if hostname resolves to a non-public address or is disallowed.
    """
    normalized = (hostname or "").strip().lower().strip("[]")
    if not normalized:
        return True
    if normalized in {"localhost", "localhost.localdomain"}:
        return True

    try:
        return is_disallowed_address(ipaddress.ip_address(normalized))
    except ValueError:
        pass

    try:
        addresses = socket.getaddrinfo(normalized, None)
    except (socket.gaierror, UnicodeError):
        return True
    if not addresses:
        return True

    for _, _, _, _, sockaddr in addresses:
        if not sockaddr:
            continue
        ip_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        if is_disallowed_address(ip):
            return True
    return False


class DisallowedAddressError(ValueError):
    """Raised when an outbound fetch would connect to a non-public address."""


def guarded_create_connection(
    address: tuple[str, int],
    timeout: Any,
    source_address: tuple[str, int] | None = None,
    socket_options: Any = None,
) -> socket.socket:
    """Resolve a host once, refuse it unless every address is public, then connect.

    The socket connects only to an address from the same lookup that was
    checked, so a second DNS answer cannot change where the connection goes.

    Args:
        address: Host and port to connect to.
        timeout: Socket timeout, or urllib3's default-timeout sentinel.
        source_address: Optional local address to bind.
        socket_options: Optional socket options to set before connecting.

    Returns:
        socket.socket: The connected socket.
    """
    host, port = address
    host = host.strip("[]")
    results = socket.getaddrinfo(host, port, allowed_gai_family(), socket.SOCK_STREAM)
    if not results:
        raise DisallowedAddressError("Host did not resolve to any address.")
    for *_, sockaddr in results:
        try:
            ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            raise DisallowedAddressError("Host resolved to an unusable address.")
        if is_disallowed_address(ip):
            raise DisallowedAddressError(
                "Access to the requested host address is restricted."
            )

    err: OSError | None = None
    for family, socktype, proto, _, sockaddr in results:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            for option in socket_options or []:
                sock.setsockopt(*option)
            if isinstance(timeout, (int, float)) or timeout is None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    assert err is not None
    raise err


def _guarded_new_conn(conn: HTTPConnection) -> socket.socket:
    try:
        return guarded_create_connection(
            (conn._dns_host, conn.port),
            conn.timeout,
            source_address=conn.source_address,
            socket_options=conn.socket_options,
        )
    except socket.gaierror as exc:
        raise NameResolutionError(conn.host, conn, exc) from exc
    except TimeoutError as exc:
        raise ConnectTimeoutError(
            conn, f"Connection to {conn.host} timed out."
        ) from exc
    except OSError as exc:
        raise NewConnectionError(
            conn, f"Failed to establish a new connection: {exc}"
        ) from exc


class GuardedHTTPConnection(HTTPConnection):
    """HTTP connection that only connects to a checked public address."""

    def _new_conn(self) -> socket.socket:
        return _guarded_new_conn(self)


class GuardedHTTPSConnection(HTTPSConnection):
    """HTTPS connection that only connects to a checked public address.

    TLS server name and certificate checks still use the URL's hostname.
    """

    def _new_conn(self) -> socket.socket:
        return _guarded_new_conn(self)


class GuardedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = GuardedHTTPConnection


class GuardedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = GuardedHTTPSConnection


class GuardedHTTPAdapter(requests.adapters.HTTPAdapter):
    """Transport adapter whose connections refuse non-public addresses."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": GuardedHTTPConnectionPool,
            "https": GuardedHTTPSConnectionPool,
        }


_image_session: requests.Session | None = None


def get_image_session() -> requests.Session:
    """Get the session used for caller-supplied image URLs.

    It is separate from the upstream API session: its connections refuse
    non-public addresses, and it ignores proxy settings from the environment,
    because a proxy would make the connection-time address check meaningless.

    Returns:
        requests.Session: The image-fetch session.
    """
    global _image_session
    if _image_session is None:
        with _session_lock:
            if _image_session is None:
                session = requests.Session()
                session.trust_env = False
                adapter = GuardedHTTPAdapter(
                    pool_connections=config.GATEWAY_POOL_CONNECTIONS,
                    pool_maxsize=config.GATEWAY_POOL_SIZE,
                )
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                _image_session = session
    return _image_session


_URL_FORBIDDEN_CHARS = re.compile(r"[\\\s\x00-\x1f\x7f]")


def checked_fetch_url(url: str) -> str:
    """Validate one URL and return the exact URL the HTTP client will request.

    Refuses backslashes, whitespace and control characters, and any URL where
    the standard-library parser and the HTTP client's parser disagree about
    the host. The host is then checked as the HTTP client reads it.

    Args:
        url: Candidate URL (the original one or a redirect target).

    Returns:
        str: The prepared URL to request.
    """
    if _URL_FORBIDDEN_CHARS.search(url):
        raise ValueError("URL contains characters that are not allowed.")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only http and https schemes are permitted.")
    try:
        client_host = urllib3.util.parse_url(url).host
    except urllib3.exceptions.LocationParseError as exc:
        raise ValueError("URL could not be parsed.") from exc
    stdlib_host = parsed.hostname
    if not stdlib_host or not client_host:
        raise ValueError("URL hostname is missing.")
    if stdlib_host.strip("[]").lower() != client_host.strip("[]").lower():
        raise ValueError("URL host is ambiguous.")

    prepared_url = requests.Request("GET", url).prepare().url
    if not prepared_url:
        raise ValueError("URL could not be parsed.")
    prepared_host = urllib3.util.parse_url(prepared_url).host
    if not prepared_host or is_disallowed_host(prepared_host):
        raise ValueError("Access to the requested host address is restricted.")
    return prepared_url


# Outbound fetch of caller-supplied URLs: every hop is checked before it is
# requested, and the guarded session connects only to addresses that pass the
# same check (see GuardedHTTPAdapter).
def secure_fetch(
    url: str, session: requests.Session, timeout: int = 30, max_redirects: int = 5
) -> requests.Response:
    """Perform an HTTP GET for a caller-supplied URL, checking every redirect hop.

    Args:
        url: The starting target URL.
        session: Session to send with; use get_image_session() so connections
            are pinned to checked addresses.
        timeout: Socket and network read timeout.
        max_redirects: Maximum allowable redirect hops.

    Returns:
        requests.Response: The successful response object.
    """
    current_url = url
    for hop in range(max_redirects + 1):
        request_url = checked_fetch_url(current_url)

        response = session.get(
            request_url,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
            verify=config.GATEWAY_VERIFY_SSL,
        )

        if 300 <= response.status_code < 400:
            if hop >= max_redirects:
                raise ValueError("Too many redirect hops detected.")

            location = response.headers.get("Location")
            if not location:
                raise ValueError("Redirect response did not provide a Location header.")

            current_url = urljoin(request_url, location)
            continue

        response.raise_for_status()
        return response

    raise ValueError("Too many redirect hops detected.")


def validate_messages(messages: Any) -> tuple[Response, int] | None:
    """Verify standard schema and format of messages list.

    Args:
        messages: Input raw message structure.

    Returns:
        tuple[Response, int] | None: Error response if invalid, else None.
    """
    if not isinstance(messages, list) or not messages:
        return error_response(
            "messages must be a non-empty array.", 400, "invalid_request_error"
        )

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return error_response(
                f"Message at index {index} must be an object.",
                400,
                "invalid_request_error",
            )
    return None


def decode_data_image(image_url: str) -> BytesIO | tuple[Response, int]:
    """Decode local inline base64 image data payload.

    Args:
        image_url: Local base64 string URL.

    Returns:
        BytesIO | tuple[Response, int]: Raw data block, or error response.
    """
    try:
        encoded = image_url.split(",", 1)[1]
    except IndexError:
        return error_response("Invalid image data URL.", 400, "invalid_image")
    # The encoded text is measured before it is decoded, so a payload that is
    # far over the limit is never turned into bytes in memory. The bound is
    # the longest text a payload of the limit can have, rounded up to a whole
    # quantum, because a payload of one or two bytes more can have the same
    # text length.
    if len(encoded) > 4 * ((config.MAX_IMAGE_BYTES + 4) // 3):
        return error_response("Image payload is too large.", 413, "image_too_large")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return error_response("Invalid base64 image data.", 400, "invalid_image")
    if len(payload) > config.MAX_IMAGE_BYTES:
        return error_response("Image payload is too large.", 413, "image_too_large")
    return BytesIO(payload)


def fetch_remote_image(image_url: str) -> BytesIO | tuple[Response, int]:
    """Fetch remote HTTP/HTTPS image safely using secure multi-hop DNS protection.

    Args:
        image_url: Remote web address.

    Returns:
        BytesIO | tuple[Response, int]: Downloaded image buffer, or error response.
    """
    try:
        # Use our secure custom DNS redirect-following fetch wrapper
        fetched = secure_fetch(
            image_url, get_image_session(), timeout=30, max_redirects=5
        )
    except ValueError as exc:
        logger.warning("failed to fetch image URL securely: %s", exc)
        return error_response("Could not fetch image URL.", 400, "invalid_image_url")
    except requests.RequestException as exc:
        logger.warning("failed to fetch image URL: %s", exc)
        return error_response("Could not fetch image URL.", 400, "invalid_image_url")

    content_type = fetched.headers.get("Content-Type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type and not (
        media_type.startswith("image/") or media_type == "application/octet-stream"
    ):
        fetched.close()
        return error_response(
            "Image URL did not return an image.", 400, "invalid_image_url"
        )

    content_length = fetched.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > config.MAX_IMAGE_BYTES:
                return error_response(
                    "Image payload is too large.", 413, "image_too_large"
                )
        except ValueError:
            pass

    data = BytesIO()
    for chunk in fetched.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        data.write(chunk)
        if data.tell() > config.MAX_IMAGE_BYTES:
            return error_response("Image payload is too large.", 413, "image_too_large")
    data.seek(0)
    return data


def upload_images(
    api_key: str, model: str, image_urls: list[str]
) -> list[str] | tuple[Response, int]:
    """Download, validate, and upload images to 1min.ai assets api.

    Args:
        api_key: API credentials token.
        model: Target model selector.
        image_urls: Raw model input image paths.

    Returns:
        list[str] | tuple[Response, int]: Uploaded asset path IDs, or error response.
    """
    if not image_urls:
        return []
    vision_ids = vision_model_ids()
    if vision_ids is None:
        return error_response(
            "The model catalog is temporarily unavailable.",
            503,
            "catalog_unavailable",
        )
    if model not in vision_ids:
        return error_response(
            f"This model does not support image inputs: {model}",
            400,
            "model_not_supported",
        )

    paths: list[str] = []
    for image_url in image_urls:
        if image_url.startswith("data:image/"):
            binary_data = decode_data_image(image_url)
        else:
            binary_data = fetch_remote_image(image_url)
        if isinstance(binary_data, tuple):
            return binary_data

        # Use connection pool session and propagated headers
        upload = post_upstream(
            "asset",
            asset_api_url(),
            files={"asset": (f"gateway-{uuid.uuid4()}.png", binary_data, "image/png")},
            headers={"API-KEY": api_key, **propagated_headers()},
            timeout=60,
            verify=config.GATEWAY_VERIFY_SSL,
        )
        if upload.status_code != 200:
            return upstream_error(upload)
        upload_data = response_json(upload)
        if not upload_data:
            return upstream_error(upload)
        try:
            paths.append(upload_data["fileContent"]["path"])
        except KeyError:
            return upstream_error(upload)
    return paths


def build_payload(
    api_key: str, data: dict[str, Any]
) -> dict[str, Any] | tuple[Response, int]:
    """Assemble final JSON payload structure required by the upstream API.

    Args:
        api_key: Credentials token.
        data: User request body parameters.

    Returns:
        dict[str, Any] | tuple[Response, int]: Formatted payload dict, or error response.
    """
    messages = data.get("messages") or []
    validation = validate_messages(messages)
    if validation is not None:
        return validation

    model = data.get("model", config.DEFAULT_CHAT_MODEL)

    # 19. Enhanced Model Warning Traces
    catalog = chat_models()
    if catalog is not None and model not in {
        model_id(m) for m in catalog if model_id(m)
    }:
        logger.warning(
            "Requested model '%s' is not listed in active models catalog.", model
        )

    prompt, image_urls = format_messages(messages)
    prompt += tool_prompt(data.get("tools") or [], has_tool_result(messages))

    image_paths = upload_images(api_key, model, image_urls)
    if isinstance(image_paths, tuple):
        return image_paths

    payload: dict[str, Any] = {
        "type": "UNIFY_CHAT_WITH_AI",
        "model": model,
        "promptObject": {"prompt": prompt},
    }
    if image_paths:
        payload["promptObject"]["attachments"] = {"images": image_paths}
    return payload


def extract_result_text(data: dict[str, Any]) -> str | None:
    """Parse result object to extract clean upstream text output.

    Args:
        data: Upstream response data.

    Returns:
        str | None: Raw text result if found, else None.
    """
    try:
        result = data["aiRecord"]["aiRecordDetail"]["resultObject"][0]
    except (KeyError, IndexError, TypeError):
        return None
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


# The characters a tokeniser of a model provider gives a token of their own:
# the ideographs, the kana and the hangul syllables. A run of them is counted
# one by one, because a word of them is not one token.
_CJK_CHARACTERS = (
    "\u2e80-\u2eff"  # CJK radicals and kangxi radicals
    "\u3005-\u3007"  # the iteration mark and the ideographic zero
    "\u3040-\u309f"  # hiragana
    "\u30a0-\u30ff"  # katakana
    "\u3105-\u312f"  # bopomofo
    "\u3130-\u318f"  # hangul compatibility jamo
    "\u31a0-\u31bf"  # bopomofo extensions
    "\u31c0-\u31ef"  # CJK strokes
    "\u31f0-\u31ff"  # katakana phonetic extensions
    "\u3400-\u4dbf"  # CJK unified ideographs extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uac00-\ud7af"  # hangul syllables
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\uff66-\uff9f"  # halfwidth katakana
    "\U00020000-\U0002ffff"  # CJK unified ideographs extension B and later
)
# The pieces a token is made of: a character of its own, a group of digits, a
# word, or a mark of punctuation. The digits are counted in groups of three,
# because that is how a tokeniser of a model provider splits them.
_TOKEN_PIECE = re.compile(
    "[" + _CJK_CHARACTERS + "]"  # a character a tokeniser gives a token of its own
    "|[0-9]{1,3}"  # a group of digits
    "|[^\\W\\d" + _CJK_CHARACTERS + "]+"  # a word
    "|[^\\w\\s]"  # a mark of punctuation
)
# The longest piece that is carried from one piece of text to the next. A
# longer one is counted as it is, so that the buffer stays bounded. It is far
# longer than any word a model writes.
MAX_TOKEN_LENGTH = 65536


def stream_with_usage(
    chunks: Iterator[str], prompt_tokens: int
) -> Generator[str, None, None]:
    """Add a usage chunk to a stream of event-stream chunks.

    The chunk is sent when the caller asked for it with
    ``stream_options.include_usage``, and it carries the token counts of the
    request and of the answer. It names the same completion as the answer it
    reports on, with the id and the model the other chunks carry.

    A tool call is not counted as text: the answer of a tool call is the call
    itself, which is reported in its own chunk.

    Args:
        chunks: The event-stream chunks of the answer.
        prompt_tokens: The number of tokens of the request.

    Yields:
        Generator[str, None, None]: The event-stream chunks, with the usage
        chunk before the last one.
    """
    counter = _TokenCounter()
    stream_id = ""
    model = ""
    created = 0
    try:
        for chunk in chunks:
            text, chunk_id, chunk_model, chunk_created = _chunk_parts(chunk)
            if chunk_id:
                stream_id = chunk_id
            if chunk_model:
                model = chunk_model
            if chunk_created and not created:
                created = chunk_created
            if chunk.startswith("data: [DONE]"):
                completion_tokens = counter.total()
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": stream_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": completion_tokens,
                                "total_tokens": prompt_tokens + completion_tokens,
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield chunk
                continue
            counter.add(text)
            yield chunk
    finally:
        # The chunks are closed with the wrapper, so that the upstream response
        # is released when the caller stops reading.
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def _chunk_parts(chunk: str) -> tuple[str, str, str, int]:
    """Read an event-stream chunk.

    Args:
        chunk: One event-stream chunk.

    Returns:
        tuple[str, str, str, int]: The text of the chunk (empty when it
        carries none), the id of the completion it belongs to, the model it
        names and the time the completion was created.
    """
    if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
        return "", "", "", 0
    try:
        body = json.loads(chunk[len("data: ") :].strip())
    except json.JSONDecodeError:
        return "", "", "", 0
    if not isinstance(body, dict):
        return "", "", "", 0
    text = ""
    choices = body.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                text += str(delta.get("content") or "")
    created = body.get("created")
    return (
        text,
        str(body.get("id") or ""),
        str(body.get("model") or ""),
        created if isinstance(created, int) else 0,
    )


def token_count(text: str) -> int:
    """Count the tokens of a text.

    A word, a group of up to three digits and a mark of punctuation are each
    one token, and a character of the Chinese, Japanese or Korean writing is
    a token of its own, because a tokeniser of a model provider gives one to
    each of them. This is close to what those tokenisers produce, without
    needing a model file or a network, so the count is the same in every
    deployment.

    Args:
        text: Core input string.

    Returns:
        int: The token count integer.
    """
    return max(1, len(_TOKEN_PIECE.findall(text)))


class _TokenCounter:
    """Count the tokens of a text that arrives in pieces.

    A word that was cut in half by the end of a piece is counted with the next
    piece, so the count does not depend on where the text was split. The same
    holds for a run of digits and for a run of characters of the Chinese,
    Japanese or Korean writing.
    """

    def __init__(self) -> None:
        """Start a count of zero."""
        self._count = 0
        self._carry = ""

    def add(self, text: str) -> None:
        """Count the tokens of a piece of text.

        Args:
            text: The piece that arrived.
        """
        text = self._carry + text
        self._carry = ""
        if text and not text[-1].isspace() and (text[-1].isalnum() or text[-1] == "_"):
            end = len(text)
            while end > 0 and (text[end - 1].isalnum() or text[end - 1] == "_"):
                end -= 1
            word = text[end:]
            if len(word) > MAX_TOKEN_LENGTH:
                # A token this long is counted as it is and not carried
                # further, so that the buffer does not grow without bound.
                self._count += len(_TOKEN_PIECE.findall(word))
                word = ""
            self._carry = word
            text = text[:end]
        self._count += len(_TOKEN_PIECE.findall(text))

    def total(self) -> int:
        """Return the number of tokens counted so far.

        Returns:
            int: The number of tokens.
        """
        return max(1, self._count + len(_TOKEN_PIECE.findall(self._carry)))


# 3. Application Factory Blueprint & 10. Trailing Slashes support
bp = Blueprint("gateway", __name__)


@bp.route("/", methods=["GET"])
def index() -> str:
    """Retrieve welcome message.

    Returns:
        str: Welcome message string.
    """
    return "one-min-ai-gateway\n"


@bp.route("/healthz", methods=["GET"])
def healthz() -> tuple[Response, int]:
    """Verify backend system and gateway operational health status.

    Returns:
        tuple[Response, int]: JSON ok and version info.
    """
    return jsonify({"ok": True, "version": gateway_version()}), 200


@bp.route("/v1/models", methods=["GET"])
def models() -> tuple[Response, int]:
    """Retrieve active list of mock/upstream models in OpenAI schema format.

    Returns:
        tuple[Response, int]: OpenAI-compatible model listing.
    """
    catalog = chat_models()
    if catalog is None:
        return error_response(
            "The model catalog is temporarily unavailable.",
            503,
            "catalog_unavailable",
        )
    return jsonify(
        {
            "object": "list",
            "data": [
                {
                    "id": model_id(entry),
                    "object": "model",
                    "created": 1727389042,
                    "owned_by": "1minai",
                }
                for entry in catalog
                if model_id(entry)
            ],
        }
    ), 200


@bp.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def chat_completions() -> tuple[Response, int] | Response:
    """OpenAI-compatible chat completions proxy endpoint supporting text, images, and streaming.

    Returns:
        tuple[Response, int] | Response: Completion output or chunked EventStream.
    """
    if request.method == "OPTIONS":
        response = make_response()
        response.headers["Access-Control-Allow-Origin"] = config.CORS_ALLOW_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return response, 204

    api_key = bearer_key()
    if not api_key:
        return error_response("Invalid Authentication", 401, "invalid_api_key")

    data = request_json()
    if isinstance(data, tuple):
        return data
    payload = build_payload(api_key, data)
    if isinstance(payload, tuple):
        return payload

    headers = {
        "API-KEY": api_key,
        "Content-Type": "application/json",
        **propagated_headers(),
    }
    model = data.get("model", config.DEFAULT_CHAT_MODEL)
    if data.get("stream"):
        # Use connection pool session
        upstream = post_upstream(
            "chat",
            chat_stream_api_url(),
            data=json.dumps(payload),
            headers=headers,
            stream=True,
            timeout=180,
            verify=config.GATEWAY_VERIFY_SSL,
        )
        if upstream.status_code != 200:
            return upstream_error(upstream)
        stream: Iterator[str] = stream_response(upstream, data, model)
        options = data.get("stream_options")
        if isinstance(options, dict) and options.get("include_usage") is True:
            stream = stream_with_usage(
                stream, token_count(payload["promptObject"]["prompt"])
            )
        return Response(stream, content_type="text/event-stream")

    # Use connection pool session
    upstream = post_upstream(
        "chat",
        chat_api_url(),
        json=payload,
        headers=headers,
        timeout=180,
        verify=config.GATEWAY_VERIFY_SSL,
    )
    if upstream.status_code != 200:
        return upstream_error(upstream)

    upstream_data = response_json(upstream)
    if upstream_data is None:
        return upstream_error(upstream)
    raw_text = extract_result_text(upstream_data)
    if raw_text is None:
        return upstream_error(upstream)
    call = parse_tool_text(raw_text, data.get("tools") or [])
    output_text = clean_text(raw_text)
    prompt_tokens = token_count(payload["promptObject"]["prompt"])
    completion_tokens = token_count(output_text)
    return jsonify(
        {
            "id": f"chatcmpl-{uuid.uuid4()}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": tool_message(call)
                    if call
                    else {"role": "assistant", "content": output_text},
                    "finish_reason": "tool_calls" if call else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def stream_response(
    upstream: requests.Response, data: dict[str, Any], model: str
) -> _ClosingStream:
    """Stream response back to client with robust tool call buffering protection and standard OpenAI formats.

    Args:
        upstream: Stream response object from 1min.ai.
        data: Core user request body.
        model: Target model indicator.

    Returns:
        _ClosingStream: The event-stream chunks. Closing it closes the upstream
        response.
    """
    return _ClosingStream(upstream, _stream_body(upstream, data, model))


class _ClosingStream:
    """Iterator over the event-stream chunks that closes the upstream response.

    Werkzeug closes the iterable it is given when the request ends. A plain
    generator runs its cleanup only once it has started, so the upstream
    response is closed here as well: this wrapper closes it even when it is
    closed before the first chunk has been read.
    """

    def __init__(
        self, upstream: requests.Response, chunks: Generator[str, None, None]
    ) -> None:
        """Store the upstream response and the chunk iterator.

        Args:
            upstream: The upstream response to close.
            chunks: The iterator producing the event-stream chunks.
        """
        self._upstream = upstream
        self._chunks = chunks
        self._closed = False

    def __iter__(self) -> _ClosingStream:
        """Return this iterator.

        Returns:
            _ClosingStream: This iterator.
        """
        return self

    def __next__(self) -> str:
        """Return the next event-stream chunk.

        The upstream response is closed when the stream ends or fails, so the
        connection is never left open.

        Returns:
            str: The next chunk.
        """
        try:
            return next(self._chunks)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Close the chunk iterator and the upstream response."""
        if self._closed:
            return
        self._closed = True
        self._chunks.close()
        self._upstream.close()


def _stream_body(
    upstream: requests.Response, data: dict[str, Any], model: str
) -> Generator[str, None, None]:
    """Yield the event-stream chunks for the upstream response."""
    buffered_text = ""
    at_line_start = True
    dropping_line = False
    tail_size = 200
    current_event = None
    stream_id = f"chatcmpl-{uuid.uuid4()}"
    tools = data.get("tools") or []
    in_tool_buffering = False

    def emit_content_chunk(content: str, finish_reason: str | None = None) -> str:
        if not content:
            return ""
        return (
            "data: "
            + json.dumps(
                {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": finish_reason,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    def emit_tool_call(call: dict[str, Any]) -> Generator[str, None, None]:
        call_payload = tool_message(call)["tool_calls"][0]
        yield (
            "data: "
            + json.dumps(
                {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": [{**call_payload, "index": 0}]},
                            "finish_reason": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )
        yield (
            "data: "
            + json.dumps(
                {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                    ],
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    for raw_line in upstream.iter_lines(decode_unicode=False):
        if raw_line == b"":
            current_event = None
            continue
        line = raw_line.decode("utf-8", errors="replace")
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
            continue
        if not line.startswith("data:"):
            continue
        raw_data = line.split(":", 1)[1].strip()
        if raw_data == "[DONE]":
            break
        if (current_event or "content") != "content":
            continue
        try:
            parsed = json.loads(raw_data)

            # 8. Robust OpenAI Stream Chunk Schema Support
            choices = parsed.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content") or ""
            else:
                content = (
                    parsed.get("content")
                    or parsed.get("delta", {}).get("content")
                    or ""
                )
        except json.JSONDecodeError:
            content = raw_data

        if content:
            buffered_text += content

            # Start buffering if tag is detected
            if not in_tool_buffering and (
                "<tool_call" in buffered_text or "<tool_code" in buffered_text
            ):
                in_tool_buffering = True

            if in_tool_buffering:
                if "</tool_call>" in buffered_text or "</tool_code>" in buffered_text:
                    call = parse_tool_text(buffered_text, tools)
                    if call:
                        yield from emit_tool_call(call)
                        # The gateway stops reading the upstream here, so the
                        # response is released now.
                        upstream.close()
                        yield "data: [DONE]\n\n"
                        return
                    else:
                        in_tool_buffering = False
                elif len(buffered_text) > 8000:
                    in_tool_buffering = False

            if not in_tool_buffering:
                # Emit every complete line. The crawl-status filter only sees
                # lines that really begin a line: the buffer can start in the
                # middle of one, and such a fragment is never a crawl-status
                # line and is never dropped.
                while True:
                    index, length = _find_line_end(buffered_text)
                    if index == -1:
                        break
                    line = buffered_text[:index]
                    terminator = buffered_text[index : index + length]
                    buffered_text = buffered_text[index + length :]
                    if dropping_line:
                        dropping_line = False
                    elif not (at_line_start and _is_crawl_line(line)):
                        chunk = emit_content_chunk(line + terminator)
                        if chunk:
                            yield chunk
                    at_line_start = True

                if dropping_line:
                    # The crawl-status line continues and its text is dropped.
                    # The last part of it is kept, so that a tool call on the
                    # same line is still detected instead of vanishing with the
                    # line.
                    if len(buffered_text) > tail_size:
                        buffered_text = buffered_text[-tail_size:]
                        at_line_start = False
                else:
                    keep = tail_size
                    if at_line_start:
                        decision = _crawl_decision(buffered_text)
                        if decision == "crawl":
                            # The line is a crawl-status line: drop it.
                            dropping_line = True
                            keep = len(buffered_text)
                        elif decision == "undecided":
                            # The line may still become a crawl-status line, so
                            # hold it back. The cap keeps the buffer bounded.
                            keep = min(len(buffered_text), CRAWL_HOLDBACK_MAX)
                    if len(buffered_text) > keep:
                        cut = len(buffered_text) - keep
                        chunk = emit_content_chunk(buffered_text[:cut])
                        if chunk:
                            yield chunk
                        buffered_text = buffered_text[cut:]
                        # The cut is inside a line, so the buffer no longer
                        # starts at a line beginning.
                        at_line_start = False

    if dropping_line:
        # The stream ended inside a crawl-status line.
        buffered_text = ""
    elif buffered_text:
        # The buffer can hold more than one line when a tool call was never
        # finished, so the crawl-status lines among them are dropped here too.
        buffered_text = _drop_crawl_lines(buffered_text, at_line_start)
    if buffered_text:
        chunk = emit_content_chunk(buffered_text)
        if chunk:
            yield chunk

    yield (
        "data: "
        + json.dumps(
            {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )
    yield "data: [DONE]\n\n"


@bp.route("/v1/images/generations", methods=["POST", "OPTIONS"])
def image_generations() -> tuple[Response, int]:
    """OpenAI-compatible image generation proxy endpoint.

    Returns:
        tuple[Response, int]: List of generated asset URLs.
    """
    if request.method == "OPTIONS":
        response = make_response()
        response.headers["Access-Control-Allow-Origin"] = config.CORS_ALLOW_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return response, 204

    api_key = bearer_key()
    if not api_key:
        return error_response("Invalid Authentication", 401, "invalid_api_key")

    data = request_json()
    if isinstance(data, tuple):
        return data
    prompt = data.get("prompt")
    if not prompt:
        return error_response("No prompt provided.", 400, "invalid_request_error")

    model = data.get("model", config.DEFAULT_IMAGE_MODEL)
    image_ids = image_model_ids()
    if image_ids is None:
        return error_response(
            "The model catalog is temporarily unavailable.",
            503,
            "catalog_unavailable",
        )
    if model not in image_ids:
        return error_response(
            f"This model does not support image generation: {model}",
            400,
            "model_not_supported",
        )

    # Use connection pool session and custom header propagation
    upstream = post_upstream(
        "images",
        feature_api_url() + "?isStreaming=false",
        json={
            "type": "IMAGE_GENERATOR",
            "model": model,
            "promptObject": {
                "prompt": prompt,
                "n": data.get("n", 1),
                "size": data.get("size", "1024x1024"),
            },
        },
        headers={
            "API-KEY": api_key,
            "Content-Type": "application/json",
            **propagated_headers(),
        },
        timeout=180,
        verify=config.GATEWAY_VERIFY_SSL,
    )
    if upstream.status_code != 200:
        return upstream_error(upstream)

    upstream_data = response_json(upstream)
    if upstream_data is None:
        return upstream_error(upstream)
    try:
        urls = upstream_data["aiRecord"]["aiRecordDetail"]["resultObject"]
    except (KeyError, TypeError):
        return upstream_error(upstream)
    if not isinstance(urls, list):
        return upstream_error(upstream)
    return jsonify(
        {"created": int(time.time()), "data": [{"url": url} for url in urls]}
    ), 200


# 6. Application Factory Pattern Refactoring
def create_app() -> Flask:
    """Flask application factory that configures and prepares the gateway instance.

    Returns:
        Flask: Configured Flask application.
    """
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    # A backstop for anything that reads the body through the framework. The
    # application reads the raw input with a limit of its own, so that a body
    # which declares no length is refused as well. The handler below answers
    # with the OpenAI error shape when the framework trips this limit.
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_REQUEST_BYTES

    # 11. Relocate global logging initialization inside create_app
    configure_logging()

    # 4. Global OPTIONS CORS Preflight interceptor
    @app.before_request
    def handle_options_preflight() -> Response | tuple[Response, int] | None:
        if request.method == "OPTIONS":
            response = make_response()
            response.headers["Access-Control-Allow-Origin"] = config.CORS_ALLOW_ORIGIN
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type,Authorization"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            return response, 204
        return None

    # 7. Request ID Correlation & Request Latency Start Hook
    @app.before_request
    def start_request() -> None:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.request_id = request_id
        g.start_time = time.time()

    # 18. Consolidate Response Headers Injection & Request Latency Logging Hook
    @app.after_request
    def log_and_headers_response(response: Response) -> Response:
        response = add_headers(response)
        start_time = getattr(g, "start_time", None)
        if start_time is not None:
            # 8. Request Duration and Latency Logging
            duration_ms = (time.time() - start_time) * 1000
            logger.info(
                "%s %s %s - completed in %.2fms",
                request.method,
                request.path,
                response.status_code,
                duration_ms,
            )
        return response

    # 5. Global Exception JSON Error Handler
    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_too_large(exc: RequestEntityTooLarge) -> tuple[Response, int]:
        """Answer a request body that is larger than the gateway accepts.

        Args:
            exc: The exception raised by the request parser.

        Returns:
            tuple[Response, int]: The OpenAI-shaped error response.
        """
        logger.warning(
            "rejected a request body larger than %d bytes", config.MAX_REQUEST_BYTES
        )
        return error_response(
            f"The request body is larger than {config.MAX_REQUEST_BYTES} bytes.",
            413,
            "request_too_large",
        )

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception) -> tuple[Response, int]:
        logger.exception("Unhandled error occurred in gateway: %s", exc)
        status_code = 500
        if isinstance(exc, HTTPException) and isinstance(exc.code, int):
            status_code = exc.code
        # The exception text stays in the log: for an unexpected error it can
        # carry internals (paths, upstream bodies), so the client gets the
        # standard reason phrase for the status only.
        message = HTTP_STATUS_CODES.get(status_code, "Internal Server Error")
        return error_response(message, status_code, "server_error")

    app.register_blueprint(bp)
    return app


# Maintain backward compatibility with WSGI imports
app = create_app()


# 15. CLI Entrypoint Script (one-min-ai-gateway)
def main() -> None:
    """CLI entrypoint function for running the gateway."""
    logger.info(
        "one-min-ai-gateway starting on %s:%s via CLI", config.HOST, config.PORT
    )
    serve(
        app,
        host=config.HOST,
        port=config.PORT,
        threads=config.WAITRESS_THREADS,
        # The limit of the server in front is a little larger than the limit of
        # the application, so that the application refuses the body and the
        # answer carries the OpenAI error shape.
        max_request_body_size=config.MAX_REQUEST_BYTES + 1024 * 1024,
    )


if __name__ == "__main__":
    main()
