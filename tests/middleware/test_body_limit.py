from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def read(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    calls.clear()


def assert_too_large(response) -> None:  # type: ignore[no-untyped-def]
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert client.get("/", headers={}).status_code == 405


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 6
        yield b"x" * 6

    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10))
    response = client.post("/", content=chunks())
    assert_too_large(response)


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10))
    # httpx would refuse to send a mismatching header, so drive the app directly.
    import anyio

    async def run() -> list[Message]:
        sent: list[Message] = []
        messages: list[Message] = [
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"x" * 6},
        ]

        async def receive() -> Message:
            return messages.pop(0)

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Any = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"content-length", b"3")],
            "scheme": "http",
            "server": ("testserver", 80),
        }
        await client.app(scope, receive, send)
        return sent

    sent = anyio.run(run)
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"12345").text == "5"
    assert_too_large(client.post("/", content=b"123456"))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/small", routes=[Route("/", read, methods=["POST"])], max_body_size=5),
            Route("/other", read, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small/", content=b"12345").text == "5"
    assert_too_large(client.post("/small/", content=b"123456"))
    assert client.post("/other", content=b"123456").text == "6"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read, methods=["POST"], max_body_size=5)])
    client = test_client_factory(app)
    assert client.post("/", content=b"12345").text == "5"
    assert_too_large(client.post("/", content=b"123456"))


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", read, methods=["POST"], max_body_size=100),
            Route("/lowered", read, methods=["POST"], max_body_size=3),
            Route("/inherited", read, methods=["POST"]),
            Mount("/m", routes=[Route("/raised", read, methods=["POST"])], max_body_size=100),
            Mount("/n", routes=[Route("/lowered", read, methods=["POST"], max_body_size=100)], max_body_size=3),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    body = b"x" * 50
    assert client.post("/raised", content=body).text == "50"
    assert_too_large(client.post("/lowered", content=b"1234"))
    assert_too_large(client.post("/inherited", content=body))
    assert client.post("/m/raised", content=body).text == "50"
    assert client.post("/n/lowered", content=body).text == "50"


def test_content_length_fail_fast_uses_settled_limit(test_client_factory: TestClientFactory) -> None:
    seen: list[str] = []

    class Spy:
        def __init__(self, app):  # type: ignore[no-untyped-def]
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            seen.append("route middleware")
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/low", read, methods=["POST"], max_body_size=5, middleware=[Middleware(Spy)]),
            Route("/high", read, methods=["POST"], max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/low", content=b"x" * 6))
    assert seen == [] and calls == []
    # Over the application limit but under the route's: let through.
    assert client.post("/high", content=b"x" * 50).text == "50"
    # No route, wrong method: the application's limit applies.
    assert_too_large(client.post("/missing", content=b"x" * 11))
    assert client.post("/missing", content=b"x" * 5).status_code == 404


def test_method_mismatch_uses_outer_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read, methods=["POST"], max_body_size=100)], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.put("/", content=b"x" * 11))
    assert client.put("/", content=b"x" * 10).status_code == 405


def test_declared_length_rejected_before_user_code(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10))
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
        await send({"type": "http.response.body", "body": b"late"})

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    import anyio

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=5)
    chunks: list[Message] = [
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456"},
    ]

    async def receive() -> Message:
        return chunks.pop(0)

    scope: Any = {"type": "http", "headers": []}
    anyio.run(wrapped, scope, receive, send)
    # The 200 was already sent: no 413 follows, the body stream is stopped and the app finishes.
    assert [m["type"] for m in messages] == ["http.response.start", "http.response.body"]
    assert messages[0]["status"] == 200


def test_standalone_middleware_with_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("app")
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.get("/").text == "0"
    calls.clear()
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []  # rejected on entry


def test_standalone_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))


def test_streaming_request_overrun(test_client_factory: TestClientFactory) -> None:
    async def consume(request: Request) -> PlainTextResponse:
        async for _ in request.stream():
            pass
        return PlainTextResponse("done")

    client = test_client_factory(Starlette(routes=[Route("/", consume, methods=["POST"])], max_body_size=3))
    assert_too_large(client.post("/", content=b"abcd"))


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("x" * 100)
        await websocket.close()

    started: list[bool] = []

    async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
        started.append(True)
        yield

    from contextlib import asynccontextmanager

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1, lifespan=asynccontextmanager(lifespan))
    with test_client_factory(app) as client:
        with client.websocket_connect("/ws") as session:
            assert session.receive_text() == "x" * 100
    assert started == [True]


def test_lower_limit_applies_to_bytes_already_read(test_client_factory: TestClientFactory) -> None:
    class ReadFirst:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            first = await receive()
            pending = [first]

            async def replay() -> Message:
                return pending.pop(0) if pending else await receive()

            await self.app(scope, replay, send)

    app = Starlette(
        routes=[Route("/", read, methods=["POST"], max_body_size=3)],
        middleware=[Middleware(ReadFirst)],
        max_body_size=100,
    )
    assert_too_large(test_client_factory(app).post("/", content=b"12345"))
    assert calls == []


def test_application_swallowing_the_error_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while (await receive())["type"] == "http.request":
                pass
        except Exception:
            pass
        assert (await receive())["type"] == "http.disconnect"
        await PlainTextResponse("handled")(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=3))
    assert_too_large(client.post("/", content=iter([b"123", b"45"])))


def test_unrelated_errors_propagate(test_client_factory: TestClientFactory) -> None:
    async def boom(request: Request) -> PlainTextResponse:
        raise RuntimeError("boom")

    client = test_client_factory(Starlette(routes=[Route("/", boom, methods=["POST"])], max_body_size=10))
    with pytest.raises(RuntimeError, match="boom"):
        client.post("/", content=b"x")


def test_opaque_mount_fail_fast(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Mount("/a", app=asgi)], max_body_size=3))
    assert_too_large(client.post("/a/x", content=b"12345"))
    assert calls == []
    assert client.post("/a/x", content=b"123").text == "ok"


def test_none_disables_middleware(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    assert (
        test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=None)).post("/", content=b"x" * 99).text
        == "ok"
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda v: Starlette(max_body_size=v),
        lambda v: Router(max_body_size=v),
        lambda v: Mount("/", routes=[], max_body_size=v),
        lambda v: Route("/", read, max_body_size=v),
        lambda v: RequestBodyLimitMiddleware(Router(), max_body_size=v),
    ],
    ids=["starlette", "router", "mount", "route", "middleware"],
)
@pytest.mark.parametrize(("value", "exc"), [(-1, ValueError), ("5", TypeError), (1.5, TypeError), (True, TypeError)])
def test_invalid_limits(factory: Any, value: Any, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        factory(value)


def test_zero_limit_allows_only_empty_bodies(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["GET", "POST"])], max_body_size=0))
    assert client.get("/").text == "0"
    assert_too_large(client.post("/", content=b"x"))


def test_huge_content_length(test_client_factory: TestClientFactory) -> None:
    headers = {"content-length": "9" * 5000}
    app = Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10)
    assert_too_large(test_client_factory(app, raise_server_exceptions=False).post("/", headers=headers))
    wrapped = RequestBodyLimitMiddleware(app, max_body_size=10)
    assert_too_large(test_client_factory(wrapped, raise_server_exceptions=False).post("/", headers=headers))


def test_host_and_wrapped_router(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Host("testserver", app=asgi)], max_body_size=3))
    assert_too_large(client.post("/", content=b"12345"))
    assert calls == []

    class Wrap:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.app(scope, receive, send)

    inner = Wrap(Router([Route("/x", read, methods=["POST"], max_body_size=100)]))
    client = test_client_factory(Starlette(routes=[Mount("/api", app=inner)], max_body_size=10))
    assert client.post("/api/x", content=b"x" * 50).text == "50"


def test_413_sent_while_app_keeps_running(test_client_factory: TestClientFactory) -> None:
    import anyio

    finished: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while True:
                await receive()
        except BaseException:
            pass
        await anyio.sleep(0)
        try:
            while True:
                await send({"type": "http.response.body", "body": b"x", "more_body": True})
                await anyio.sleep(0)
        finally:
            finished.append(True)

    sent: list[Message] = []
    chunks: list[Message] = [
        {"type": "http.request", "body": b"12", "more_body": True},
        {"type": "http.request", "body": b"345"},
    ]

    async def receive() -> Message:
        return chunks.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Any = {"type": "http", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=3), scope, receive, send)
    assert sent[0]["status"] == 413 and finished == [True]


def test_sibling_error_not_swallowed() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def reader() -> None:
            await receive()
            await receive()

        async def failing() -> None:
            raise RuntimeError("database down")

        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(failing)

    chunks: list[Message] = [
        {"type": "http.request", "body": b"12", "more_body": True},
        {"type": "http.request", "body": b"345"},
    ]

    async def receive() -> Message:
        return chunks.pop(0)

    async def send(message: Message) -> None:
        pass

    scope: Any = {"type": "http", "headers": []}
    with pytest.raises(BaseException) as info:
        anyio.run(RequestBodyLimitMiddleware(app, max_body_size=3), scope, receive, send)
    assert "database down" in repr(info.value)


def test_nested_error_handler_not_called(test_client_factory: TestClientFactory) -> None:
    seen: list[Exception] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        seen.append(exc)
        return PlainTextResponse("500", status_code=500)

    sub = Starlette(routes=[Route("/", read, methods=["POST"])], exception_handlers={Exception: handler})
    client = test_client_factory(Starlette(routes=[Mount("/sub", app=sub)], max_body_size=3))
    assert_too_large(client.post("/sub/", content=iter([b"12", b"345"])))
    assert seen == []


def test_invalid_limit_in_middleware_list() -> None:
    with pytest.raises(ValueError):
        Starlette(middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=-1)])
    with pytest.raises(TypeError):
        Starlette(middleware=[Middleware(RequestBodyLimitMiddleware, "5")])  # type: ignore[arg-type]
