from __future__ import annotations

from collections.abc import Callable, Iterator
from tempfile import SpooledTemporaryFile
from typing import Any
from unittest import mock

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Mount, Route, Router
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_body(request: Request) -> PlainTextResponse:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


async def ignore_body(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ignored")


def chunked(size: int) -> Iterator[bytes]:
    # A generator body is sent without a Content-Length header.
    yield b"x" * (size // 2)
    yield b"x" * (size - size // 2)


def make_app(kind: str, limit: int | None, endpoint: Callable[..., Any] = read_body) -> Starlette:
    if kind == "starlette":
        return Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=limit)
    if kind == "router":
        router = Router([Route("/", endpoint, methods=["POST"])], max_body_size=limit)
        return Starlette(routes=[Mount("/", app=router)])
    if kind == "mount":
        return Starlette(routes=[Mount("/", routes=[Route("/", endpoint, methods=["POST"])], max_body_size=limit)])
    assert kind == "route"
    return Starlette(routes=[Route("/", endpoint, methods=["POST"], max_body_size=limit)])


KINDS = ["starlette", "router", "mount", "route"]


@pytest.mark.parametrize("kind", KINDS)
def test_default_is_unlimited(test_client_factory: TestClientFactory, kind: str) -> None:
    client = test_client_factory(make_app(kind, None))
    response = client.post("/", content=b"x" * 100_000)
    assert response.status_code == 200
    assert response.text == "100000"


@pytest.mark.parametrize("kind", KINDS)
def test_limit_with_content_length(test_client_factory: TestClientFactory, kind: str) -> None:
    client = test_client_factory(make_app(kind, 10))
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413


@pytest.mark.parametrize("kind", KINDS)
def test_limit_without_content_length(test_client_factory: TestClientFactory, kind: str) -> None:
    client = test_client_factory(make_app(kind, 10))
    assert client.post("/", content=chunked(10)).text == "10"
    response = client.post("/", content=chunked(11))
    assert response.status_code == 413


@pytest.mark.parametrize("kind", KINDS)
def test_content_length_fail_fast_when_endpoint_ignores_body(test_client_factory: TestClientFactory, kind: str) -> None:
    client = test_client_factory(make_app(kind, 10, ignore_body))
    assert client.post("/", content=b"x" * 10).text == "ignored"
    assert client.post("/", content=b"x" * 11).status_code == 413


class BodyReadingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body", False):
            pass
        await self.app(scope, receive, send)


def test_app_middleware_reading_body_is_limited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", ignore_body, methods=["POST"])],
        middleware=[Middleware(BodyReadingMiddleware)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=chunked(10)).status_code == 200
    assert client.post("/", content=chunked(11)).status_code == 413


def make_nested_app(kind: str, outer: int, inner: int) -> Starlette:
    if kind == "router":
        routes: list[BaseRoute] = [
            Mount("/", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=inner))
        ]
    elif kind == "mount":
        routes = [Mount("/", routes=[Route("/", read_body, methods=["POST"])], max_body_size=inner)]
    else:
        routes = [Route("/", read_body, methods=["POST"], max_body_size=inner)]
    return Starlette(routes=routes, max_body_size=outer)


@pytest.mark.parametrize("kind", ["router", "mount", "route"])
def test_inner_limit_raises_outer_limit(test_client_factory: TestClientFactory, kind: str) -> None:
    client = test_client_factory(make_nested_app(kind, outer=10, inner=100))
    assert client.post("/", content=b"x" * 50).text == "50"
    assert client.post("/", content=chunked(50)).text == "50"
    assert client.post("/", content=b"x" * 101).status_code == 413
    assert client.post("/", content=chunked(101)).status_code == 413


@pytest.mark.parametrize("kind", ["router", "mount", "route"])
def test_inner_limit_lowers_outer_limit(test_client_factory: TestClientFactory, kind: str) -> None:
    client = test_client_factory(make_nested_app(kind, outer=100, inner=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=b"x" * 50).status_code == 413
    assert client.post("/", content=chunked(50)).status_code == 413


def test_sibling_route_keeps_outer_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Route("/small", read_body, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/big", content=b"x" * 50).text == "50"
    assert client.post("/small", content=b"x" * 50).status_code == 413


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_body_limit() -> None:
    created: list[SpooledTemporaryFile[bytes]] = []

    def tracking_factory(*args: Any, **kwargs: Any) -> SpooledTemporaryFile[bytes]:
        file: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(*args, **kwargs)
        created.append(file)
        return file

    async def form_endpoint(request: Request) -> PlainTextResponse:
        async with request.form():
            return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=150)

    chunks = [
        b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n" + b"x" * 50,
        b"x" * 200 + b"\r\n--b--\r\n",
    ]
    index = 0

    async def receive() -> Message:
        nonlocal index
        body = chunks[index]
        index += 1
        return {"type": "http.request", "body": body, "more_body": index < len(chunks)}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
    }
    with mock.patch("starlette.formparsers.SpooledTemporaryFile", tracking_factory):
        await app(scope, receive, send)

    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert all(file.closed for file in created)
