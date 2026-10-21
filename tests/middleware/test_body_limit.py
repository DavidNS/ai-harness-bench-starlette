from __future__ import annotations

from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def test_under_and_over_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).text == "100000"


def test_no_body_request(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", PlainTextResponse("ok"))], max_body_size=0)
    assert test_client_factory(app).get("/").text == "ok"


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def gen() -> Any:
        for _ in range(4):
            yield b"abc"

    assert client.post("/", content=gen()).status_code == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert response.status_code == 413


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"12345").status_code == 200
    assert client.post("/", content=b"123456").status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"123456").status_code == 413
    assert client.post("/m/", content=b"12345").status_code == 200
    assert client.post("/free", content=b"x" * 1000).status_code == 200


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/a", read_body, methods=["POST"], max_body_size=5),
            Route("/b", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/a", content=b"123456").status_code == 413
    assert client.post("/b", content=b"123456").status_code == 200


def test_inner_limit_raises_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=50),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 11).status_code == 413
    assert client.post("/big", content=b"x" * 100).status_code == 200
    assert client.post("/big", content=b"x" * 101).status_code == 413
    assert client.post("/m/", content=b"x" * 50).status_code == 200
    assert client.post("/m/", content=b"x" * 51).status_code == 413


def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/a", read_body, methods=["POST"], max_body_size=5),
            Route("/b", read_body, methods=["POST"]),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/a", content=b"x" * 6).status_code == 413
    assert client.post("/a", content=b"x" * 5).status_code == 200
    assert client.post("/b", content=b"x" * 100).status_code == 200


def test_content_length_fail_fast_without_reading(test_client_factory: TestClientFactory) -> None:
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert not called


def test_inner_content_length_fail_fast(test_client_factory: TestClientFactory) -> None:
    async def never_reads(request: Request) -> Response:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", never_reads, methods=["POST"], max_body_size=5)], max_body_size=100)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 6)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_invalid_content_length_ignored() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    scope: Scope = {"type": "http", "headers": [(b"content-length", b"abc")]}
    messages: list[Message] = []

    async def receive() -> Message:  # pragma: no cover
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        messages.append(message)

    import anyio

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=1), scope, receive, send)
    assert messages[0]["status"] == 200


def test_standalone_middleware_plain_asgi(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11, headers={"content-length": "1"})
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            if not message.get("more_body", False):  # pragma: no cover
                break
        await send({"type": "http.response.body", "body": b"done"})  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"xxxxxx", "more_body": True}

    import anyio

    scope: Scope = {"type": "http", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_stream_stopped_after_limit() -> None:
    import anyio

    reads = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reads
        while True:  # pragma: no branch
            reads += 1
            await receive()

    async def receive() -> Message:
        return {"type": "http.request", "body": b"xxxxxx", "more_body": True}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), {"type": "http", "headers": []}, receive, send)
    assert reads == 2
    assert sent[0]["status"] == 413


def test_streaming_response_not_affected(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        await request.body()

        async def gen() -> Any:
            yield b"a"
            yield b"b"

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    assert test_client_factory(app).post("/", content=b"x").text == "ab"


def test_lifespan_and_websocket_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app) as client:  # runs lifespan
        with client.websocket_connect("/ws") as session:
            session.send_text("much longer than one byte")
            assert session.receive_text() == "much longer than one byte"


def test_multipart_over_limit(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:  # pragma: no cover
            return PlainTextResponse(str(len(form)))

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    response = client.post("/", files={"file": ("a.txt", b"x" * 5000)})
    assert response.status_code == 413
    ok = client.post("/", files={"file": ("a.txt", b"x" * 100)})
    assert ok.text == "1"
    # Headers and boundaries count as well as the file data.
    exact = client.post("/", files={"file": ("a.txt", b"x" * 900)})
    assert exact.status_code == 413


def test_multipart_tempfiles_closed_on_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    created: list[Any] = []
    original = MultiPartParser.on_headers_finished

    def spy(self: MultiPartParser) -> None:
        original(self)
        if self._current_part.file is not None:
            created.append(self._current_part.file.file)

    monkeypatch.setattr(MultiPartParser, "on_headers_finished", spy)

    async def endpoint(request: Request) -> Response:
        async with request.form():  # pragma: no cover
            return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=1500)
    boundary = b"b"
    body = (
        b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n'
        + b"x" * 1000
        + b'\r\n--b\r\nContent-Disposition: form-data; name="g"; filename="b.txt"\r\n\r\n'
        + b"y" * 1000
        + b"\r\n--b--\r\n"
    )
    chunks = [body[i : i + 500] for i in range(0, len(body), 500)]
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=" + boundary)],
    }
    sent: list[Message] = []

    async def receive() -> Message:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: Message) -> None:
        sent.append(message)

    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413
    assert created
    assert all(f.closed for f in created)


def test_multipart_tempfiles_closed_on_any_interruption() -> None:
    import anyio

    from starlette.datastructures import Headers

    boundary = "b"
    head = b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\ndata'

    async def stream() -> Any:
        yield head
        raise RuntimeError("interrupted")

    parser = MultiPartParser(Headers({"content-type": f"multipart/form-data; boundary={boundary}"}), stream())

    async def run() -> None:
        with pytest.raises(RuntimeError):
            await parser.parse()

    anyio.run(run)
    assert parser._files_to_close_on_error
    assert all(f.closed for f in parser._files_to_close_on_error)
