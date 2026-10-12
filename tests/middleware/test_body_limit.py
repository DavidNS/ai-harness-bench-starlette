from __future__ import annotations

from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_all(request: Request) -> PlainTextResponse:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


async def raw_read_all(scope: Scope, receive: Receive, send: Send) -> None:
    total = 0
    more_body = True
    while more_body:
        message = await receive()
        total += len(message.get("body", b""))
        more_body = message.get("more_body", False)
    await PlainTextResponse(str(total))(scope, receive, send)


def make_app(max_body_size: int) -> RequestBodyLimitMiddleware:
    return RequestBodyLimitMiddleware(raw_read_all, max_body_size=max_body_size)


async def drive(
    app: Any,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
) -> tuple[list[Message], list[str]]:
    """Run an ASGI app with a scripted body; returns sent messages and the names of receive calls."""
    pending = list(chunks)
    sent: list[Message] = []
    calls: list[str] = []

    async def receive() -> Message:
        calls.append("receive")
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": scope_type, "method": "POST", "path": "/", "headers": headers or []}
    await app(scope, receive, send)
    return sent, calls


def status_of(sent: list[Message]) -> int:
    return sent[0]["status"]


def test_over_limit_with_content_length(test_client_factory: TestClientFactory) -> None:
    ran = False

    async def endpoint(request: Request) -> PlainTextResponse:
        nonlocal ran
        ran = True
        return PlainTextResponse("ok")  # pragma: no cover

    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", endpoint, methods=["POST"])]), max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Content Too Large"
    assert not ran


def test_within_limit_and_exactly_at_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app(10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=b"x" * 3).text == "3"
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_streaming_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app(10))

    def gen(n: int) -> Any:
        for _ in range(n):
            yield b"xxxx"

    assert "content-length" not in client.build_request("POST", "/", content=gen(1)).headers
    assert client.post("/", content=gen(2)).text == "8"
    assert client.post("/", content=gen(3)).status_code == 413


@pytest.mark.anyio
async def test_understated_content_length() -> None:
    sent, _ = await drive(make_app(10), [b"x" * 20], headers=[(b"content-length", b"5")])
    assert status_of(sent) == 413
    assert sent[1]["body"] == b"Content Too Large"


@pytest.mark.anyio
async def test_invalid_content_length_is_ignored() -> None:
    sent, _ = await drive(make_app(10), [b"x" * 4], headers=[(b"content-length", b"abc")])
    assert status_of(sent) == 200
    sent, _ = await drive(make_app(10), [b"x" * 40], headers=[(b"content-length", b"abc")])
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_declared_content_length_rejected_without_receive() -> None:
    sent, calls = await drive(make_app(10), [b"x" * 20], headers=[(b"content-length", b"20")])
    assert status_of(sent) == 413
    assert calls == []


def test_multipart_upload(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        async with request.form() as form:
            upload = form["file"]
            data = await upload.read()  # type: ignore[union-attr]
        return PlainTextResponse(str(len(data)))

    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", endpoint, methods=["POST"])]), max_body_size=300)
    client = test_client_factory(app)
    assert client.post("/", files={"file": ("a.txt", b"x" * 50)}).text == "50"
    # The file data alone fits in the limit, but boundaries and headers push the body over it.
    assert client.post("/", files={"file": ("a.txt", b"x" * 290)}).status_code == 413


@pytest.mark.anyio
async def test_no_second_response_when_response_started() -> None:
    raised: list[BaseException] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await receive()
        except RequestBodyTooLarge as exc:
            raised.append(exc)
            raise

    sent, _ = await drive(RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"def"])
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert sent[0]["status"] == 200
    assert len(raised) == 1
    assert raised[0].max_body_size == 5  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_receive_keeps_raising_and_passes_disconnect() -> None:
    results: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        results.append((await receive())["type"])
        try:
            await receive()
        except RequestBodyTooLarge:
            results.append("raised")
        try:
            await receive()
        except RequestBodyTooLarge:
            results.append("raised again")
            raise

    sent, _ = await drive(RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"defgh"])
    assert results == ["http.request", "raised", "raised again"]
    assert status_of(sent) == 413

    results.clear()

    async def disconnect_app(scope: Scope, receive: Receive, send: Send) -> None:
        results.append((await receive())["type"])

    await drive(RequestBodyLimitMiddleware(disconnect_app, max_body_size=5), [])
    assert results == ["http.disconnect"]


@pytest.mark.anyio
async def test_inner_limiter_overrides_outer() -> None:
    raised = RequestBodyLimitMiddleware(make_app(1000), max_body_size=10)
    sent, _ = await drive(raised, [b"x" * 500])
    assert status_of(sent) == 200

    lowered = RequestBodyLimitMiddleware(make_app(10), max_body_size=1000)
    sent, _ = await drive(lowered, [b"x" * 500])
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_non_http_scope_passes_through() -> None:
    seen: list[Any] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((scope["type"], receive, send))

    middleware = RequestBodyLimitMiddleware(app, max_body_size=1)

    async def receive() -> Message:  # pragma: no cover
        raise AssertionError

    async def send(message: Message) -> None:  # pragma: no cover
        raise AssertionError

    await middleware({"type": "websocket"}, receive, send)
    assert seen == [("websocket", receive, send)]


def test_pure_asgi_and_no_new_exports() -> None:
    import starlette
    import starlette.middleware

    assert not issubclass(RequestBodyLimitMiddleware, BaseHTTPMiddleware)
    assert not hasattr(starlette, "RequestBodyLimitMiddleware")
    assert not hasattr(starlette.middleware, "RequestBodyLimitMiddleware")
