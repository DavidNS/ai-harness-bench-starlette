from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def echo_size(request: Request) -> JSONResponse:
    calls.append(request.url.path)
    return JSONResponse({"size": len(await request.body())})


async def no_read(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    return PlainTextResponse("ok")


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


def post(client: TestClient, path: str, size: int, **kwargs: object) -> object:
    return client.post(path, content=b"x" * size, **kwargs)  # type: ignore[arg-type]


def test_starlette_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert response.headers["content-type"].startswith("text/plain")


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_no_body_and_zero_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", no_read, methods=["GET", "POST"])], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").status_code == 200
    assert client.post("/").status_code == 200
    assert client.post("/", content=b"x").status_code == 413


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_and_route_limits(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", echo_size, methods=["POST"]),
            Mount(
                "/mount",
                routes=[
                    Route("/default", echo_size, methods=["POST"]),
                    Route("/lower", echo_size, methods=["POST"], max_body_size=3),
                    Route("/higher", echo_size, methods=["POST"], max_body_size=1000),
                ],
                max_body_size=20,
            ),
            Mount("/inner", app=Router([Route("/", echo_size, methods=["POST"])], max_body_size=100)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert post(client, "/small", 10).status_code == 200  # type: ignore[attr-defined]
    assert post(client, "/small", 11).status_code == 413  # type: ignore[attr-defined]
    # The mount raises the application limit.
    assert post(client, "/mount/default", 20).status_code == 200  # type: ignore[attr-defined]
    assert post(client, "/mount/default", 21).status_code == 413  # type: ignore[attr-defined]
    # A route lowers or raises the limit of its mount.
    assert post(client, "/mount/lower", 3).status_code == 200  # type: ignore[attr-defined]
    assert post(client, "/mount/lower", 4).status_code == 413  # type: ignore[attr-defined]
    assert post(client, "/mount/higher", 1000).status_code == 200  # type: ignore[attr-defined]
    assert post(client, "/mount/higher", 1001).status_code == 413  # type: ignore[attr-defined]
    assert post(client, "/inner/", 100).status_code == 200  # type: ignore[attr-defined]
    assert post(client, "/inner/", 101).status_code == 413  # type: ignore[attr-defined]


def test_content_length_rejects_without_running_endpoint(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", echo_size, methods=["POST"]),
            Route("/big", echo_size, methods=["POST"], max_body_size=1000),
            Route("/low", no_read, methods=["POST"], max_body_size=2),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert post(client, "/", 11).status_code == 413  # type: ignore[attr-defined]
    assert post(client, "/low", 5).status_code == 413  # type: ignore[attr-defined]
    assert calls == []
    # The declared length is not rejected by the limit of an outer part of the application.
    assert post(client, "/big", 500).status_code == 200  # type: ignore[attr-defined]
    assert calls == ["/big"]


def test_content_length_not_reading_body() -> None:
    received = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await RequestBodyLimitMiddleware(inner, max_body_size=10)(scope, receive, send)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body"):  # pragma: no cover
            pass
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    async def counting_receive() -> Message:
        nonlocal received
        received += 1  # pragma: no cover
        return {"type": "http.request", "body": b""}  # pragma: no cover

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"11")]}
    anyio.run(app, scope, counting_receive, send)
    assert received == 0
    assert sent[0]["status"] == 413


def test_standalone_middleware_with_asgi_app() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        size = 0
        while True:
            message = await receive()
            size += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await PlainTextResponse(str(size))(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.get("/").text == "0"


def test_streamed_body_without_content_length() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    def chunks() -> object:
        for _ in range(5):
            yield b"abc"

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    response = client.post("/", content=chunks())  # type: ignore[arg-type]
    assert response.status_code == 413


def test_understated_content_length() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert response.status_code == 413


def test_app_swallowing_the_error_still_gets_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await Request(scope, receive).body()
        except Exception:
            pass
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert (response.status_code, response.text) == (413, "Content Too Large")


def test_response_already_started_is_cut_off() -> None:
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await Request(scope, receive).body()
        await send({"type": "http.response.body", "body": b"never"})  # pragma: no cover

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 6, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["more_body"] is True


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    started = []

    @asynccontextmanager
    async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
        started.append(True)
        yield

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1, lifespan=lifespan)
    with test_client_factory(app) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("hello world")
            assert session.receive_text() == "hello world"
    assert started == [True]


def test_user_middleware_reading_body_is_limited(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    class Reader(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
            await request.body()
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", no_read, methods=["POST"])],
        middleware=[Middleware(Reader)],
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    assert post(client, "/", 10).status_code == 200  # type: ignore[attr-defined]
    assert post(client, "/", 11).status_code == 413  # type: ignore[attr-defined]


def test_negative_limit_is_rejected() -> None:
    with pytest.raises(AssertionError):
        RequestBodyLimitMiddleware(PlainTextResponse("x"), max_body_size=-1)


@pytest.mark.parametrize(
    "headers, accepted",
    [
        ([(b"content-length", b"abc")], True),
        ([(b"content-length", b"-5")], True),
        ([(b"content-length", b"50"), (b"content-length", b"50")], True),
        ([(b"content-length", b"50")], False),
    ],
)
def test_unusable_content_length_is_ignored(headers: list[tuple[bytes, bytes]], accepted: bool) -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await app(scope, receive, send)

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x"}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    anyio.run(RequestBodyLimitMiddleware(inner, max_body_size=10), scope, receive, send)
    assert (sent[0]["status"] == 200) is accepted


def test_standalone_middleware_around_starlette_app_with_inner_limits(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/big", echo_size, methods=["POST"], max_body_size=1000),
            Route("/default", echo_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert post(client, "/big", 500).status_code == 200  # type: ignore[attr-defined]
    assert post(client, "/big", 1001).status_code == 413  # type: ignore[attr-defined]
    assert post(client, "/default", 11).status_code == 413  # type: ignore[attr-defined]
    assert calls == ["/big"]


def test_method_not_allowed_is_not_replaced_by_content_length_check(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", no_read, methods=["GET"])], max_body_size=1)
    assert post(test_client_factory(app), "/", 50).status_code == 405  # type: ignore[attr-defined]


def test_other_exceptions_are_not_swallowed() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    with pytest.raises(RuntimeError, match="boom"):
        client.post("/", content=b"x")


def test_cut_off_response_drops_what_the_app_sends_afterwards() -> None:
    import anyio

    sent: list[Message] = []
    messages: list[Message] = [
        {"type": "http.request", "body": b"x" * 11, "more_body": True},
    ]

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(2):  # The body stream stays stopped.
            with pytest.raises(RequestBodyTooLarge):
                await receive()
        await send({"type": "http.response.body", "body": b"late"})

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_error_raised_through_a_task_group_is_handled() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def read() -> None:
            await receive()

        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            tg.start_soon(read)

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 11}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert sent[0]["status"] == 413


def test_disconnect_message_is_passed_through() -> None:
    import anyio

    seen: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(await receive())

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None: ...  # pragma: no cover

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert seen == [{"type": "http.disconnect"}]
