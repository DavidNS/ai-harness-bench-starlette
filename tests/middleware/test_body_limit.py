from __future__ import annotations

from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


def chunks(*sizes: int) -> Any:
    def gen() -> Any:
        for size in sizes:
            yield b"x" * size

    return gen()


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(5, 5)).text == "read 10"
    assert client.post("/", content=chunks(5, 5, 1)).status_code == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert response.status_code == 413


def test_invalid_content_length_is_ignored() -> None:
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        await PlainTextResponse("ok")(scope, receive, send)

    from starlette.testclient import TestClient

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.get("/", headers={"content-length": "abc"}).status_code == 200
    assert called


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_router_limit_overrides_app(test_client_factory: TestClientFactory) -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/inner/", content=b"x" * 50).status_code == 200
    assert client.post("/inner/", content=b"x" * 101).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 10).status_code == 200
    assert client.post("/m/", content=b"x" * 11).status_code == 413


def test_mount_limit_lowers_and_route_raises(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount(
                "/m",
                routes=[
                    Route("/low", read_body, methods=["POST"]),
                    Route("/high", read_body, methods=["POST"], max_body_size=100),
                ],
                max_body_size=10,
            )
        ],
        max_body_size=1000,
    )
    client = test_client_factory(app)
    assert client.post("/m/low", content=b"x" * 11).status_code == 413
    assert client.post("/m/low", content=b"x" * 10).status_code == 200
    assert client.post("/m/high", content=b"x" * 100).status_code == 200
    assert client.post("/m/high", content=b"x" * 101).status_code == 413


def test_route_limit_raises_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 11).status_code == 413
    assert client.post("/big", content=b"x" * 100).status_code == 200
    assert client.post("/big", content=b"x" * 101).status_code == 413
    # Understated header on the raised route is still counted.
    assert client.post("/big", content=b"x" * 101, headers={"content-length": "1"}).status_code == 413


def test_route_limit_lowers_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=5)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_route_limit_without_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert client.post("/free", content=b"x" * 600).status_code == 200


def test_endpoint_handler_exception_handlers_do_not_hide_413(test_client_factory: TestClientFactory) -> None:
    async def handler(request: Request, exc: Exception) -> Response:  # pragma: no cover
        return PlainTextResponse("handled", status_code=500)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        exception_handlers={Exception: handler},
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def failing_app_calls() -> tuple[list[str], Any]:
    calls: list[str] = []

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
            calls.append("middleware")
            await self.app(scope, receive, send)

    return calls, Spy


def test_content_length_fail_fast_does_not_run_app(test_client_factory: TestClientFactory) -> None:
    calls, Spy = failing_app_calls()

    def endpoint(request: Request) -> Response:  # pragma: no cover
        calls.append("endpoint")
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"], middleware=[Middleware(Spy)])],
        max_body_size=5,
    )
    response = test_client_factory(app).post("/", content=b"x" * 6)
    assert response.status_code == 413
    assert calls == []


def test_content_length_fail_fast_waits_for_inner_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount(
                "/m",
                routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
                max_body_size=5,
            )
        ],
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 50).status_code == 200
    assert client.post("/m/", content=b"x" * 101).status_code == 413


def test_content_length_fail_fast_on_non_matching(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        max_body_size=5,
    )
    client = test_client_factory(app)
    # No route: the application's limit applies, not the 404.
    assert client.post("/missing", content=b"x" * 6).status_code == 413
    assert client.post("/missing", content=b"x" * 5).status_code == 404
    # No method: the application's limit applies, not the route's.
    assert client.put("/", content=b"x" * 6).status_code == 413
    assert client.put("/", content=b"x" * 5).status_code == 405


def test_content_length_fail_fast_mount_of_asgi_app(test_client_factory: TestClientFactory) -> None:
    called = False

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        nonlocal called
        called = True

    app = Starlette(routes=[Mount("/m", app=asgi)], max_body_size=5)
    assert test_client_factory(app).post("/m/x", content=b"x" * 6).status_code == 413
    assert not called


def test_standalone_middleware_rejects_on_entry() -> None:
    called = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        nonlocal called
        called = True

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive() -> Message:  # pragma: no cover
        raise AssertionError("body must not be read")

    import anyio

    scope = {"type": "http", "method": "POST", "headers": [(b"content-length", b"11")]}
    anyio.run(RequestBodyLimitMiddleware(inner, max_body_size=10), scope, receive, send)
    assert not called
    assert messages[0]["status"] == 413
    assert messages[1]["body"] == b"Content Too Large"


def test_standalone_middleware_plain_asgi(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await PlainTextResponse(f"read {len(body)}")(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=chunks(6, 6))
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_response_already_started() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        for _ in range(2):  # the stream stays stopped
            with pytest.raises(RequestBodyTooLarge):
                await receive()

    sent: list[Message] = []
    message: Message = {"type": "http.request", "body": b"x" * 6, "more_body": True}

    async def receive() -> Message:
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    async def run() -> None:
        await RequestBodyLimitMiddleware(app, max_body_size=10)({"type": "http", "headers": []}, receive, send)

    anyio.run(run)
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("much longer than one byte")
            assert session.receive_text() == "much longer than one byte"


def test_no_body_requests_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    assert test_client_factory(app).get("/").text == "read 0"


def test_disconnect_message_is_passed_through() -> None:
    import anyio

    seen: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(await receive())

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None: ...  # pragma: no cover

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=0), {"type": "http", "headers": []}, receive, send)
    assert seen == [{"type": "http.disconnect"}]
