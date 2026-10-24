from __future__ import annotations

from collections.abc import Callable
from tempfile import SpooledTemporaryFile
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def size(request: Request) -> Response:
    calls.append(request.url.path)
    return PlainTextResponse(str(len(await request.body())))


async def upload(request: Request) -> Response:
    calls.append(request.url.path)
    async with request.form() as form:
        return PlainTextResponse(str(len(form)))


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    calls.clear()


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert client.get("/missing").status_code == 404


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", size, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_chunked_body_is_counted(test_client_factory: TestClientFactory) -> None:
    def chunks() -> Any:
        for _ in range(5):
            yield b"x" * 4

    client = test_client_factory(Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10))
    assert_too_large(client.post("/", content=chunks()))


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10))
    assert_too_large(client.post("/", content=b"x" * 50, headers={"content-length": "1"}))


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/", content=b"x" * 6))
    assert calls == ["/"]


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/small", routes=[Route("/", size, methods=["POST"])], max_body_size=5),
            Route("/free", size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small/", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/small/", content=b"x" * 6))
    assert client.post("/free", content=b"x" * 600).text == "600"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/small", size, methods=["POST"], max_body_size=5), Route("/free", size, methods=["POST"])]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/small", content=b"x" * 6))
    assert client.post("/free", content=b"x" * 600).text == "600"


def test_inner_limit_raises_and_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", size, methods=["POST"], max_body_size=100),
            Route("/lowered", size, methods=["POST"], max_body_size=3),
            Mount("/mount", routes=[Route("/raised", size, methods=["POST"], max_body_size=100)], max_body_size=50),
            Mount(
                "/router",
                app=Router(routes=[Route("/", size, methods=["POST"])], max_body_size=30),
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raised", content=b"x" * 100).text == "100"
    assert_too_large(client.post("/raised", content=b"x" * 101))
    assert_too_large(client.post("/lowered", content=b"x" * 4))
    assert client.post("/mount/raised", content=b"x" * 100).text == "100"
    assert client.post("/router/", content=b"x" * 30).text == "30"
    assert_too_large(client.post("/router/", content=b"x" * 31))


def test_content_length_fail_fast_in_application(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", size, methods=["POST"]),
            Route("/raised", size, methods=["POST"], max_body_size=100),
            Route("/lowered", size, methods=["POST"], max_body_size=3),
            Mount("/mount", routes=[Route("/", size, methods=["POST"])], max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    headers = {"content-length": "50"}
    # The declared length is over the application limit, but an inner limit may raise it.
    assert client.post("/raised", content=b"x" * 50).text == "50"
    assert calls == ["/raised"]
    assert client.post("/mount/", content=b"x" * 50).text == "50"
    calls.clear()

    for path in ["/", "/lowered", "/missing"]:
        assert_too_large(client.post(path, content=b"", headers=headers))
    # A method not handled by the route uses the application limit, not the route's.
    assert_too_large(client.get("/lowered", headers=headers))
    assert client.get("/raised", headers={"content-length": "5"}).status_code == 405
    assert calls == []


def test_content_length_fail_fast_standalone(test_client_factory: TestClientFactory) -> None:
    reached = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reached
        reached = True
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert_too_large(client.post("/", content=b"", headers={"content-length": "11"}))
    assert not reached
    assert client.post("/", content=b"x" * 10).text == "ok"
    assert client.post("/", content=b"", headers={"content-length": "garbage"}).text == "ok"


def test_standalone_middleware_with_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await PlainTextResponse(str(len(body)))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))


def test_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", size, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=b"", headers={"content-length": "11"}))


def test_requests_without_body_and_other_scopes(test_client_factory: TestClientFactory) -> None:
    async def get(request: Request) -> Response:
        return PlainTextResponse("ok")

    with test_client_factory(Starlette(routes=[Route("/", get)], max_body_size=0)) as client:
        assert client.get("/").text == "ok"


def test_websocket_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    client = test_client_factory(Starlette(routes=[WebSocketRoute("/", ws)], max_body_size=1))
    with client.websocket_connect("/") as session:
        session.send_text("a long message")
        assert session.receive_text() == "a long message"


def test_response_already_started() -> None:
    sent: list[Message] = []
    receive_calls = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal receive_calls
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            receive_calls += 1
            message = await receive()
            if message["type"] == "http.disconnect":
                break

    messages = [{"type": "http.request", "body": b"x" * 6, "more_body": True} for _ in range(3)]

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert receive_calls == 2  # The stream was stopped, the remaining message is never read.
    assert len(messages) == 1


def test_application_swallowing_the_error_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def swallow(request: Request) -> Response:
        try:
            await request.body()
        except Exception:
            return PlainTextResponse("handled", status_code=400)
        return PlainTextResponse("ok")  # pragma: no cover

    client = test_client_factory(Starlette(routes=[Route("/", swallow, methods=["POST"])], max_body_size=3))
    assert_too_large(client.post("/", content=b"x" * 4))


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300))
    assert client.post("/", files={"f": ("a.txt", b"x" * 10)}).text == "1"
    # The file itself fits in the limit, the multipart overhead doesn't.
    assert_too_large(client.post("/", files={"f": ("a.txt", b"x" * 290)}))
    assert_too_large(client.post("/", files={"f": ("a.txt", b"x" * 1000)}))


def test_multipart_closes_temporary_files(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[SpooledTemporaryFile[bytes]] = []
    factory: Callable[..., SpooledTemporaryFile[bytes]] = SpooledTemporaryFile

    def recording(*args: Any, **kwargs: Any) -> SpooledTemporaryFile[bytes]:
        file = factory(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", recording)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=500)

    # The body arrives in chunks, so the parser is interrupted by the limit after the files were opened.
    chunks = [
        b'--b\r\nContent-Disposition: form-data; name="a"; filename="a.txt"\r\n\r\n' + b"x" * 100,
        b'\r\n--b\r\nContent-Disposition: form-data; name="b"; filename="b.txt"\r\n\r\n' + b"y" * 100,
        b"y" * 1000,
    ]
    messages: list[Message] = [{"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
    }
    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(created) == 2
    assert all(file.closed for file in created)


def test_multipart_closes_temporary_files_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    created: list[SpooledTemporaryFile[bytes]] = []
    factory: Callable[..., SpooledTemporaryFile[bytes]] = SpooledTemporaryFile

    def recording(*args: Any, **kwargs: Any) -> SpooledTemporaryFile[bytes]:
        file = factory(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", recording)

    async def stream() -> Any:
        yield (b'--b\r\nContent-Disposition: form-data; name="f"; filename="f.txt"\r\n\r\nabc')
        raise RuntimeError("interrupted")

    async def run() -> None:
        parser = MultiPartParser(Headers({"Content-Type": "multipart/form-data; boundary=b"}), stream())
        await parser.parse()

    with pytest.raises(RuntimeError):
        anyio.run(run)
    assert len(created) == 1 and created[0].closed


def test_other_errors_are_not_swallowed(test_client_factory: TestClientFactory) -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    client = test_client_factory(RequestBodyLimitMiddleware(broken, max_body_size=10))
    with pytest.raises(RuntimeError, match="boom"):
        client.post("/", content=b"x")


def test_swallowed_error_replaces_the_response() -> None:
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception:
            pass
        await PlainTextResponse("handled", status_code=400)(scope, receive, send)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 11}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"
