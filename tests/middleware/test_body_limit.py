from __future__ import annotations

from collections.abc import Iterator

import pytest

from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_all(scope: Scope, receive: Receive, send: Send) -> None:
    request = Request(scope, receive)
    body = await request.body()
    await PlainTextResponse(str(len(body)))(scope, receive, send)


def test_no_limit_allows_any_body(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(read_all))
    assert client.post("/", content=b"x" * 10_000).text == "10000"


def test_body_within_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))
    response = client.post("/", content=b"x" * 10)
    assert response.status_code == 200
    assert response.text == "10"


def test_body_over_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_chunked_body_without_content_length_is_counted(test_client_factory: TestClientFactory) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 6
        yield b"x" * 6

    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))
    response = client.post("/", content=chunks())
    assert "content-length" not in response.request.headers
    assert response.status_code == 413


def test_declared_content_length_fails_fast(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))
    response = client.post("/", content=b"x" * 11)
    assert response.request.headers["content-length"] == "11"
    assert response.status_code == 413
    assert response.text == "Content Too Large"


@pytest.mark.anyio
async def test_declared_content_length_is_rejected_before_body_is_read() -> None:
    raw_receive_calls = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal raw_receive_calls
        raw_receive_calls += 1  # pragma: no cover
        return {"type": "http.request", "body": b"x" * 11}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"11")]}
    await RequestBodyLimitMiddleware(read_all, max_body_size=10)(scope, receive, send)
    assert raw_receive_calls == 0
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


async def collect_http(
    app: ASGIApp, headers: list[tuple[bytes, bytes]], bodies: list[bytes]
) -> tuple[list[Message], None]:
    incoming: list[Message] = [
        {"type": "http.request", "body": body, "more_body": i < len(bodies) - 1} for i, body in enumerate(bodies)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    await app(scope, receive, send)
    return sent, None


@pytest.mark.anyio
async def test_understated_content_length_is_enforced() -> None:
    sent, _ = await collect_http(
        RequestBodyLimitMiddleware(read_all, max_body_size=10),
        [(b"content-length", b"3")],
        [b"x" * 8, b"x" * 8],
    )
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


@pytest.mark.anyio
async def test_non_ascii_digit_content_length_is_ignored() -> None:
    sent, _ = await collect_http(
        RequestBodyLimitMiddleware(read_all, max_body_size=10),
        [(b"content-length", b"\xb2")],
        [b"x" * 5],
    )
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_declared_content_length_replaces_response_of_app_that_never_reads() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("hello")(scope, receive, send)

    sent, _ = await collect_http(
        RequestBodyLimitMiddleware(app, max_body_size=10),
        [(b"content-length", b"11")],
        [b"x" * 11],
    )
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


@pytest.mark.anyio
async def test_invalid_content_length_is_ignored() -> None:
    sent, _ = await collect_http(
        RequestBodyLimitMiddleware(read_all, max_body_size=10),
        [(b"content-length", b"abc")],
        [b"x" * 5],
    )
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_response_already_started_sends_no_second_response() -> None:
    seen: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        message = await receive()
        seen.append(message)
        message = await receive()
        seen.append(message)
        await send({"type": "http.response.body", "body": b"late", "more_body": False})

    sent, _ = await collect_http(RequestBodyLimitMiddleware(app, max_body_size=5), [], [b"x" * 6])
    assert [m["type"] for m in seen] == ["http.disconnect", "http.disconnect"]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"] == b"partial"


@pytest.mark.anyio
async def test_client_disconnect_after_limit_is_swallowed() -> None:
    sent, _ = await collect_http(RequestBodyLimitMiddleware(read_all, max_body_size=5), [], [b"x" * 6])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_client_disconnect_without_limit_propagates() -> None:
    with pytest.raises(ClientDisconnect):
        await collect_http(RequestBodyLimitMiddleware(read_all, max_body_size=5), [], [])


@pytest.mark.anyio
async def test_non_http_scope_passes_through() -> None:
    calls: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope["type"])

    async def receive() -> Message:
        raise AssertionError  # pragma: no cover

    async def send(message: Message) -> None:
        raise AssertionError  # pragma: no cover

    await RequestBodyLimitMiddleware(app, max_body_size=1)({"type": "lifespan"}, receive, send)
    assert calls == ["lifespan"]


@pytest.mark.anyio
async def test_nested_middleware_overrides_limit() -> None:
    raised = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(read_all, max_body_size=20), max_body_size=5)
    sent, _ = await collect_http(raised, [], [b"x" * 10])
    assert sent[0]["status"] == 200

    lowered = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(read_all, max_body_size=5), max_body_size=20)
    sent, _ = await collect_http(lowered, [], [b"x" * 10])
    assert sent[0]["status"] == 413

    declared = [(b"content-length", b"10")]
    sent, _ = await collect_http(lowered, declared, [b"x" * 10])
    assert sent[0]["status"] == 413
    assert len(sent) == 2

    sent, _ = await collect_http(raised, declared, [b"x" * 10])
    assert sent[0]["status"] == 200
