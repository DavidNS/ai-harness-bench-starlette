from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest

import starlette.formparsers as formparsers
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.bodylimit import RequestBodyLimitMiddleware, _BodyLimitState
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, Router, WebSocketRoute
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocket
from tests.types import TestClientFactory


async def read_body(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"size": len(body)})


def make_app(**kwargs: object) -> Starlette:
    return Starlette(routes=[Route("/", read_body, methods=["POST"])], **kwargs)  # type: ignore[arg-type]


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app())
    assert client.post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app(max_body_size=10))
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit_applies_to_streamed_body(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app(max_body_size=10))

    def chunks() -> Generator[bytes, None, None]:
        yield b"x" * 6
        yield b"x" * 6

    assert client.post("/", content=chunks()).status_code == 413


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert client.post("/", content=b"x" * 600).status_code == 200


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Route("/any", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 5).status_code == 200
    assert client.post("/small", content=b"x" * 6).status_code == 413
    assert client.post("/any", content=b"x" * 600).status_code == 200


def test_inner_limit_raises_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=50),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/big", content=b"x" * 100).status_code == 200
    assert client.post("/big", content=b"x" * 101).status_code == 413
    assert client.post("/m/", content=b"x" * 50).status_code == 200
    assert client.post("/m/", content=b"x" * 51).status_code == 413


def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/small", read_body, methods=["POST"], max_body_size=5),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100).status_code == 200
    assert client.post("/small", content=b"x" * 5).status_code == 200
    assert client.post("/small", content=b"x" * 6).status_code == 413


def test_middleware_standalone() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, 4))
    assert client.post("/", content=b"1234").text == "ok"
    assert client.post("/", content=b"12345").status_code == 413
    assert TestClient(RequestBodyLimitMiddleware(app, None)).post("/", content=b"x" * 99).text == "ok"


@pytest.mark.anyio
async def test_content_length_fail_fast() -> None:
    called = False
    received_calls = 0
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True  # pragma: no cover

    async def receive() -> Message:
        nonlocal received_calls
        received_calls += 1  # pragma: no cover
        return {"type": "http.request"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"100")]}
    await RequestBodyLimitMiddleware(app, 10)(scope, receive, send)
    assert not called
    assert received_calls == 0
    assert sent[0]["status"] == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(make_app(max_body_size=10))
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert response.status_code == 413


def test_invalid_content_length_is_ignored() -> None:
    assert _BodyLimitState(10, {"headers": [(b"content-length", b"abc")]}).declared is None
    assert _BodyLimitState(10, {"headers": [(b"content-length", b"-3")]}).declared is None


@pytest.mark.anyio
async def test_response_already_started() -> None:
    sent: list[Message] = []
    chunks = [b"123", b"456"]

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi", "more_body": True})
        async for _ in Request(scope, receive).stream():
            pass  # pragma: no cover

    async def receive() -> Message:
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(app, 4)(scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert chunks == []


def test_websocket_not_limited(test_client_factory: TestClientFactory) -> None:
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    client = test_client_factory(Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1))
    with client.websocket_connect("/ws") as session:
        session.send_text("hello world")
        assert session.receive_text() == "hello world"


def test_user_middleware_with_limit(test_client_factory: TestClientFactory) -> None:
    app = make_app(max_body_size=1, middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=100)])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).status_code == 200


def test_multipart(test_client_factory: TestClientFactory) -> None:
    async def handler(request: Request) -> JSONResponse:
        async with request.form() as form:
            return JSONResponse({"files": len(form.getlist("f"))})

    app = Starlette(routes=[Route("/", handler, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    assert client.post("/", files={"f": ("a.txt", b"a" * 100)}).json() == {"files": 1}
    # File data and multipart encoding overhead count towards the limit.
    assert client.post("/", files={"f": ("a.txt", b"a" * 990)}).status_code == 413
    assert client.post("/", files={"f": ("a.txt", b"a" * 5000)}).status_code == 413


@pytest.mark.anyio
async def test_multipart_closes_files_on_any_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[object] = []
    original = formparsers.SpooledTemporaryFile

    class Tracked(original):  # type: ignore[valid-type, misc]
        def close(self) -> None:
            closed.append(self)
            super().close()

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Tracked)

    body = b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n' + b"a" * 10

    class Interrupted(Exception):
        pass

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body
        raise Interrupted()

    headers = Headers({"Content-Type": "multipart/form-data; boundary=b"})
    with pytest.raises(Interrupted):
        await MultiPartParser(headers, stream()).parse()
    assert len(closed) == 1
