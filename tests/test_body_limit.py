from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest import mock

import anyio
import pytest

from starlette import formparsers
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute, Mount, Route, Router
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

LIMIT = 100
PATHS = {"route": "/route", "mount": "/mount/", "router": "/router/"}


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def chunked(size: int) -> Iterator[bytes]:
    yield b"x" * (size // 2)
    yield b"x" * (size - size // 2)


def app_with(
    *,
    app: int | None = None,
    route: int | None = None,
    mount: int | None = None,
    router: int | None = None,
) -> Starlette:
    routes: list[BaseRoute] = [
        Route("/route", read_body, methods=["POST"], max_body_size=route),
        Mount("/mount", routes=[Route("/", read_body, methods=["POST"])], max_body_size=mount),
        Mount("/router", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=router)),
    ]
    return Starlette(routes=routes, max_body_size=app)


@pytest.mark.parametrize("path", PATHS.values())
def test_unlimited_by_default(test_client_factory: TestClientFactory, path: str) -> None:
    client = test_client_factory(app_with())
    response = client.post(path, content=b"x" * 100_000)
    assert response.status_code == 200
    assert response.text == "100000"


def test_standalone_router_unlimited_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Router([Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).status_code == 200


@pytest.mark.parametrize("path", PATHS.values())
def test_application_limit(test_client_factory: TestClientFactory, path: str) -> None:
    client = test_client_factory(app_with(app=LIMIT))
    assert client.post(path, content=b"x" * LIMIT).text == str(LIMIT)
    response = client.post(path, content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit_chunked_without_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app_with(app=LIMIT))
    assert client.post("/route", content=chunked(LIMIT)).status_code == 200
    assert client.post("/route", content=chunked(LIMIT + 1)).status_code == 413


def test_application_limit_with_user_middleware_reading_body(test_client_factory: TestClientFactory) -> None:
    class ReadBody(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            await request.body()
            return await call_next(request)  # pragma: no cover

    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(ReadBody)], max_body_size=LIMIT)
    client = test_client_factory(app)
    assert client.post("/", content=chunked(LIMIT + 1)).status_code == 413


@pytest.mark.parametrize("kind", ["route", "mount", "router"])
def test_inner_limit(test_client_factory: TestClientFactory, kind: str) -> None:
    path = PATHS[kind]
    client = test_client_factory(app_with(**{kind: LIMIT}))
    assert client.post(path, content=b"x" * LIMIT).status_code == 200
    assert client.post(path, content=b"x" * (LIMIT + 1)).status_code == 413
    assert client.post(path, content=chunked(LIMIT + 1)).status_code == 413


@pytest.mark.parametrize("kind", ["route", "mount", "router"])
def test_inner_limit_raises_outer(test_client_factory: TestClientFactory, kind: str) -> None:
    path = PATHS[kind]
    client = test_client_factory(app_with(app=LIMIT, **{kind: LIMIT * 10}))
    assert client.post(path, content=b"x" * (LIMIT * 5)).status_code == 200
    assert client.post(path, content=chunked(LIMIT * 5)).status_code == 200
    assert client.post(path, content=b"x" * (LIMIT * 10 + 1)).status_code == 413


@pytest.mark.parametrize("kind", ["route", "mount", "router"])
def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory, kind: str) -> None:
    path = PATHS[kind]
    client = test_client_factory(app_with(app=LIMIT * 10, **{kind: LIMIT}))
    assert client.post(path, content=b"x" * LIMIT).status_code == 200
    assert client.post(path, content=b"x" * (LIMIT * 5)).status_code == 413
    assert client.post(path, content=chunked(LIMIT * 5)).status_code == 413


def test_other_routes_keep_outer_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(app_with(app=LIMIT, route=LIMIT * 10))
    assert client.post("/mount/", content=b"x" * (LIMIT + 1)).status_code == 413
    assert client.post("/route", content=b"x" * (LIMIT + 1)).status_code == 200


def test_mounted_application_limit_overrides_outer(test_client_factory: TestClientFactory) -> None:
    sub = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=LIMIT * 10)
    app = Starlette(routes=[Mount("/sub", app=sub)], max_body_size=LIMIT)
    client = test_client_factory(app)
    assert client.post("/sub/", content=b"x" * (LIMIT * 5)).status_code == 200


def test_application_router_limit_overrides_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(max_body_size=LIMIT)
    app.router.max_body_size = LIMIT * 10  # only read when building the middleware stack
    app.router.routes.append(Route("/", read_body, methods=["POST"]))
    client = test_client_factory(app)
    # The attribute alone does not install a limit, but it defers the Content-Length check.
    assert client.post("/", content=b"x" * (LIMIT + 1)).status_code == 413


def test_standalone_router_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Router([Route("/", read_body, methods=["POST"])], max_body_size=LIMIT))
    assert client.post("/", content=b"x" * LIMIT).status_code == 200
    assert client.post("/", content=b"x" * (LIMIT + 1)).status_code == 413


def test_content_length_fail_fast_does_not_run_endpoint(test_client_factory: TestClientFactory) -> None:
    called: list[str] = []

    async def endpoint(request: Request) -> Response:
        called.append("endpoint")  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=LIMIT)
    response = test_client_factory(app).post("/", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert called == []


def test_non_http_requests_are_not_limited(test_client_factory: TestClientFactory) -> None:
    started: list[bool] = []

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        started.append(True)
        yield

    with test_client_factory(Starlette(max_body_size=LIMIT, lifespan=lifespan)):
        assert started == [True]


def test_response_already_started_is_not_replaced() -> None:
    sent: list[dict[str, Any]] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    app = Starlette(routes=[Mount("/", app=inner)], max_body_size=LIMIT)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"", "root_path": ""}

    async def main() -> None:
        await app(scope, receive, send)  # type: ignore[arg-type]

    anyio.run(main)
    assert [m["type"] for m in sent] == ["http.response.start"]


def multipart_body(file_size: int) -> tuple[bytes, str]:
    boundary = "bound"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode()
        + b"f" * file_size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def split(body: bytes, at: int) -> Iterator[bytes]:
    yield body[:at]
    yield body[at:]


async def form_endpoint(request: Request) -> Response:
    async with request.form() as form:
        upload = form["file"]
        return PlainTextResponse(str(len(await upload.read())))  # type: ignore[union-attr]


def test_multipart_counts_total_including_encoding_overhead(test_client_factory: TestClientFactory) -> None:
    body, content_type = multipart_body(60)
    headers = {"content-type": content_type}
    assert len(body) > 60

    # The file data alone fits, the whole encoded body does not.
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=len(body) - 1)
    client = test_client_factory(app)
    assert client.post("/", content=split(body, 10), headers=headers).status_code == 413
    assert client.post("/", content=body, headers=headers).status_code == 413

    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=len(body))
    response = test_client_factory(app).post("/", content=body, headers=headers)
    assert response.status_code == 200
    assert response.text == "60"


def test_multipart_closes_temp_files_on_any_exception() -> None:
    body, content_type = multipart_body(10)

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body[:-20]
        raise KeyboardInterrupt()

    parser = MultiPartParser(Headers({"content-type": content_type}), stream())

    with pytest.raises(KeyboardInterrupt):
        anyio.run(parser.parse)

    assert len(parser._files_to_close_on_error) == 1
    assert all(file.closed for file in parser._files_to_close_on_error)


def test_multipart_closes_temp_files_when_body_limit_is_hit() -> None:
    created: list[Any] = []
    sent: list[dict[str, Any]] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    body, content_type = multipart_body(500)
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=300)
    chunks = list(split(body, 200))

    async def receive() -> dict[str, Any]:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", content_type.encode())],
        "query_string": b"",
        "root_path": "",
    }

    async def main() -> None:
        await app(scope, receive, send)  # type: ignore[arg-type]

    with mock.patch.object(formparsers, "SpooledTemporaryFile", tracking):
        anyio.run(main)
    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert created[0].closed
