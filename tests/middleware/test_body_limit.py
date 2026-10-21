from __future__ import annotations

from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory


async def read_size(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


async def never_reads(request: Request) -> Response:
    return PlainTextResponse("ok")


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def chunks(*sizes: int) -> Any:
    def gen() -> Any:
        for size in sizes:
            yield b"x" * size

    return gen()


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    # streamed without Content-Length
    assert_too_large(client.post("/", content=chunks(5, 5, 5)))
    assert client.post("/", content=chunks(5, 5)).text == "10"
    assert client.get("/missing").status_code == 404


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100000).text == "100000"


def test_application_limit_applies_before_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(max_body_size=10)
    assert_too_large(test_client_factory(app).post("/missing", content=b"x" * 11))


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(6, 6)))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_size, methods=["POST"])], max_body_size=10),
            Route("/free", read_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/m/", content=b"x" * 11))
    assert client.post("/free", content=b"x" * 1000).text == "1000"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_size, methods=["POST"], max_body_size=10),
            Route("/free", read_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/limited", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/limited", content=b"x" * 11))
    assert_too_large(client.post("/limited", content=chunks(6, 6)))
    assert client.post("/free", content=b"x" * 1000).text == "1000"


def test_route_as_asgi_app(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Route("/", read_size, methods=["POST"], max_body_size=10))  # type: ignore[arg-type]
    assert_too_large(client.post("/", content=b"x" * 11))
    assert client.post("/", content=b"x" * 10).text == "10"


def test_inner_limits_override_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_size, methods=["POST"], max_body_size=1000),
            Route("/lower", read_size, methods=["POST"], max_body_size=5),
            Route("/inherit", read_size, methods=["POST"]),
            Mount(
                "/mount",
                routes=[
                    Route("/raise", read_size, methods=["POST"], max_body_size=1000),
                    Route("/inherit", read_size, methods=["POST"]),
                ],
                max_body_size=100,
            ),
            Mount(
                "/router",
                app=Router(routes=[Route("/", read_size, methods=["POST"])], max_body_size=500),
                max_body_size=50,
            ),
            Mount("/sub", app=Starlette(routes=[Route("/", read_size, methods=["POST"])], max_body_size=300)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # raising the application limit, with a declared and with a streamed body
    assert client.post("/raise", content=b"x" * 500).text == "500"
    assert client.post("/raise", content=chunks(300, 200)).text == "500"
    assert_too_large(client.post("/raise", content=b"x" * 1001))
    # lowering it
    assert client.post("/lower", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/lower", content=b"x" * 6))
    assert_too_large(client.post("/lower", content=chunks(3, 3)))
    # inheriting it
    assert_too_large(client.post("/inherit", content=b"x" * 11))
    # nested
    assert client.post("/mount/inherit", content=b"x" * 100).text == "100"
    assert_too_large(client.post("/mount/inherit", content=b"x" * 101))
    assert client.post("/mount/raise", content=b"x" * 1000).text == "1000"
    assert client.post("/router/", content=b"x" * 500).text == "500"
    assert_too_large(client.post("/router/", content=b"x" * 501))
    assert client.post("/sub/", content=b"x" * 300).text == "300"
    assert_too_large(client.post("/sub/", content=b"x" * 301))


def test_middleware_standalone(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        size = 0
        while True:
            message = await receive()
            size += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await PlainTextResponse(str(size))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(6, 6)))
    assert client.get("/").text == "0"


def test_middleware_no_limit_is_passthrough(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])])
    client = test_client_factory(RequestBodyLimitMiddleware(app))
    assert client.post("/", content=b"x" * 1000).text == "1000"


def test_middleware_in_starlette_stack(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_size, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(6, 6)))


def test_with_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)

    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_size, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=chunks(6, 6)))


def test_content_length_fail_fast(test_client_factory: TestClientFactory) -> None:
    calls = []

    async def endpoint(request: Request) -> Response:
        calls.append(1)
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/", endpoint, methods=["POST"]),
            Route("/raise", endpoint, methods=["POST"], max_body_size=1000),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # the endpoint never reads the body and is never called
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []
    assert client.post("/", content=b"x" * 10).text == "ok"
    assert client.post("/raise", content=b"x" * 100).text == "ok"
    # an unparseable header is ignored
    assert client.post("/", content=b"x", headers={"content-length": "abc"}).status_code in (200, 400, 413)


def test_content_length_fail_fast_standalone() -> None:
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        await PlainTextResponse("ok")(scope, receive, send)

    sent: list[dict[str, Any]] = []

    async def receive() -> Any:  # pragma: no cover
        raise AssertionError("the body must not be read")

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"11")]}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert not called
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


def test_understated_content_length() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":  # pragma: no cover
                return
            size += len(message["body"])
            if not message["more_body"]:
                break
        await PlainTextResponse(str(size))(scope, receive, send)

    sent: list[dict[str, Any]] = []
    messages = [
        {"type": "http.request", "body": b"x" * 8, "more_body": True},
        {"type": "http.request", "body": b"x" * 8, "more_body": False},
    ]

    async def receive() -> Any:
        return messages.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"3")]}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(sent) == 2


def test_response_already_started() -> None:
    sent: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            received.append(message)
            if message["type"] == "http.disconnect":
                break
        await receive()
        await send({"type": "http.response.body", "body": b"done"})

    messages = [
        {"type": "http.request", "body": b"x" * 8, "more_body": True},
        {"type": "http.request", "body": b"x" * 8, "more_body": True},
        {"type": "http.request", "body": b"x" * 8, "more_body": False},
    ]

    async def receive() -> Any:
        return messages.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert received[-1] == {"type": "http.disconnect"}
    assert len(messages) == 1  # the stream was not read further


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    started = []

    async def lifespan(app: Starlette) -> Any:
        started.append(True)
        yield

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    import contextlib

    app = Starlette(
        routes=[WebSocketRoute("/ws", ws)],
        lifespan=contextlib.asynccontextmanager(lifespan),
        max_body_size=1,
    )
    with test_client_factory(app) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("x" * 100)
            assert session.receive_text() == "x" * 100
    assert started


def test_no_body_requests(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", never_reads)], max_body_size=0)
    assert test_client_factory(app).get("/").text == "ok"


def test_multipart_counts_overhead(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> Response:
        async with request.form() as form:
            upload_file = form["file"]
            return JSONResponse({"size": upload_file.size})  # type: ignore[union-attr]

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    assert client.post("/", files={"file": ("a.txt", b"x" * 100)}).json() == {"size": 100}
    # the file data fits in the limit, the encoded request does not
    assert_too_large(client.post("/", files={"file": ("a.txt", b"x" * 990)}))

    def gen() -> Any:
        yield from chunks(*[100] * 20)

    boundary = "xyz"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'.encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def streamed() -> Any:
        yield head
        yield from gen()
        yield tail

    content_type = f"multipart/form-data; boundary={boundary}"
    response = client.post("/", content=streamed(), headers={"content-type": content_type})
    assert_too_large(response)


def test_multipart_closes_temp_files_when_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    import starlette.formparsers as formparsers

    opened: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def spy(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", spy)

    async def upload(request: Request) -> Response:
        async with request.form():
            pass  # pragma: no cover
        return PlainTextResponse("no")  # pragma: no cover

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300)
    head = b'--xyz\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
    messages = [
        {"type": "http.request", "body": head + b"x" * 100, "more_body": True},
        {"type": "http.request", "body": b"x" * 400, "more_body": True},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> Any:
        return messages.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=xyz")],
    }
    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(opened) == 1
    assert opened[0].closed


def test_multipart_closes_temp_files_on_any_interruption() -> None:
    import starlette.formparsers as formparsers
    from starlette.datastructures import Headers

    async def stream() -> Any:
        yield b'--b\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\nabc'
        raise KeyboardInterrupt

    parser = formparsers.MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(KeyboardInterrupt):
        anyio.run(parser.parse)
    assert parser._files_to_close_on_error and all(f.closed for f in parser._files_to_close_on_error)
