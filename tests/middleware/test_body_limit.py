from __future__ import annotations

from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def read_body(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


async def read_json(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    await request.json()
    return PlainTextResponse("ok")  # pragma: no cover


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


def post(client: TestClient, path: str, size: int, **kwargs: Any) -> Any:
    return client.post(path, content=b"x" * size, **kwargs)


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert post(client, "/", 100_000).text == "100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert post(client, "/", 10).text == "10"
    assert_too_large(post(client, "/", 11))
    assert calls == ["/"]


def test_no_body_and_other_scopes_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    app = Starlette(
        routes=[Route("/", read_body, methods=["GET", "POST"]), WebSocketRoute("/ws", ws)],
        max_body_size=0,
    )
    with test_client_factory(app) as client:  # runs lifespan
        assert client.get("/").text == "0"
        with client.websocket_connect("/ws") as session:
            session.send_text("x" * 100)
            assert session.receive_text() == "x" * 100


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert post(client, "/", 5).text == "5"
    assert_too_large(post(client, "/", 6))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/free", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert post(client, "/free", 100).text == "100"
    assert post(client, "/m/", 5).text == "5"
    assert_too_large(post(client, "/m/", 6))


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert post(client, "/small", 5).text == "5"
    assert_too_large(post(client, "/small", 6))
    assert post(client, "/other", 100).text == "100"


def test_route_limit_without_state_for_standalone_route(test_client_factory: TestClientFactory) -> None:
    route = Route("/", read_body, methods=["POST"], max_body_size=5)
    client = test_client_factory(route)
    assert post(client, "/", 5).text == "5"
    assert_too_large(post(client, "/", 6))


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=3),
            Route("/inherited", read_body, methods=["POST"]),
            Mount(
                "/mount",
                routes=[
                    Route("/", read_body, methods=["POST"]),
                    Route("/lowered", read_body, methods=["POST"], max_body_size=2),
                ],
                max_body_size=50,
            ),
            Mount("/router", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=30)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert post(client, "/raised", 100).text == "100"
    assert_too_large(post(client, "/raised", 101))
    assert post(client, "/lowered", 3).text == "3"
    assert_too_large(post(client, "/lowered", 4))
    assert post(client, "/inherited", 10).text == "10"
    assert_too_large(post(client, "/inherited", 11))
    assert post(client, "/mount/", 50).text == "50"
    assert_too_large(post(client, "/mount/", 51))
    assert_too_large(post(client, "/mount/lowered", 3))
    assert post(client, "/router/", 30).text == "30"
    assert_too_large(post(client, "/router/", 31))


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks() -> Any:
        for _ in range(5):
            yield b"abc"

    assert_too_large(client.post("/", content=chunks()))


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(post(client, "/", 50, headers={"content-length": "5"}))


def test_content_length_fail_fast_in_application(test_client_factory: TestClientFactory) -> None:
    class Reader:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("endpoint")
            await PlainTextResponse("ok")(scope, receive, send)

    reader = Reader()

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("middleware")
            await self.app(scope, receive, send)

    from starlette.middleware import Middleware

    app = Starlette(
        routes=[
            Route("/lowered", reader, methods=["POST"], max_body_size=5),
            Route("/raised", reader, methods=["POST"], max_body_size=100),
            Route("/plain", reader, methods=["POST"]),
            Mount("/mount", app=reader, max_body_size=5),
        ],
        middleware=[Middleware(Spy)],
        max_body_size=10,
    )
    client = test_client_factory(app)

    # The declared length is judged against the limit of the route that matched.
    assert_too_large(post(client, "/lowered", 6))
    assert_too_large(post(client, "/plain", 11))
    assert_too_large(post(client, "/mount/x", 6))
    assert "endpoint" not in calls
    assert post(client, "/raised", 50).text == "ok"
    assert calls[-1] == "endpoint"

    # No route / no method: the application's limit applies.
    assert_too_large(post(client, "/missing", 11))
    assert_too_large(client.get("/lowered", headers={"content-length": "11"}))
    assert client.get("/lowered").status_code == 405
    assert client.post("/missing", content=b"x" * 10).status_code == 404


def test_content_length_fail_fast_standalone_middleware() -> None:
    calls_made: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls_made.append("app")  # pragma: no cover
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert_too_large(post(client, "/", 11))
    assert calls_made == []


def test_standalone_middleware_with_plain_asgi_app() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await PlainTextResponse(str(total))(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert post(client, "/", 10).text == "10"
    assert client.get("/").text == "0"
    assert_too_large(post(client, "/", 11, headers={"content-length": "1"}))

    def chunks() -> Any:
        yield b"x" * 6
        yield b"x" * 6

    assert_too_large(client.post("/", content=chunks()))


def test_standalone_middleware_unlimited_and_websocket() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close"})
        else:
            await PlainTextResponse("ok")(scope, receive, send)

    assert post(TestClient(RequestBodyLimitMiddleware(app)), "/", 1000).text == "ok"
    with TestClient(RequestBodyLimitMiddleware(app, max_body_size=0)).websocket_connect("/"):
        pass


def test_invalid_limit() -> None:
    with pytest.raises(ValueError):
        Starlette(max_body_size=-1)
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(PlainTextResponse("x"), max_body_size=-1)


def test_standalone_middleware_around_starlette_with_error_handlers(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_json, methods=["POST"])], debug=True)
    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert_too_large(client.post("/", content=b"[" + b"1," * 20 + b"1]", headers={"content-length": "1"}))
    assert calls == ["/"]


def test_unhandled_error_handler_does_not_replace_413(test_client_factory: TestClientFactory) -> None:
    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        return PlainTextResponse("custom 500", status_code=500)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_json, methods=["POST"])],
        exception_handlers={Exception: handler},
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    assert_too_large(client.post("/", content=b"[" + b"1," * 20 + b"1]", headers={"content-length": "1"}))


def test_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert post(client, "/", 10).text == "10"
    assert_too_large(post(client, "/", 20, headers={"content-length": "1"}))


def test_exceeded_after_response_started_sends_no_second_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            await receive()

    sent: list[dict[str, Any]] = []
    chunks = [
        {"type": "http.request", "body": b"x" * 6, "more_body": True},
        {"type": "http.request", "body": b"x" * 6, "more_body": False},
    ]

    async def receive() -> Any:
        return chunks.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


def test_body_stream_stops_after_limit() -> None:
    received = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal received
        request = Request(scope, receive)
        with pytest.raises(Exception):
            async for _ in request.stream():
                received += 1
        with pytest.raises(Exception):
            await receive()
        await PlainTextResponse("swallowed")(scope, receive, send)

    chunks = [{"type": "http.request", "body": b"x" * 6, "more_body": True} for _ in range(5)]

    async def receive() -> Any:
        return chunks.pop(0)

    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert received == 1
    assert sent[0]["status"] == 413
    assert len(chunks) == 3


def test_streaming_response_started_then_exceeded(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> StreamingResponse:
        async def gen() -> Any:
            yield b"a"
            await request.body()
            yield b"b"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.post("/", content=b"x" * 20, headers={"content-length": "1"})
    assert response.status_code in (200, 413)


def test_invalid_content_length_header_is_ignored() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.get("/", headers={"content-length": "abc"}).text == "ok"


def test_inner_limit_lowered_below_bytes_already_received(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    class ReadAll:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await receive()
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=3)],
        middleware=[Middleware(ReadAll)],
        max_body_size=100,
    )
    assert_too_large(post(test_client_factory(app), "/", 5))
    assert calls == []


def test_disconnect_and_app_errors_pass_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.disconnect"
        raise RuntimeError("boom")

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        raise AssertionError(message)  # pragma: no cover

    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    with pytest.raises(RuntimeError, match="boom"):
        anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)


def test_mount_of_plain_app_without_own_limit(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Mount("/m", app=asgi)], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(post(client, "/m/", 11))
    assert calls == []
    assert post(client, "/m/", 10).text == "ok"
