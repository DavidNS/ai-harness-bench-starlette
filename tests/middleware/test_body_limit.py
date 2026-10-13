from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import MAX_BODY_SIZE_SCOPE_KEY, RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory


async def read_body(request: Request) -> Response:
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def ignore_body(request: Request) -> Response:
    return PlainTextResponse("ignored")


async def read_form(request: Request) -> Response:
    async with request.form() as form:
        return JSONResponse({"keys": sorted(form.keys())})


def chunks(n: int, size: int = 1) -> Iterator[bytes]:
    for _ in range(n):
        yield b"x" * size


def test_under_and_exactly_at_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert client.post("/", content=b"x" * 3).json() == {"size": 3}
    assert client.post("/", content=b"").json() == {"size": 0}


def test_over_limit_with_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    response = test_client_factory(app).post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_fail_fast_even_if_body_is_not_read(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=b"x" * 10).status_code == 200


def test_over_limit_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(1, 11)).status_code == 413
    assert client.post("/", content=chunks(1, 10)).json() == {"size": 10}


def test_streaming_body(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return JSONResponse({"size": total})

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(10)).json() == {"size": 10}
    assert client.post("/", content=chunks(11)).status_code == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert response.status_code == 413


def test_understated_content_length_plain_asgi() -> None:
    messages: list[dict[str, Any]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(Request(scope, receive))
        raise AssertionError("unreachable")  # pragma: no cover

    parts = [b"x" * 8, b"x" * 8]

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": parts.pop(0), "more_body": bool(parts)}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"4")]}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert messages[0]["status"] == 413
    assert len(messages) == 2
    assert MAX_BODY_SIZE_SCOPE_KEY not in scope


def test_invalid_content_length_is_ignored(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=chunks(1, 5), headers={"content-length": "abc"})
    assert response.status_code == 200


def test_multipart_upload(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    assert client.post("/", files={"f": ("a.txt", b"x" * 100)}).json() == {"keys": ["f"]}
    assert client.post("/", files={"f": ("a.txt", b"x" * 2000)}).status_code == 413


def test_multipart_overhead_counts(test_client_factory: TestClientFactory) -> None:
    # The file content fits, but the encoded multipart body does not.
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=100)
    response = test_client_factory(app).post("/", files={"f": ("a.txt", b"x" * 90)})
    assert response.status_code == 413


def test_no_double_response() -> None:
    messages: list[dict[str, Any]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await receive()  # exceeds the limit after the response has started

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"x" * 20, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def run() -> None:
        scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        with pytest.raises(HTTPException) as exc_info:
            await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)
        assert exc_info.value.status_code == 413

    anyio.run(run)
    assert [m["type"] for m in messages] == ["http.response.start", "http.response.body"]


def test_scope_key_set_and_restored() -> None:
    seen: list[Any] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope[MAX_BODY_SIZE_SCOPE_KEY])

    async def send(message: Any) -> None: ...  # pragma: no cover

    async def receive() -> Any: ...  # pragma: no cover

    async def run() -> None:
        scope: dict[str, Any] = {"type": "http", "headers": []}
        await RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(inner, 5), 10)(scope, receive, send)
        assert MAX_BODY_SIZE_SCOPE_KEY not in scope
        scope = {"type": "http", "headers": [], MAX_BODY_SIZE_SCOPE_KEY: 99}
        await RequestBodyLimitMiddleware(inner, 5)(scope, receive, send)
        assert scope[MAX_BODY_SIZE_SCOPE_KEY] == 99

    anyio.run(run)
    assert seen == [5, 5]


def test_non_http_passthrough() -> None:
    called: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        called.append(scope["type"])

    anyio.run(RequestBodyLimitMiddleware(app, 1), {"type": "lifespan"}, None, None)
    assert called == ["lifespan"]


@pytest.mark.parametrize("kind", ["router", "mount", "route"])
def test_router_mount_route_limits(test_client_factory: TestClientFactory, kind: str) -> None:
    route = Route("/", read_body, methods=["POST"])
    if kind == "router":
        app = Router([route], max_body_size=5)
        path = "/"
    elif kind == "mount":
        app = Router([Mount("/m", routes=[route], max_body_size=5)])
        path = "/m/"
    else:
        app = Router([Route("/", read_body, methods=["POST"], max_body_size=5)])
        path = "/"
    client = test_client_factory(app)
    assert client.post(path, content=b"x" * 5).status_code == 200
    assert client.post(path, content=b"x" * 6).status_code == 413
    assert client.post(path, content=chunks(6)).status_code == 413


def test_inner_limit_raises_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 50).status_code == 413
    assert client.post("/big", content=b"x" * 50).status_code == 200
    assert client.post("/big", content=chunks(50)).status_code == 200
    assert client.post("/big", content=b"x" * 101).status_code == 413
    assert client.post("/m/", content=b"x" * 50).status_code == 200


def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/low", read_body, methods=["POST"], max_body_size=5),
            Route("/low-ignored", ignore_body, methods=["POST"], max_body_size=5),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).status_code == 200
    assert client.post("/low", content=b"x" * 50).status_code == 413
    assert client.post("/low-ignored", content=b"x" * 50).status_code == 413


def test_app_middleware_reading_body_is_limited(test_client_factory: TestClientFactory) -> None:
    class ReadBody(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
            await request.body()
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", ignore_body, methods=["POST"])],
        middleware=[Middleware(ReadBody)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=chunks(11)).status_code == 413


def test_unlimited_by_default(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100000).json() == {"size": 100000}
