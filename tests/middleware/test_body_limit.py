from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocket
from tests.types import TestClientFactory

calls: list[str] = []


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def read_body(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"{len(body)}")


def chunks(*sizes: int) -> Iterator[bytes]:
    for size in sizes:
        yield b"x" * size


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == ["/"]


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(5, 5)).text == "10"
    assert_too_large(client.post("/", content=chunks(5, 5, 1)))


def test_no_body_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").text == "0"
    assert_too_large(client.post("/", content=b"x"))


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"12345").text == "5"
    assert_too_large(client.post("/", content=b"123456"))
    assert_too_large(client.post("/missing", content=b"123456"))
    assert_too_large(client.put("/", content=b"123456"))
    assert client.put("/", content=b"1234").status_code == 405
    assert calls == ["/"]


def test_router_limit_redirect(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/a/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router, follow_redirects=False)
    assert client.post("/a", content=b"12").status_code == 307
    assert_too_large(client.post("/a", content=b"123456"))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"12345").text == "5"
    assert_too_large(client.post("/m/", content=b"123456"))
    assert client.post("/free", content=b"x" * 1000).text == "1000"


def test_mounted_asgi_app_fails_fast(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover
        await PlainTextResponse("hi")(scope, receive, send)  # pragma: no cover

    client = test_client_factory(Starlette(routes=[Mount("/m", asgi, max_body_size=5)]))
    assert_too_large(client.post("/m/", content=b"123456"))
    assert calls == []


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=3),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"123").text == "3"
    assert_too_large(client.post("/small", content=b"1234"))
    assert client.post("/other", content=b"x" * 1000).text == "1000"


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_body, methods=["POST"], max_body_size=100),
            Route("/lower", read_body, methods=["POST"], max_body_size=3),
            Route("/inherit", read_body, methods=["POST"]),
            Mount(
                "/m",
                routes=[
                    Route("/raise", read_body, methods=["POST"], max_body_size=100),
                    Route("/inherit", read_body, methods=["POST"]),
                    Mount("/r", Router([Route("/", read_body, methods=["POST"])], max_body_size=50)),
                ],
                max_body_size=20,
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # raising, for both the declared length and a streamed body
    assert client.post("/raise", content=b"x" * 50).text == "50"
    assert client.post("/raise", content=chunks(25, 25)).text == "50"
    assert_too_large(client.post("/raise", content=b"x" * 101))
    assert_too_large(client.post("/raise", content=chunks(60, 60)))
    # lowering
    assert client.post("/lower", content=b"x" * 3).text == "3"
    assert_too_large(client.post("/lower", content=b"x" * 4))
    assert_too_large(client.post("/lower", content=chunks(2, 2)))
    # inheriting
    assert client.post("/inherit", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/inherit", content=b"x" * 11))
    # mount overrides the application, route the mount, router the mount
    assert client.post("/m/inherit", content=b"x" * 20).text == "20"
    assert_too_large(client.post("/m/inherit", content=b"x" * 21))
    assert client.post("/m/raise", content=b"x" * 100).text == "100"
    assert client.post("/m/r/", content=b"x" * 50).text == "50"
    assert_too_large(client.post("/m/r/", content=b"x" * 51))
    assert_too_large(client.post("/nothing", content=b"x" * 11))
    assert client.post("/nothing", content=b"x" * 10).status_code == 404


def test_application_limit_without_route_or_method(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=5)
    client = test_client_factory(app)
    assert_too_large(client.put("/", content=b"x" * 6))
    assert_too_large(client.post("/missing", content=b"x" * 6))


def test_standalone_route_and_mount() -> None:
    route = Route("/", read_body, methods=["POST"], max_body_size=2)
    from starlette.testclient import TestClient

    assert TestClient(route).post("/", content=b"12").text == "2"
    assert_too_large(TestClient(route).post("/", content=b"123"))
    mount = Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=2)
    assert_too_large(TestClient(mount).post("/m/", content=b"123"))


async def run_asgi(
    app: Any, headers: list[tuple[bytes, bytes]], body_chunks: list[bytes], scope_type: str = "http"
) -> tuple[list[Message], list[str]]:
    sent: list[Message] = []
    events: list[str] = []
    pending = list(body_chunks)

    async def receive() -> Message:
        events.append("receive")
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": bool(pending)}
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": scope_type, "method": "POST", "path": "/", "headers": headers, "query_string": b""}
    await app(scope, receive, send)
    return sent, events


@pytest.mark.anyio
async def test_content_length_fail_fast_in_middleware() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("app")  # pragma: no cover

    sent, events = await run_asgi(
        RequestBodyLimitMiddleware(app, max_body_size=5), [(b"content-length", b"6")], [b"123456"]
    )
    assert calls == []
    assert events == []
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


@pytest.mark.anyio
async def test_fail_fast_in_starlette_before_endpoint_and_body_read() -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=5)],
        max_body_size=100,
    )
    sent, events = await run_asgi(app, [(b"content-length", b"6")], [b"123456"])
    assert sent[0]["status"] == 413
    assert calls == []
    assert events == []


@pytest.mark.anyio
async def test_bad_or_understated_content_length_does_not_let_body_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(Request(scope, receive))  # pragma: no cover

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=5)
    for value in (b"1", b"abc", b"-1", b""):
        calls.clear()
        sent, _ = await run_asgi(wrapped, [(b"content-length", value)], [b"123", b"456"])
        assert sent[0]["status"] == 413, value


@pytest.mark.anyio
async def test_standalone_middleware_with_plain_asgi_app() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        while True:
            message = await receive()
            total += len(message["body"])
            if not message["more_body"]:
                break
        await PlainTextResponse(str(total))(scope, receive, send)

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=6)
    sent, _ = await run_asgi(wrapped, [], [b"123", b"456"])
    assert sent[1]["body"] == b"6"
    sent, _ = await run_asgi(wrapped, [], [b"123", b"4567"])
    assert sent[0]["status"] == 413
    assert len(sent) == 2


@pytest.mark.anyio
async def test_swallowed_exception_still_gets_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except RequestBodyTooLarge:
            pass

    sent, _ = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=1), [], [b"123"])
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_other_error_after_exceeding_is_a_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except RequestBodyTooLarge:
            raise ValueError("wrapped")

    sent, _ = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=1), [], [b"123"])
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_unrelated_error_propagates() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=1), [], [b"1"])


@pytest.mark.anyio
async def test_response_already_started_gets_no_second_response() -> None:
    stopped: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        await receive()
        try:
            await receive()
        except RequestBodyTooLarge:
            stopped.append(True)
            raise

    sent, _ = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=4), [], [b"123", b"456", b"789"])
    assert stopped == [True]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


@pytest.mark.anyio
async def test_unrelated_error_after_response_started_propagates() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await receive()
        except RequestBodyTooLarge:
            raise ValueError("boom")

    with pytest.raises(ValueError):
        await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=1), [], [b"123"])


@pytest.mark.anyio
async def test_body_consumed_before_routing_counts_against_lowered_limit() -> None:
    async def consume(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()
        await router(scope, receive, send)

    router = Router([Route("/", read_body, methods=["POST"], max_body_size=2)])
    sent, _ = await run_asgi(RequestBodyLimitMiddleware(consume, max_body_size=100), [], [b"12345"])
    assert sent[0]["status"] == 413
    assert calls == []


@pytest.mark.anyio
async def test_non_http_scopes_unaffected() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=0)
    await wrapped({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    await wrapped({"type": "websocket"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    started: list[bool] = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        started.append(True)
        yield

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1, lifespan=lifespan)
    with test_client_factory(app) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("a long message")
            assert session.receive_text() == "a long message"
    assert started == [True]


def test_standalone_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=4)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"1234").text == "4"
    assert_too_large(client.post("/", content=b"12345"))


def test_multipart_over_limit(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> Response:
        async with request.form() as form:  # pragma: no cover
            return PlainTextResponse(str(len(form)))  # pragma: no cover

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300)
    client = test_client_factory(app)
    ok = client.post("/", files={"f": ("a.txt", b"x" * 10)})
    assert ok.text == "1"
    # the limit counts the whole multipart body, boundaries included
    too_big = client.post("/", files={"f": ("a.txt", b"x" * 290)})
    assert_too_large(too_big)
    # streamed, so that no Content-Length is available
    headers = {"content-type": "multipart/form-data; boundary=x"}
    assert_too_large(client.post("/", content=chunks(250, 250), headers=headers))


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_interrupted() -> None:
    opened: list[Any] = []

    class Spy(MultiPartParser):
        def on_headers_finished(self) -> None:
            super().on_headers_finished()
            opened.extend(self._files_to_close_on_error)

    body = b"--b\r\nContent-Disposition: form-data; name=f; filename=a.txt\r\n\r\n" + b"x" * 20

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body
        raise RequestBodyTooLarge()

    parser = Spy(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RequestBodyTooLarge):
        await parser.parse()
    assert len(opened) == 1
    assert all(f.closed for f in opened)


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_cancellation() -> None:
    import anyio

    opened: list[Any] = []

    class Spy(MultiPartParser):
        def on_headers_finished(self) -> None:
            super().on_headers_finished()
            opened.extend(self._files_to_close_on_error)

    async def stream() -> AsyncGenerator[bytes, None]:
        yield b"--b\r\nContent-Disposition: form-data; name=f; filename=a.txt\r\n\r\nabc"
        await anyio.sleep_forever()
        yield b""  # pragma: no cover

    parser = Spy(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with anyio.move_on_after(0.1):
        await parser.parse()
    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.anyio
async def test_receive_keeps_failing_once_exceeded_and_passes_disconnect() -> None:
    outcomes: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        outcomes.append((await receive())["type"])
        for _ in range(2):
            try:
                await receive()
            except RequestBodyTooLarge:
                outcomes.append("too large")

    class Server:
        messages: list[Message] = [
            {"type": "http.disconnect"},
            {"type": "http.request", "body": b"123", "more_body": True},
        ]

        async def receive(self) -> Message:
            return self.messages.pop(0)

    async def send(message: Message) -> None:
        pass

    scope = {"type": "http", "headers": []}
    await RequestBodyLimitMiddleware(app, max_body_size=1)(scope, Server().receive, send)
    assert outcomes == ["http.disconnect", "too large", "too large"]
