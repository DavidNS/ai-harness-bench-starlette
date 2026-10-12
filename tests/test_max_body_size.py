from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router, WebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket
from tests.types import TestClientFactory


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def chunks(total: int) -> Iterator[bytes]:
    # A generator body is sent without Content-Length, so only received bytes are counted.
    for _ in range(total // 10):
        yield b"x" * 10


class ReadBodyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover
            await self.app(scope, receive, send)
            return

        body = await Request(scope, receive).body()
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


def post(client: Any, path: str, size: int, chunked: bool = False) -> Any:
    if chunked:
        return client.post(path, content=chunks(size))
    return client.post(path, content=b"x" * size)


@pytest.mark.parametrize("chunked", [False, True])
def test_default_is_unlimited(test_client_factory: TestClientFactory, chunked: bool) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])]),
        ],
        max_body_size=None,
    )
    client = test_client_factory(app)
    assert post(client, "/", 100_000, chunked).text == "100000"
    assert post(client, "/m/", 100_000, chunked).text == "100000"
    assert Router(max_body_size=None) is not None
    assert Mount("/x", routes=[], max_body_size=None) is not None
    assert Route("/", read_body, max_body_size=None) is not None


@pytest.mark.parametrize("chunked", [False, True])
def test_starlette_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    client = test_client_factory(app)
    assert post(client, "/", 100, chunked).text == "100"
    response = post(client, "/", 200, chunked)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


@pytest.mark.parametrize("chunked", [False, True])
def test_starlette_limit_applies_to_middleware_reading_body(
    test_client_factory: TestClientFactory, chunked: bool
) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(ReadBodyMiddleware)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert post(client, "/", 50, chunked).text == "50"
    assert post(client, "/", 200, chunked).status_code == 413


@pytest.mark.parametrize("chunked", [False, True])
def test_route_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_body, methods=["POST"], max_body_size=100),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert post(client, "/limited", 100, chunked).text == "100"
    assert post(client, "/limited", 200, chunked).status_code == 413
    assert post(client, "/free", 200, chunked).text == "200"


@pytest.mark.parametrize("chunked", [False, True])
def test_mount_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert post(client, "/m/", 100, chunked).text == "100"
    assert post(client, "/m/", 200, chunked).status_code == 413
    assert post(client, "/free", 200, chunked).text == "200"


@pytest.mark.parametrize("chunked", [False, True])
def test_router_limit(test_client_factory: TestClientFactory, chunked: bool) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    client = test_client_factory(router)
    assert post(client, "/", 100, chunked).text == "100"
    assert post(client, "/", 200, chunked).status_code == 413


@pytest.mark.parametrize("chunked", [False, True])
def test_router_limit_as_mounted_app(test_client_factory: TestClientFactory, chunked: bool) -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/r", app=inner)])
    client = test_client_factory(app)
    assert post(client, "/r/", 100, chunked).text == "100"
    assert post(client, "/r/", 200, chunked).status_code == 413


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("kind", ["route", "mount", "router"])
def test_inner_limit_raises_outer_limit(test_client_factory: TestClientFactory, chunked: bool, kind: str) -> None:
    if kind == "route":
        routes = [Route("/", read_body, methods=["POST"], max_body_size=1000)]
    elif kind == "mount":
        routes = [Mount("/", routes=[Route("/", read_body, methods=["POST"])], max_body_size=1000)]
    else:
        routes = [Mount("/", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=1000))]
    client = test_client_factory(Starlette(routes=routes, max_body_size=100))
    assert post(client, "/", 500, chunked).text == "500"
    assert post(client, "/", 2000, chunked).status_code == 413


def test_non_http_scopes_pass_through(test_client_factory: TestClientFactory) -> None:
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)], max_body_size=100)
    with test_client_factory(app) as client:  # also runs lifespan
        with client.websocket_connect("/ws") as session:
            assert session.receive_text() == "hello"


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("kind", ["route", "mount", "router"])
def test_inner_limit_lowers_outer_limit(test_client_factory: TestClientFactory, chunked: bool, kind: str) -> None:
    if kind == "route":
        routes = [Route("/", read_body, methods=["POST"], max_body_size=10)]
    elif kind == "mount":
        routes = [Mount("/", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)]
    else:
        routes = [Mount("/", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10))]
    client = test_client_factory(Starlette(routes=routes, max_body_size=1000))
    assert post(client, "/", 10, chunked).text == "10"
    assert post(client, "/", 50, chunked).status_code == 413
