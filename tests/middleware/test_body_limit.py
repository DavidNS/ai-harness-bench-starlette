from __future__ import annotations

from collections.abc import Iterator
from tempfile import SpooledTemporaryFile
from typing import Any
from unittest import mock

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import BaseRoute, Host, Match, Mount, Route, Router
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.types import TestClientFactory

TOO_LARGE = "Content Too Large"


async def echo_size(request: Request) -> PlainTextResponse:
    return PlainTextResponse(str(len(await request.body())))


async def echo_size_asgi(scope: Scope, receive: Receive, send: Send) -> None:
    response = await echo_size(Request(scope, receive))
    await response(scope, receive, send)


async def never_called(request: Request) -> PlainTextResponse:
    raise AssertionError("The endpoint must not run.")  # pragma: no cover


async def call_asgi(
    app: ASGIApp,
    chunks: list[bytes],
    *,
    content_length: int | None = None,
    path: str = "/",
    method: str = "POST",
    content_type: str | None = None,
) -> list[Message]:
    """Call an ASGI app with a body sent in `chunks`, returning the messages it sent."""
    headers = [(b"host", b"testserver")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    sent: list[Message] = []

    async def receive() -> Message:
        if not pending:
            return {"type": "http.disconnect"}  # pragma: no cover
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def statuses(sent: list[Message]) -> list[int]:
    return [m["status"] for m in sent if m["type"] == "http.response.start"]


def body_of(sent: list[Message]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


# Application


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    response = client.post("/", content=b"x" * 10)
    assert (response.status_code, response.text) == (200, "10")

    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert response.headers["content-type"].startswith("text/plain")


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_zero_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["GET", "POST"])], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").status_code == 200
    assert client.post("/", content=b"x").status_code == 413


def test_negative_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        Starlette(max_body_size=-1)
    with pytest.raises(ValueError):
        Router(max_body_size=-1)
    with pytest.raises(ValueError):
        Mount("/", routes=[], max_body_size=-1)
    with pytest.raises(ValueError):
        Route("/", echo_size, max_body_size=-1)
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(Starlette(), max_body_size=-1)


def test_streamed_body_is_counted(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks(count: int) -> Iterator[bytes]:
        for _ in range(count):
            yield b"xxxx"

    assert client.post("/", content=chunks(2)).text == "8"
    response = client.post("/", content=chunks(3))
    assert (response.status_code, response.text) == (413, TOO_LARGE)


@pytest.mark.anyio
async def test_understated_and_absent_content_length_do_not_help() -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    for content_length in (None, 1, 0, 5):
        sent = await call_asgi(app, [b"x" * 6, b"x" * 6], content_length=content_length)
        assert statuses(sent) == [413]
        assert body_of(sent) == TOO_LARGE.encode()


@pytest.mark.anyio
async def test_limit_counts_bytes_after_reading_stopped() -> None:
    receives = 0

    async def endpoint(request: Request) -> PlainTextResponse:
        nonlocal receives
        async for _ in request.stream():
            receives += 1
        raise AssertionError  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    sent = await call_asgi(app, [b"x" * 6, b"x" * 6, b"x" * 6, b"x" * 6])
    assert statuses(sent) == [413]
    assert receives == 1  # The stream is stopped as soon as the limit is crossed.


@pytest.mark.anyio
async def test_receive_keeps_failing_after_the_limit() -> None:
    errors = 0

    async def endpoint(request: Request) -> PlainTextResponse:
        nonlocal errors
        for _ in range(3):
            try:
                await request.body()
            except RequestBodyTooLarge:
                errors += 1
            request._stream_consumed = False
        return PlainTextResponse("swallowed")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    sent = await call_asgi(app, [b"xxxx"])
    assert errors == 3
    # The application swallowed the error, but the client still gets the 413.
    assert statuses(sent) == [413]
    assert body_of(sent) == TOO_LARGE.encode()


@pytest.mark.anyio
async def test_response_already_started_is_cut_off() -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        raise AssertionError  # pragma: no cover

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await Request(scope, receive).body()
        await send({"type": "http.response.body", "body": b"second"})  # pragma: no cover

    sent = await call_asgi(RequestBodyLimitMiddleware(app, max_body_size=3), [b"xxxx"])
    assert statuses(sent) == [200]
    assert body_of(sent) == b"first"
    assert all(m.get("more_body") for m in sent if m["type"] == "http.response.body")


@pytest.mark.anyio
async def test_streaming_response_listening_for_disconnect() -> None:
    async def endpoint(request: Request) -> StreamingResponse:
        async def content() -> Any:
            yield b"first"  # pragma: no cover

        return StreamingResponse(content())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    sent = await call_asgi(app, [b"xxxx"])
    assert statuses(sent) == [413]


@pytest.mark.anyio
async def test_response_started_then_swallowed_error_sends_nothing_more() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        try:
            await receive()
        except RequestBodyTooLarge:
            pass
        await send({"type": "http.response.body", "body": b"more", "more_body": False})

    sent = await call_asgi(RequestBodyLimitMiddleware(app, max_body_size=1), [b"xx"])
    assert statuses(sent) == [200]
    assert body_of(sent) == b"partial"


def test_user_middleware_reading_the_body(test_client_factory: TestClientFactory) -> None:
    class ReadBody(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            await request.body()
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"])],
        middleware=[Middleware(ReadBody)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_base_http_middleware_around_the_endpoint(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"], middleware=[Middleware(Passthrough)])],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_error_handlers_do_not_see_the_rejection(test_client_factory: TestClientFactory) -> None:
    seen: list[Exception] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        seen.append(exc)  # pragma: no cover
        return PlainTextResponse("handled", status_code=500)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"])],
        exception_handlers={Exception: handler},
        max_body_size=10,
    )
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert seen == []


def test_unrelated_requests_are_unaffected(test_client_factory: TestClientFactory) -> None:
    async def websocket_endpoint(websocket: Any) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    from starlette.routing import WebSocketRoute

    events: list[str] = []

    async def lifespan(app: Starlette) -> Any:
        events.append("startup")
        yield
        events.append("shutdown")

    from contextlib import asynccontextmanager

    app = Starlette(
        routes=[Route("/", echo_size, methods=["GET"]), WebSocketRoute("/ws", websocket_endpoint)],
        lifespan=asynccontextmanager(lifespan),
        max_body_size=0,
    )
    with test_client_factory(app) as client:
        assert client.get("/").text == "0"
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_text() == "hello"
    assert events == ["startup", "shutdown"]


# Router, Mount and Route


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router([Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", echo_size, methods=["POST"]),
            Mount("/mount", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100).status_code == 200
    assert client.post("/mount/", content=b"x" * 10).status_code == 200
    response = client.post("/mount/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_mounted_asgi_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Mount("/mount", echo_size_asgi, max_body_size=10)])
    client = test_client_factory(app)
    assert client.post("/mount/", content=b"x" * 10).status_code == 200
    assert client.post("/mount/", content=b"x" * 11).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", echo_size, methods=["POST"]),
            Route("/upload", echo_size, methods=["POST"], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 100).status_code == 200
    assert client.post("/upload", content=b"x" * 10).status_code == 200
    assert client.post("/upload", content=b"x" * 11).status_code == 413


def test_route_limit_without_starlette(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Route("/", echo_size, methods=["POST"], max_body_size=10))
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_inner_limit_raises_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", echo_size, methods=["POST"]),
            Route("/upload", echo_size, methods=["POST"], max_body_size=100),
            Mount(
                "/mount",
                routes=[
                    Route("/", echo_size, methods=["POST"]),
                    Route("/upload", echo_size, methods=["POST"], max_body_size=100),
                ],
                max_body_size=20,
            ),
            Mount("/router", Router([Route("/", echo_size, methods=["POST"])], max_body_size=30)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)

    assert client.post("/small", content=b"x" * 11).status_code == 413
    assert client.post("/upload", content=b"x" * 100).status_code == 200
    assert client.post("/upload", content=b"x" * 101).status_code == 413

    assert client.post("/mount/", content=b"x" * 20).status_code == 200
    assert client.post("/mount/", content=b"x" * 21).status_code == 413
    assert client.post("/mount/upload", content=b"x" * 100).status_code == 200
    assert client.post("/mount/upload", content=b"x" * 101).status_code == 413

    assert client.post("/router/", content=b"x" * 30).status_code == 200
    assert client.post("/router/", content=b"x" * 31).status_code == 413


def test_inner_limit_lowers_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", echo_size, methods=["POST"], max_body_size=5),
            Mount(
                "/mount",
                routes=[
                    Route("/", echo_size, methods=["POST"]),
                    Route("/small", echo_size, methods=["POST"], max_body_size=3),
                ],
                max_body_size=10,
            ),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert client.post("/mount/", content=b"x" * 10).status_code == 200
    assert client.post("/mount/", content=b"x" * 11).status_code == 413
    assert client.post("/mount/small", content=b"x" * 3).status_code == 200
    assert client.post("/mount/small", content=b"x" * 4).status_code == 413


def test_mounted_application_overrides_the_outer_limit(test_client_factory: TestClientFactory) -> None:
    sub = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=50)
    plain = Starlette(routes=[Route("/", echo_size, methods=["POST"])])
    app = Starlette(routes=[Mount("/sub", sub), Mount("/plain", plain)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/sub/", content=b"x" * 50).status_code == 200
    assert client.post("/sub/", content=b"x" * 51).status_code == 413
    assert client.post("/plain/", content=b"x" * 10).status_code == 200
    assert client.post("/plain/", content=b"x" * 11).status_code == 413


def test_host_with_application(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Host("testserver", Router([Route("/", echo_size, methods=["POST"], max_body_size=50)])),
            Host("other", echo_size_asgi),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).status_code == 200
    assert client.post("/", content=b"x" * 51).status_code == 413
    client = test_client_factory(app, base_url="http://other")
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


# Content-Length


@pytest.mark.anyio
async def test_content_length_rejects_without_reading() -> None:
    app = Starlette(routes=[Route("/", never_called, methods=["POST"])], max_body_size=10)
    sent = await call_asgi(app, [], content_length=11)
    assert statuses(sent) == [413]
    assert body_of(sent) == TOO_LARGE.encode()


@pytest.mark.anyio
async def test_content_length_does_not_read_the_body() -> None:
    reads = 0

    class CountingReceive:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            async def counting() -> Message:
                nonlocal reads
                reads += 1  # pragma: no cover
                return await receive()  # pragma: no cover

            await self.app(scope, counting, send)

    app = CountingReceive(Starlette(routes=[Route("/", never_called, methods=["POST"])], max_body_size=10))
    sent = await call_asgi(app, [b"x" * 100], content_length=100)
    assert statuses(sent) == [413]
    assert reads == 0


@pytest.mark.anyio
async def test_content_length_waits_for_the_limit_that_applies() -> None:
    app = Starlette(
        routes=[
            Route("/upload", echo_size, methods=["POST"], max_body_size=100),
            Mount("/mount", routes=[Route("/upload", echo_size, methods=["POST"], max_body_size=100)]),
            Mount("/sub", Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=100)),
            Mount("/lowered", routes=[Route("/", never_called, methods=["POST"], max_body_size=5)], max_body_size=100),
        ],
        max_body_size=10,
    )
    for path in ("/upload", "/mount/upload", "/sub/"):
        sent = await call_asgi(app, [b"x" * 50], content_length=50, path=path)
        assert statuses(sent) == [200], path
        assert body_of(sent) == b"50"
        sent = await call_asgi(app, [b"x" * 101], content_length=101, path=path)
        assert statuses(sent) == [413], path

    # The limit of the route that applies is lower than the one of its mount.
    sent = await call_asgi(app, [b"x" * 6], content_length=6, path="/lowered/")
    assert statuses(sent) == [413]


@pytest.mark.anyio
async def test_content_length_of_a_redirect_is_not_checked() -> None:
    app = Starlette(
        routes=[Route("/upload/", echo_size, methods=["POST"], max_body_size=100)],
        max_body_size=10,
    )
    sent = await call_asgi(app, [], content_length=50, path="/upload")
    assert statuses(sent) == [307]


@pytest.mark.anyio
async def test_content_length_of_unmatched_requests() -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    assert statuses(await call_asgi(app, [], content_length=10, path="/missing")) == [404]
    assert statuses(await call_asgi(app, [], content_length=11, path="/missing")) == [413]


@pytest.mark.anyio
async def test_content_length_of_a_custom_default() -> None:
    called = False

    async def default(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True  # pragma: no cover

    app = Router(default=default, max_body_size=10)
    assert statuses(await call_asgi(app, [], content_length=11)) == [413]
    assert not called


@pytest.mark.anyio
async def test_invalid_content_length_is_ignored() -> None:
    app = RequestBodyLimitMiddleware(echo_size_asgi, max_body_size=10)
    for value in (b"abc", b"-1", b"1\xb2", b"", b" 5 "):
        scope_headers = [(b"content-length", value)]
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"hello", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": scope_headers}
        await app(scope, receive, send)
        assert statuses(sent) == [200], value

    # Several different values are ambiguous, the bytes are what counts.
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"50")] * 1}
    scope["headers"] = [(b"content-length", b"50"), (b"content-length", b"5")]
    sent = []
    await app(scope, receive, send)
    assert statuses(sent) == [200]

    # A huge number of digits is a huge length.
    sent = []
    scope["headers"] = [(b"content-length", b"9" * 5000)]
    await app(scope, receive, send)
    assert statuses(sent) == [413]


# Standalone middleware


def test_middleware_with_asgi_app(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(echo_size_asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_middleware_without_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(echo_size_asgi, max_body_size=None))
    assert client.post("/", content=b"x" * 1000).text == "1000"


def test_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_middleware_is_a_ceiling_for_the_routes(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"], max_body_size=100)])
    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


@pytest.mark.anyio
async def test_middleware_content_length_and_streamed_bytes() -> None:
    app = RequestBodyLimitMiddleware(echo_size_asgi, max_body_size=10)
    sent = await call_asgi(app, [], content_length=11)
    assert (statuses(sent), body_of(sent)) == ([413], TOO_LARGE.encode())
    sent = await call_asgi(app, [b"x" * 6, b"x" * 6], content_length=3)
    assert (statuses(sent), body_of(sent)) == ([413], TOO_LARGE.encode())
    sent = await call_asgi(app, [b"x" * 5, b"x" * 5])
    assert (statuses(sent), body_of(sent)) == ([200], b"10")


@pytest.mark.anyio
async def test_middleware_ignores_other_scopes() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestBodyLimitMiddleware(app, max_body_size=0)
    for scope_type in ("websocket", "lifespan"):
        await middleware({"type": scope_type}, None, None)  # type: ignore[arg-type]
    assert seen == ["websocket", "lifespan"]


@pytest.mark.anyio
async def test_other_exceptions_are_not_hidden() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        raise RuntimeError("boom")

    middleware = RequestBodyLimitMiddleware(app, max_body_size=1)
    with pytest.raises(RuntimeError, match="boom"):
        await call_asgi(middleware, [b"x"])

    # Once the limit is crossed the client still gets its 413.
    sent: list[Message] = []

    async def failing_after_limit(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except RequestBodyTooLarge:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        sent = await call_asgi(RequestBodyLimitMiddleware(failing_after_limit, max_body_size=1), [b"xx"])
    assert sent == []


# Multipart


BOUNDARY = "a7f7ac8d4e2e437c877bb7b8d7cc549c"
MULTIPART_HEADERS = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


def multipart_body(file_size: int) -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n" + "x" * file_size + f"\r\n--{BOUNDARY}--\r\n"
    ).encode()


async def upload(request: Request) -> PlainTextResponse:
    async with request.form() as form:
        file = form["file"]
        assert not isinstance(file, str)
        return PlainTextResponse(str(file.size))


def test_multipart_counts_the_whole_body(test_client_factory: TestClientFactory) -> None:
    body = multipart_body(100)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body))
    client = test_client_factory(app)
    response = client.post("/", content=body, headers=MULTIPART_HEADERS)
    assert (response.status_code, response.text) == (200, "100")

    # The file itself is below the limit: the encoding overhead is what crosses it.
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body) - 1)
    client = test_client_factory(app)
    response = client.post("/", content=body, headers=MULTIPART_HEADERS)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


@pytest.mark.anyio
async def test_multipart_closes_tempfiles_at_the_limit() -> None:
    closed: list[SpooledTemporaryFile[bytes]] = []
    created: list[SpooledTemporaryFile[bytes]] = []

    class TrackedFile(SpooledTemporaryFile[bytes]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

        def close(self) -> None:
            closed.append(self)
            super().close()

    async def endpoint(request: Request) -> PlainTextResponse:
        await request.form()
        raise AssertionError  # pragma: no cover

    body = multipart_body(1000)
    # The file is created by the second chunk, and the limit is crossed by the fourth one.
    chunks = [body[i : i + 100] for i in range(0, len(body), 100)]
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=350)
    with mock.patch("starlette.formparsers.SpooledTemporaryFile", TrackedFile):
        sent = await call_asgi(app, chunks, content_type=MULTIPART_HEADERS["Content-Type"])
    assert statuses(sent) == [413]
    assert len(created) == 1
    assert closed == created


@pytest.mark.anyio
@pytest.mark.parametrize("error", [RequestBodyTooLarge, KeyboardInterrupt, OSError])
async def test_multipart_closes_tempfiles_when_interrupted(error: type[BaseException]) -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    closed: list[bool] = []

    class TrackedFile(SpooledTemporaryFile[bytes]):
        def close(self) -> None:
            closed.append(True)
            super().close()

    async def stream() -> Any:
        yield multipart_body(10)[:200]
        raise error()

    parser = MultiPartParser(Headers(MULTIPART_HEADERS), stream())
    with mock.patch("starlette.formparsers.SpooledTemporaryFile", TrackedFile):
        with pytest.raises(error):
            await parser.parse()
    assert closed == [True]


def test_inner_limit_below_what_was_already_received(test_client_factory: TestClientFactory) -> None:
    class ReadBody(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            await request.body()
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", never_called, methods=["POST"], max_body_size=5)],
        middleware=[Middleware(ReadBody)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 6)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


# Wrapped applications, middleware reading the body, and the shape of the rejection


class Wrapper:
    """A plain ASGI middleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


class ReadBody(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        await request.body()
        return await call_next(request)


def raised_app(**kwargs: Any) -> Starlette:
    return Starlette(routes=[Route("/upload", echo_size, methods=["POST"], max_body_size=100)], **kwargs)


@pytest.mark.anyio
async def test_content_length_behind_wrapped_applications() -> None:
    inner = Router([Route("/upload", echo_size, methods=["POST"], max_body_size=100)])
    cases = [
        (Mount("/sub", Wrapper(raised_app())), "/sub/upload"),
        (Mount("/sub", Wrapper(inner)), "/sub/upload"),
        (Host("testserver", Wrapper(raised_app())), "/upload"),
        (Host("testserver", Wrapper(inner)), "/upload"),
        (Route("/upload", raised_app()), "/upload"),
        (Route("/upload", Wrapper(raised_app())), "/upload"),
    ]
    for route, path in cases:
        app = Starlette(routes=[route], max_body_size=10)
        sent = await call_asgi(app, [b"x" * 50], content_length=50, path=path)
        assert (statuses(sent), body_of(sent)) == ([200], b"50"), route
        sent = await call_asgi(app, [b"x" * 101], content_length=101, path=path)
        assert statuses(sent) == [413], route


@pytest.mark.anyio
async def test_content_length_of_a_plain_mounted_application_is_not_read() -> None:
    reads = 0

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reads
        reads += 1  # pragma: no cover

    app = Starlette(routes=[Mount("/sub", Wrapper(asgi))], max_body_size=10)
    sent = await call_asgi(app, [], content_length=11, path="/sub/")
    assert (statuses(sent), reads) == ([413], 0)


class Opaque:
    """An application that may set limits of its own: the declared length waits for its first read."""

    def _survey_body_limit(self, inherited: int | None, survey: Any) -> None:
        survey.limits.add(inherited)  # pragma: no cover

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        await PlainTextResponse(str(len(message["body"])))(scope, receive, send)


@pytest.mark.anyio
async def test_content_length_check_on_first_read() -> None:
    app = Starlette(routes=[Mount("/sub", Opaque())], max_body_size=10)
    sent = await call_asgi(app, [b"xxxxx"], content_length=11, path="/sub/")
    assert (statuses(sent), body_of(sent)) == ([413], TOO_LARGE.encode())
    sent = await call_asgi(app, [b"xxxxx"], content_length=5, path="/sub/")
    assert (statuses(sent), body_of(sent)) == ([200], b"5")


def test_raised_route_limit_with_middleware_reading_the_body(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/upload", echo_size, methods=["POST"], max_body_size=100),
            Route("/small", echo_size, methods=["POST"]),
            Mount("/mount", routes=[Route("/", echo_size, methods=["POST"], max_body_size=60)]),
        ],
        middleware=[Middleware(ReadBody)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/upload", content=b"x" * 50).text == "50"
    assert client.post("/upload", content=b"x" * 101).status_code == 413
    assert client.post("/mount/", content=b"x" * 60).text == "60"
    # What the middleware read is counted against the limit of the route that was chosen.
    assert client.post("/small", content=b"x" * 11).status_code == 413
    assert client.post("/mount/", content=b"x" * 61).status_code == 413


def test_unlimited_application_with_a_limited_route_counts_what_middleware_read(
    test_client_factory: TestClientFactory,
) -> None:
    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"], max_body_size=10), Route("/free", echo_size, methods=["POST"])],
        middleware=[Middleware(ReadBody)],
    )
    client = test_client_factory(app)
    assert client.post("/free", content=b"x" * 1000).text == "1000"
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_error_handlers_do_not_see_a_rejection_in_middleware(test_client_factory: TestClientFactory) -> None:
    seen: list[Exception] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        seen.append(exc)  # pragma: no cover
        return PlainTextResponse("handled", status_code=500)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(ReadBody)],
        exception_handlers={Exception: handler},
        max_body_size=10,
    )
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 101)
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert seen == []


@pytest.mark.anyio
async def test_mounted_application_error_handlers_do_not_see_the_rejection() -> None:
    seen: list[Exception] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        seen.append(exc)  # pragma: no cover
        return PlainTextResponse("handled", status_code=500)  # pragma: no cover

    sub = Starlette(
        routes=[Route("/", echo_size, methods=["POST"], max_body_size=5)],
        exception_handlers={Exception: handler},
    )
    app = Starlette(routes=[Mount("/sub", sub)], max_body_size=100)
    sent = await call_asgi(app, [b"x" * 6], path="/sub/")
    assert (statuses(sent), body_of(sent)) == ([413], TOO_LARGE.encode())
    assert seen == []


class HoldStart:
    """Holds the response start until the first body, and refuses a second start."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        held: Message | None = None
        started = False

        async def hold(message: Message) -> None:
            nonlocal held, started
            if message["type"] == "http.response.start":
                if started:  # pragma: no cover
                    raise RuntimeError("response already started")
                started = True
                held = message
                return
            if held is not None:  # pragma: no cover
                await send(held)
                held = None
            await send(message)  # pragma: no cover

        await self.app(scope, receive, hold)


@pytest.mark.anyio
async def test_route_limit_behind_a_middleware_holding_the_response_start() -> None:
    async def endpoint(request: Request) -> StreamingResponse:
        async def content() -> Any:
            await request.body()
            yield b"never"  # pragma: no cover

        return StreamingResponse(content())

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"], max_body_size=10)],
        middleware=[Middleware(HoldStart)],
    )
    sent = await call_asgi(app, [b"x" * 11])
    assert (statuses(sent), body_of(sent)) == ([413], TOO_LARGE.encode())


@pytest.mark.anyio
async def test_gzip_streaming_rejection_starts_the_response() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    async def endpoint(request: Request) -> StreamingResponse:
        async def content() -> Any:
            yield b"a" * 10
            await request.body()
            yield b"b"  # pragma: no cover

        return StreamingResponse(content())

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"])],
        middleware=[Middleware(GZipMiddleware, minimum_size=1)],
        max_body_size=3,
    )
    sent = await call_asgi(app, [b"xxxx"])
    assert sent[0]["type"] == "http.response.start"
    assert statuses(sent) == [200] or statuses(sent) == [413]
    assert len(statuses(sent)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("content_length", [None, 6])
async def test_route_limit_behind_base_http_middleware_completes_the_response(content_length: int | None) -> None:
    import anyio

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"], max_body_size=5)],
        middleware=[Middleware(Passthrough)],
        max_body_size=100,
    )
    sent: list[Message] = []

    async def slow_send(message: Message) -> None:
        await anyio.sleep(0.001)
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "root_path": "",
        "query_string": b"",
        "headers": [] if content_length is None else [(b"content-length", str(content_length).encode())],
    }
    chunks = [{"type": "http.request", "body": b"x" * 6, "more_body": False}]

    async def receive() -> Message:
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    await app(scope, receive, slow_send)
    assert statuses(sent) == [413]
    assert body_of(sent) == TOO_LARGE.encode()
    assert sent[-1]["type"] == "http.response.body" and not sent[-1].get("more_body", False)


def test_exceptions_with_an_exceptions_attribute_are_left_alone(test_client_factory: TestClientFactory) -> None:
    class Odd(Exception):
        exceptions = 3

    async def boom(request: Request) -> PlainTextResponse:
        raise Odd("boom")

    app = Starlette(routes=[Route("/", boom, methods=["POST"])], max_body_size=1)
    client = test_client_factory(app, raise_server_exceptions=True)
    with pytest.raises(Odd):
        client.post("/", content=b"")
    app = Starlette(routes=[Route("/", boom, methods=["POST"])], max_body_size=1)
    with pytest.raises(Odd):
        test_client_factory(app).post("/", content=b"x")


@pytest.mark.anyio
async def test_content_length_with_leading_zeros() -> None:
    app = RequestBodyLimitMiddleware(echo_size_asgi, max_body_size=10)
    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"0" * 2000 + b"5")]}
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"hello", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    assert statuses(sent) == [200]
    scope["headers"] = [(b"content-length", b"0" * 2000)]
    sent.clear()
    await app(scope, receive, send)
    assert statuses(sent) == [200]


def test_invalid_limit_types_are_rejected() -> None:
    for bad in (float("nan"), 1.5, "10", True):
        with pytest.raises(TypeError):
            Starlette(max_body_size=bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            RequestBodyLimitMiddleware(Starlette(), max_body_size=bad)  # type: ignore[arg-type]


# Routes that do not apply a limit themselves, cycles


class CustomRoute(BaseRoute):
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        return (Match.FULL, {}) if scope["path"] == "/custom" else (Match.NONE, {})

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await echo_size_asgi(scope, receive, send)


class QuietRoute(Route):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


@pytest.mark.anyio
@pytest.mark.parametrize("route", [CustomRoute(), QuietRoute("/custom", echo_size, methods=["POST"])])
async def test_routes_without_their_own_limit_keep_the_application_limit(route: BaseRoute) -> None:
    app = Starlette(
        routes=[route, Route("/upload", echo_size, methods=["POST"], max_body_size=100_000)],
        max_body_size=10,
    )
    sent = await call_asgi(app, [b"x" * 10], path="/custom")
    assert (statuses(sent), body_of(sent)) == ([200], b"10")
    for content_length in (None, 1000):
        sent = await call_asgi(app, [b"x" * 500, b"x" * 500], content_length=content_length, path="/custom")
        assert statuses(sent) == [413]
    sent = await call_asgi(app, [b"x" * 1000], path="/upload")
    assert statuses(sent) == [200]


@pytest.mark.anyio
async def test_unknown_route_is_not_called_for_a_declared_length_over_the_limit() -> None:
    app = Starlette(routes=[CustomRoute()], max_body_size=10)
    sent = await call_asgi(app, [], content_length=11, path="/custom")
    assert statuses(sent) == [413]


@pytest.mark.anyio
async def test_scope_is_not_kept_in_a_reference_cycle() -> None:
    import gc
    import weakref

    class WeakScope(dict):  # type: ignore[type-arg]
        """A dict can not be weakly referenced."""

    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    scope: Any = WeakScope(type="http", method="POST", path="/", root_path="", query_string=b"", headers=[])

    async def receive() -> Message:
        return {"type": "http.request", "body": b"abc", "more_body": False}

    async def send(message: Message) -> None:
        pass

    gc.collect()
    gc.disable()
    try:
        await app(scope, receive, send)
        ref = weakref.ref(scope)
        del scope
        assert ref() is None
    finally:
        gc.enable()


def test_applications_referring_back_to_each_other(test_client_factory: TestClientFactory) -> None:
    class View:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await PlainTextResponse("view")(scope, receive, send)

    for limit in (None, 10):
        app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=limit)
        app.router.routes.append(Mount("/view", View(app)))
        client = test_client_factory(app)
        assert client.post("/", content=b"abc").text == "3"
        assert client.get("/view/").text == "view"


# Middleware reading the body is held to the limit of the route the request is headed for


async def pulled_bytes(app: ASGIApp, path: str, chunk: int, count: int, method: str = "POST") -> tuple[list[int], int]:
    pulled = 0
    remaining = count
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
    }

    async def receive() -> Message:
        nonlocal pulled, remaining
        if remaining == 0:  # pragma: no cover
            return {"type": "http.disconnect"}
        remaining -= 1
        pulled += chunk
        return {"type": "http.request", "body": b"x" * chunk, "more_body": remaining > 0}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return statuses(sent), pulled


@pytest.mark.anyio
async def test_middleware_stops_reading_at_the_limit_of_the_route_it_is_headed_for() -> None:
    app = Starlette(
        routes=[
            Route("/comments", echo_size, methods=["POST"]),
            Route("/upload", echo_size, methods=["POST"], max_body_size=1_000_000),
            Mount(
                "/mount",
                routes=[Route("/small", echo_size, methods=["POST"], max_body_size=500)],
                max_body_size=2000,
            ),
            Mount("/wrapped", Wrapper(Router([Route("/", echo_size, methods=["POST"])], max_body_size=300))),
        ],
        middleware=[Middleware(ReadBody)],
        max_body_size=1000,
    )
    assert await pulled_bytes(app, "/comments", 1000, 500) == ([413], 2000)
    assert await pulled_bytes(app, "/mount/small", 500, 1000) == ([413], 1000)
    assert await pulled_bytes(app, "/wrapped/", 300, 1000) == ([413], 600)
    # The wrong method gets a 405 from the router: it is held to the limit of the application.
    assert await pulled_bytes(app, "/comments", 1000, 500, method="PUT") == ([413], 2000)
    assert await pulled_bytes(app, "/upload", 1000, 500, method="PUT") == ([413], 2000)
    # A path that matches nothing is held to the limit of the application, whatever other routes raise.
    assert await pulled_bytes(app, "/missing", 1000, 500) == ([413], 2000)
    # The raised route still reads what it accepts.
    assert (await pulled_bytes(app, "/upload", 1000, 500))[0] == [200]


@pytest.mark.anyio
async def test_standalone_router_resolves_the_limit_of_the_route() -> None:
    router = Router(
        [
            Route("/small", echo_size, methods=["POST"]),
            Route("/big", echo_size, methods=["POST"], max_body_size=100_000),
        ],
        middleware=[Middleware(ReadBody)],
        max_body_size=1000,
    )
    assert await pulled_bytes(router, "/small", 1000, 500) == ([413], 2000)


class StripPrefixAndBuffer:
    """Strips an `/api` prefix from the path on a copy of the scope, and buffers the body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope = dict(scope, path=scope["path"][4:])
        body = await Request(scope, receive).body()
        done = False

        async def replay() -> Message:
            nonlocal done
            if done:  # pragma: no cover
                return {"type": "http.disconnect"}
            done = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


@pytest.mark.anyio
async def test_path_rewriting_middleware_reading_the_body_needs_a_limit_for_the_external_path() -> None:
    upload = Route("/upload", echo_size, methods=["POST"], max_body_size=100)
    # Routing has not seen the rewrite when the middleware reads: the path the client sent decides.
    app = Starlette(routes=[upload], middleware=[Middleware(StripPrefixAndBuffer)], max_body_size=10)
    assert statuses(await call_asgi(app, [b"x" * 50], path="/api/upload")) == [413]
    # A mount on the external prefix gives that path its own limit.
    app = Starlette(
        routes=[upload, Mount("/api", routes=[], max_body_size=100)],
        middleware=[Middleware(StripPrefixAndBuffer)],
        max_body_size=10,
    )
    sent = await call_asgi(app, [b"x" * 50], path="/api/upload")
    assert (statuses(sent), body_of(sent)) == ([200], b"50")
    assert statuses(await call_asgi(app, [b"x" * 101], path="/api/upload")) == [413]


def test_routing_errors_reach_the_error_handlers_with_a_raised_route_limit(
    test_client_factory: TestClientFactory,
) -> None:
    from starlette.convertors import Convertor, register_url_convertor

    class Picky(Convertor[str]):
        regex = "[a-z]+"

        def convert(self, value: str) -> str:
            raise ValueError(value)

        def to_string(self, value: str) -> str:  # pragma: no cover
            return value

    register_url_convertor("picky", Picky())
    seen: list[str] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        seen.append(type(exc).__name__)
        return PlainTextResponse("handled", status_code=500)

    app = Starlette(
        routes=[
            Route("/items/{name:picky}", echo_size, methods=["POST"]),
            Route("/upload", echo_size, methods=["POST"], max_body_size=100),
        ],
        exception_handlers={Exception: handler},
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.post("/items/bad", content=b"abc")
    assert (response.status_code, response.text) == (500, "handled")
    assert seen == ["ValueError"]


@pytest.mark.anyio
async def test_standalone_router_routing_errors_are_not_swallowed() -> None:
    from starlette.convertors import Convertor, register_url_convertor

    class Broken(Convertor[str]):
        regex = "[a-z]+"

        def convert(self, value: str) -> str:
            raise ValueError(value)

        def to_string(self, value: str) -> str:  # pragma: no cover
            return value

    register_url_convertor("broken", Broken())
    router = Router(
        [
            Route("/{name:broken}", echo_size, methods=["POST"]),
            Route("/up", echo_size, methods=["POST"], max_body_size=100),
        ],
        max_body_size=10,
    )
    with pytest.raises(ValueError):
        await call_asgi(router, [b"abc"], path="/bad")


@pytest.mark.anyio
async def test_routing_errors_do_not_raise_the_limit_before_routing() -> None:
    from starlette.convertors import Convertor, register_url_convertor

    class Picky(Convertor[str]):
        regex = "[a-z]+"

        def convert(self, value: str) -> str:
            raise ValueError(value)

        def to_string(self, value: str) -> str:  # pragma: no cover
            return value

    register_url_convertor("picky2", Picky())
    app = Starlette(
        routes=[
            Route("/items/{name:picky2}", echo_size, methods=["POST"]),
            Route("/upload", echo_size, methods=["POST"], max_body_size=1_000_000),
        ],
        middleware=[Middleware(ReadBody)],
        max_body_size=1000,
    )
    assert await pulled_bytes(app, "/items/bad", 1000, 500) == ([413], 2000)


def test_slash_redirect_to_a_raised_route_with_middleware_reading_the_body(
    test_client_factory: TestClientFactory,
) -> None:
    app = Starlette(
        routes=[
            Route("/upload/", echo_size, methods=["POST"], max_body_size=100),
            Route("/plain", echo_size, methods=["POST"]),
        ],
        middleware=[Middleware(ReadBody)],
        max_body_size=10,
    )
    client = test_client_factory(app, follow_redirects=False)
    assert client.post("/upload", content=b"x" * 50).status_code == 307
    assert client.post("/upload", content=b"x" * 101).status_code == 413
    # The other form of a path without a raised route has the limit of the application.
    assert client.post("/plain/", content=b"x" * 50).status_code == 413
    assert client.post("/plain/", content=b"x" * 5).status_code == 307
    # The redirect is only a hint of where the path goes: a path with no such route gets the application's limit.
    assert client.post("/nothing", content=b"x" * 50).status_code == 413
    assert client.post("/", content=b"x" * 50).status_code == 413
