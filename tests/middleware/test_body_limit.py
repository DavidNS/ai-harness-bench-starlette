from __future__ import annotations

from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

called: list[str] = []


async def read(request: Request) -> Response:
    called.append(request.url.path)
    return JSONResponse({"size": len(await request.body())})


async def no_read(request: Request) -> Response:
    called.append(request.url.path)
    return PlainTextResponse("ok")


@pytest.fixture(autouse=True)
def clear_called() -> None:
    called.clear()


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10))
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert client.post("/").json() == {"size": 0}


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10))

    def gen() -> Any:
        yield b"x" * 6
        yield b"x" * 6

    response = client.post("/", content=gen())
    assert response.status_code == 413


def test_default_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100000).json() == {"size": 100000}


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_and_route_override(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        max_body_size=10,
        routes=[
            Route("/default", read, methods=["POST"]),
            Route("/raise", read, methods=["POST"], max_body_size=100),
            Route("/lower", read, methods=["POST"], max_body_size=3),
            Mount(
                "/m",
                max_body_size=50,
                routes=[
                    Route("/a", read, methods=["POST"]),
                    Route("/b", read, methods=["POST"], max_body_size=2),
                ],
            ),
            Mount("/r", routes=[Route("/a", read, methods=["POST"])], max_body_size=None),
        ],
    )
    client = test_client_factory(app)
    assert client.post("/default", content=b"x" * 11).status_code == 413
    assert client.post("/raise", content=b"x" * 100).status_code == 200
    assert client.post("/raise", content=b"x" * 101).status_code == 413
    assert client.post("/lower", content=b"x" * 3).status_code == 200
    assert client.post("/lower", content=b"x" * 4).status_code == 413
    assert client.post("/m/a", content=b"x" * 50).status_code == 200
    assert client.post("/m/a", content=b"x" * 51).status_code == 413
    assert client.post("/m/b", content=b"x" * 3).status_code == 413
    assert client.post("/r/a", content=b"x" * 11).status_code == 413


def test_inner_router_raises_limit(test_client_factory: TestClientFactory) -> None:
    inner = Router(routes=[Route("/a", read, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/m", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/m/a", content=b"x" * 50).status_code == 200
    assert client.post("/m/a", content=b"x" * 101).status_code == 413


def test_content_length_fail_fast(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        max_body_size=10,
        routes=[
            Route("/default", no_read, methods=["POST"]),
            Route("/raise", no_read, methods=["POST"], max_body_size=100),
            Route("/post", no_read, methods=["POST"]),
        ],
    )
    client = test_client_factory(app)
    assert client.post("/default", content=b"x" * 11).status_code == 413
    assert client.post("/raise", content=b"x" * 50).status_code == 200
    assert client.post("/missing", content=b"x" * 11).status_code == 413
    assert client.get("/post", headers={"content-length": "11"}).status_code == 413
    assert called == ["/raise"]


def test_understated_content_length() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        called.append("x")
        await request.body()
        await PlainTextResponse("ok")(scope, receive, send)

    import anyio

    sent: list[Any] = []
    messages = [
        {"type": "http.request", "body": b"x" * 8, "more_body": True},
        {"type": "http.request", "body": b"x" * 8, "more_body": False},
    ]

    async def receive() -> Any:
        return messages.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "headers": [(b"content-length", b"3")]}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert sent[0]["status"] == 413


def test_standalone_middleware_rejects_on_entry() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        called.append("app")  # pragma: no cover

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> Any:  # pragma: no cover
        raise AssertionError

    scope = {"type": "http", "headers": [(b"content-length", b"11")]}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"
    assert called == []


def test_non_http_untouched() -> None:
    import anyio

    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=1), {"type": "lifespan"}, None, None)  # type: ignore
    assert seen == ["lifespan"]


def test_response_already_started() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await send({"type": "http.response.body", "body": b"unreachable"})  # pragma: no cover

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> Any:
        return {"type": "http.request", "body": b"x" * 20, "more_body": False}

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), {"type": "http", "headers": []}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_multipart_over_limit(test_client_factory: TestClientFactory) -> None:
    async def form(request: Request) -> Response:
        async with request.form() as f:
            return JSONResponse(list(f.keys()))

    client = test_client_factory(Starlette(routes=[Route("/", form, methods=["POST"])], max_body_size=300))
    assert client.post("/", files={"a": ("a.txt", b"x" * 10)}).status_code == 200
    assert client.post("/", files={"a": ("a.txt", b"x" * 1000)}).status_code == 413


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_interruption() -> None:
    from starlette.datastructures import Headers

    parser_files: list[Any] = []

    async def stream() -> Any:
        yield (b"--b\r\nContent-Disposition: form-data; name=a; filename=a.txt\r\n\r\n" + b"x" * 10)
        raise RuntimeError("interrupted")

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RuntimeError):
        await parser.parse()
    parser_files.extend(parser._files_to_close_on_error)
    assert parser_files and all(f.closed for f in parser_files)
