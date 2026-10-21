from __future__ import annotations

from collections.abc import Iterator
from tempfile import SpooledTemporaryFile
from typing import Any
from unittest import mock

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def size(request: Request) -> Response:
    return PlainTextResponse(str(len(await request.body())))


def chunks(*sizes: int) -> Iterator[bytes]:
    for n in sizes:
        yield b"x" * n


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])])
    response = test_client_factory(app).post("/", content=b"x" * 100_000)
    assert response.text == "100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit_applies_to_unknown_routes_and_bodyless(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["GET", "POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.get("/").text == "0"
    assert client.get("/missing").status_code == 404


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"12345").text == "5"
    assert client.post("/", content=b"123456").status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/free", size, methods=["POST"]),
            Mount("/m", routes=[Route("/", size, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/free", content=b"x" * 100).text == "100"
    assert client.post("/m/", content=b"x" * 5).text == "5"
    assert client.post("/m/", content=b"x" * 6).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/free", size, methods=["POST"]),
            Route("/limited", size, methods=["POST"], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/free", content=b"x" * 100).text == "100"
    assert client.post("/limited", content=b"x" * 5).text == "5"
    assert client.post("/limited", content=b"x" * 6).status_code == 413


def test_route_limit_with_class_endpoint(test_client_factory: TestClientFactory) -> None:
    class Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            response = await size(Request(scope, receive))
            await response(scope, receive, send)

    endpoint = Endpoint()
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"], max_body_size=3)])
    assert test_client_factory(app).post("/", content=b"xxxx").status_code == 413


def test_inner_limits_raise_and_lower_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/outer", size, methods=["POST"]),
            Route("/raised", size, methods=["POST"], max_body_size=100),
            Route("/lowered", size, methods=["POST"], max_body_size=5),
            Mount(
                "/m",
                routes=[
                    Route("/", size, methods=["POST"]),
                    Route("/raised", size, methods=["POST"], max_body_size=1000),
                ],
                max_body_size=50,
            ),
        ],
        max_body_size=20,
    )
    client = test_client_factory(app)
    assert client.post("/outer", content=b"x" * 20).text == "20"
    assert client.post("/outer", content=b"x" * 21).status_code == 413
    assert client.post("/raised", content=b"x" * 100).text == "100"
    assert client.post("/raised", content=b"x" * 101).status_code == 413
    assert client.post("/lowered", content=b"x" * 5).text == "5"
    assert client.post("/lowered", content=b"x" * 6).status_code == 413
    assert client.post("/m/", content=b"x" * 50).text == "50"
    assert client.post("/m/", content=b"x" * 51).status_code == 413
    assert client.post("/m/raised", content=b"x" * 1000).text == "1000"
    assert client.post("/m/raised", content=b"x" * 1001).status_code == 413


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(5, 5)).text == "10"
    assert client.post("/", content=chunks(5, 5, 1)).status_code == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 20, headers={"Content-Length": "5"})
    assert response.status_code == 413


def test_content_length_fail_fast_without_reading(test_client_factory: TestClientFactory) -> None:
    received = False

    async def never_reads(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    async def counting_receive_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def spy() -> Message:
            nonlocal received
            received = True
            return await receive()

        await RequestBodyLimitMiddleware(never_reads, max_body_size=10)(scope, spy, send)

    client = test_client_factory(counting_receive_app)
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert not received
    assert client.post("/", content=b"x" * 10).text == "ok"


def test_content_length_fail_fast_does_not_defeat_inner_raise(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"], max_body_size=100)], max_body_size=10)
    assert test_client_factory(app).post("/", content=b"x" * 50).text == "50"


def test_invalid_content_length_is_ignored() -> None:
    from starlette.middleware.body_limit import _declared_length

    for value in (b"abc", b"-5", b"", b"\xb2"):
        assert _declared_length({"headers": [(b"content-length", value)]}) is None
    assert _declared_length({"headers": []}) is None
    assert _declared_length({"headers": [(b"content-length", b" 7 ")]}) == 7


def test_standalone_middleware_with_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=8))
    assert client.post("/", content=b"x" * 8).text == "8"
    response = client.post("/", content=chunks(4, 4, 4))
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_middleware_without_limit_is_a_passthrough(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(Starlette(routes=[Route("/", size, methods=["POST"])])))
    assert client.post("/", content=b"x" * 5000).text == "5000"


def test_middleware_in_application_stack(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", size, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=4)],
    )
    assert test_client_factory(app).post("/", content=b"x" * 5).status_code == 413


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    sent: list[Message] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            message = await receive()
            if not message.get("more_body", False):  # pragma: no cover
                break

    async def recording_send_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def record(message: Message) -> None:
            sent.append(message)
            await send(message)

        await RequestBodyLimitMiddleware(asgi, max_body_size=4)(scope, receive, record)

    client = test_client_factory(recording_send_app)
    client.post("/", content=chunks(3, 3))
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


def test_swallowed_exception_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await Request(scope, receive).body()
        except Exception:
            pass
        await PlainTextResponse("handled")(scope, receive, send)

    response = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=2)).post("/", content=b"xxx")
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_stream_is_stopped_after_limit() -> None:
    upstream_reads = 0
    seen: list[int] = []

    async def receive() -> Message:
        nonlocal upstream_reads
        upstream_reads += 1
        return {"type": "http.request", "body": b"abc", "more_body": True}

    async def send(message: Message) -> None:
        seen.append(message.get("status", 0))

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        with pytest.raises(RequestBodyTooLarge):
            await receive()
        # Once over the limit the stream stays stopped, the server is not read again.
        reads_at_limit = upstream_reads
        with pytest.raises(RequestBodyTooLarge):
            await receive()
        assert upstream_reads == reads_at_limit
        raise RuntimeError("done")

    async def main() -> None:
        scope: Scope = {"type": "http", "headers": []}
        with pytest.raises(RuntimeError, match="done"):
            await RequestBodyLimitMiddleware(asgi, max_body_size=5)(scope, receive, send)

    anyio.run(main)
    assert upstream_reads == 2


def test_streaming_response_started_before_limit(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"first"
            await request.body()
            yield b"second"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=2)
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.post("/", content=b"xxx")
    # Content-Length is over the limit, so the response is replaced before it starts.
    assert response.status_code == 413


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["file"].read())))  # type: ignore[union-attr]

    boundary = "boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n" + "x" * 100 + f"\r\n--{boundary}--\r\n"
    ).encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    ok = test_client_factory(Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=len(body)))
    assert ok.post("/", content=body, headers=headers).text == "100"

    # The file data alone fits in the limit, the multipart overhead does not.
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=len(body) - 1)
    assert test_client_factory(app).post("/", content=body, headers=headers).status_code == 413
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=100)
    assert test_client_factory(app).post("/", content=body, headers=headers).status_code == 413


def test_multipart_closes_tempfiles_when_limit_crossed(test_client_factory: TestClientFactory) -> None:
    created: list[SpooledTemporaryFile[bytes]] = []

    class TrackedFile(SpooledTemporaryFile[bytes]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    async def endpoint(request: Request) -> Response:
        await request.form()  # pragma: no cover
        return PlainTextResponse("no")  # pragma: no cover

    boundary = "boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n' + "x" * 100 + f"\r\n--{boundary}--\r\n"
    ).encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    # Sent in two ASGI messages, so the limit is crossed while the file is being received.
    messages: list[Message] = [
        {"type": "http.request", "body": body[:120], "more_body": True},
        {"type": "http.request", "body": body[120:], "more_body": False},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=150)
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-type", headers["Content-Type"].encode())],
    }
    with mock.patch("starlette.formparsers.SpooledTemporaryFile", TrackedFile):
        anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert created[0].closed


def test_multipart_closes_tempfiles_on_cancellation() -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    created: list[SpooledTemporaryFile[bytes]] = []

    class TrackedFile(SpooledTemporaryFile[bytes]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    first = (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        b"data"
    )

    async def stream() -> Any:
        yield first
        await anyio.sleep_forever()

    async def main() -> None:
        parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
        with anyio.move_on_after(0.1):
            await parser.parse()

    with mock.patch("starlette.formparsers.SpooledTemporaryFile", TrackedFile):
        anyio.run(main)
    assert len(created) == 1
    assert created[0].closed


def test_lifespan_and_websocket_are_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    started = False

    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        nonlocal started
        started = True
        yield

    from starlette.routing import WebSocketRoute

    app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)], lifespan=lifespan, max_body_size=1)
    with test_client_factory(app) as client:
        assert started
        with client.websocket_connect("/ws") as ws:
            ws.send_text("hello world")
            assert ws.receive_text() == "hello world"
