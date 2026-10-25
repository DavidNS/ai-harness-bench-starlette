from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

CALLS: list[str] = []


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    CALLS.clear()


async def read_body(request: Request) -> Response:
    CALLS.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


def chunks(*sizes: int) -> Iterator[bytes]:
    for size in sizes:
        yield b"x" * size


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    assert client.post("/", content=b"").text == "read 0"
    assert client.get("/nothing").status_code == 404
    assert_too_large(client.post("/", content=b"x" * 11))


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(5, 5)).text == "read 10"
    assert_too_large(client.post("/", content=chunks(5, 5, 1)))


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=chunks(20), headers={"content-length": "5"})
    assert_too_large(response)


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(11)))
    assert CALLS == ["/", "/"]  # the Content-Length request never reached the endpoint


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/open", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/open", content=b"x" * 100).text == "read 100"
    assert client.post("/m/", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/m/", content=chunks(11)))
    assert_too_large(client.post("/m/", content=b"x" * 11))
    assert CALLS == ["/open", "/m/", "/m/"]


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_body, methods=["POST"], max_body_size=10),
            Route("/open", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/open", content=b"x" * 100).text == "read 100"
    assert client.post("/limited", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/limited", content=chunks(11)))
    assert_too_large(client.post("/limited", content=b"x" * 11))
    assert CALLS == ["/open", "/limited", "/limited"]


def test_inner_limits_raise_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/outer", read_body, methods=["POST"]),
            Route("/route", read_body, methods=["POST"], max_body_size=100),
            Mount("/mount", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount(
                "/router",
                app=Router([Route("/", read_body, methods=["POST"])], max_body_size=100),
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/outer", content=b"x" * 50))
    assert client.post("/route", content=b"x" * 50).text == "read 50"
    assert client.post("/mount/", content=b"x" * 50).text == "read 50"
    assert client.post("/router/", content=b"x" * 50).text == "read 50"
    assert_too_large(client.post("/route", content=chunks(101)))
    assert_too_large(client.post("/mount/", content=b"x" * 101))
    assert CALLS == ["/route", "/mount/", "/router/", "/route"]


def test_inner_limits_lower_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/outer", read_body, methods=["POST"]),
            Route("/route", read_body, methods=["POST"], max_body_size=10),
            Mount("/mount", routes=[Route("/", read_body, methods=["POST"], max_body_size=5)], max_body_size=10),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/outer", content=b"x" * 50).text == "read 50"
    assert_too_large(client.post("/route", content=b"x" * 50))
    assert_too_large(client.post("/mount/", content=b"x" * 7))
    assert client.post("/mount/", content=b"x" * 5).text == "read 5"
    assert_too_large(client.post("/mount/", content=chunks(6)))
    assert CALLS == ["/outer", "/mount/", "/mount/"]


def test_content_length_fail_fast_when_nothing_matches(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # No route, no method: the application's limit applies, not the route's.
    assert_too_large(client.post("/missing", content=b"x" * 11))
    assert_too_large(client.put("/", content=b"x" * 11))
    assert client.put("/", content=b"x" * 5).status_code == 405
    assert client.post("/missing", content=b"x" * 5).status_code == 404
    assert CALLS == []


def test_content_length_fail_fast_waits_for_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10_000)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 500).text == "read 500"


def test_mount_of_plain_asgi_app_fail_fast(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        CALLS.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Mount("/m", app=asgi)], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/m/", content=b"x" * 11))
    assert client.post("/m/", content=b"x" * 10).text == "ok"
    assert CALLS == ["asgi"]


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
            session.send_text("hello world")
            assert session.receive_text() == "hello world"


async def echo_asgi(scope: Scope, receive: Receive, send: Send) -> None:
    CALLS.append("app")
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    await PlainTextResponse(f"read {len(body)}")(scope, receive, send)


def test_middleware_on_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(echo_asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "read 10"
    assert client.get("/").text == "read 0"
    assert_too_large(client.post("/", content=chunks(6, 6)))
    assert_too_large(client.post("/", content=chunks(20), headers={"content-length": "1"}))
    assert CALLS == ["app", "app", "app", "app"]


def test_middleware_content_length_rejected_on_entry(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(echo_asgi, max_body_size=10))
    assert_too_large(client.post("/", content=b"x" * 11))
    assert CALLS == []


def test_middleware_in_starlette_middleware_list(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(11)))
    assert CALLS == ["/"]  # only the streamed request reached the endpoint


def test_middleware_ignores_websocket_and_lifespan() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    app = RequestBodyLimitMiddleware(inner, max_body_size=1)
    import anyio

    async def run() -> None:
        for kind in ("websocket", "lifespan"):
            await app({"type": kind}, mock.AsyncMock(), mock.AsyncMock())

    anyio.run(run)
    assert seen == ["websocket", "lifespan"]


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    sent: list[dict[str, Any]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"early", "more_body": True})
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                CALLS.append("stopped")
                break
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def recording_send(message: dict[str, Any]) -> None:
        sent.append(message)

    messages = iter([{"type": "http.request", "body": b"x" * 20, "more_body": True}])

    async def receive() -> dict[str, Any]:
        return next(messages)

    import anyio

    scope = {"type": "http", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, recording_send)  # type: ignore[arg-type]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body", "http.response.body"]
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [200]
    assert CALLS == ["stopped"]


def test_no_second_response_after_413() -> None:
    sent: list[dict[str, Any]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        # Keep going as if nothing happened.
        await PlainTextResponse("late")(scope, receive, send)

    async def recording_send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"x" * 20}

    import anyio

    middleware = RequestBodyLimitMiddleware(app, max_body_size=10)
    anyio.run(middleware, {"type": "http", "headers": []}, receive, recording_send)  # type: ignore[arg-type]
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]
