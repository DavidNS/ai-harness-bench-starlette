from __future__ import annotations

from contextlib import asynccontextmanager
from tempfile import SpooledTemporaryFile
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def echo_size(request: Request) -> JSONResponse:
    calls.append(request.url.path)
    return JSONResponse({"size": len(await request.body())})


async def no_read(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    return PlainTextResponse("ran")


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


def ok(client: Any, path: str, size: int) -> bool:
    response = client.post(path, content=b"x" * size)
    if response.status_code == 413:
        assert response.text == "Content Too Large"
        assert response.headers["content-type"].startswith("text/plain")
        return False
    assert response.status_code == 200
    return True


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", echo_size, methods=["POST"])]))
    assert ok(client, "/", 100_000)


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert ok(client, "/", 10)
    assert not ok(client, "/", 11)
    assert client.get("/", headers={}).status_code == 405  # no body, unaffected


def test_application_limit_zero(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=0)
    client = test_client_factory(app)
    assert ok(client, "/", 0)
    assert not ok(client, "/", 1)


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert ok(client, "/", 5)
    assert not ok(client, "/", 6)


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5),
            Route("/", echo_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert ok(client, "/m/", 5)
    assert not ok(client, "/m/", 6)
    assert ok(client, "/", 100)


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"], max_body_size=5)])
    client = test_client_factory(app)
    assert ok(client, "/", 5)
    assert not ok(client, "/", 6)


def test_inner_limits_raise_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/route", echo_size, methods=["POST"], max_body_size=100),
            Route("/plain", echo_size, methods=["POST"]),
            Mount("/mount", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=100),
            Mount(
                "/router",
                app=Router(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=100),
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path in ("/route", "/mount/", "/router/"):
        assert ok(client, path, 50), path
        assert not ok(client, path, 101), path
    assert not ok(client, "/plain", 50)
    assert ok(client, "/plain", 10)


def test_inner_limits_lower_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/route", echo_size, methods=["POST"], max_body_size=5),
            Mount("/mount", routes=[Route("/", echo_size, methods=["POST"], max_body_size=7)], max_body_size=5),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert ok(client, "/route", 5)
    assert not ok(client, "/route", 6)
    assert ok(client, "/mount/", 7)
    assert not ok(client, "/mount/", 8)


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=(b"12345" for _ in range(3)))
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert client.post("/", content=(b"12345" for _ in range(2))).status_code == 200


def test_content_length_fails_fast_after_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", no_read, methods=["POST"]),
            Route("/big", no_read, methods=["POST"], max_body_size=1000),
            Mount("/m", routes=[Route("/", no_read, methods=["POST"])], max_body_size=1000),
            Mount("/plain", app=PlainTextResponse("mounted")),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # The endpoints never read the body, only the Content-Length check can reject.
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/nowhere", content=b"x" * 11).status_code == 413  # no route
    assert client.get("/", headers={"content-length": "11"}).status_code == 413  # no method
    assert client.post("/plain/", content=b"x" * 11).status_code == 413
    assert client.post("/m", content=b"x" * 11).status_code == 413  # redirect_slashes
    assert calls == []
    assert client.post("/big", content=b"x" * 500).status_code == 200
    assert client.post("/m/", content=b"x" * 500).status_code == 200
    assert calls == ["/big", "/m/"]


def test_content_length_fail_fast_inner_lowering(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", no_read, methods=["POST"], max_body_size=5)], max_body_size=100)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert calls == []


def test_fail_fast_runs_before_route_middleware(test_client_factory: TestClientFactory) -> None:
    seen: list[str] = []

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            seen.append("spy")
            await self.app(scope, receive, send)

    app = Starlette(routes=[Route("/", no_read, methods=["POST"], middleware=[Middleware(Spy)], max_body_size=5)])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert seen == []


def test_exception_handlers_are_not_used(test_client_factory: TestClientFactory) -> None:
    def handler(request: Request, exc: Exception) -> PlainTextResponse:  # pragma: no cover
        return PlainTextResponse("handled", status_code=500)

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"])],
        max_body_size=5,
        exception_handlers={Exception: handler},
    )
    client = test_client_factory(app, raise_server_exceptions=True)
    response = client.post("/", content=b"x" * 6)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def asgi_scope(headers: list[tuple[bytes, bytes]] | None = None, method: str = "POST") -> Scope:
    return {"type": "http", "method": method, "path": "/", "headers": headers or [], "query_string": b""}


async def run(app: Any, scope: Scope, chunks: list[bytes]) -> list[Message]:
    sent: list[Message] = []
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
    ]

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def read_all(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    await PlainTextResponse(str(len(body)))(scope, receive, send)


@pytest.mark.anyio
async def test_middleware_with_plain_asgi_app() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    sent = await run(app, asgi_scope(), [b"12345", b"12345"])
    assert sent[-1]["body"] == b"10"
    sent = await run(app, asgi_scope(), [b"12345", b"123456"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


@pytest.mark.anyio
async def test_middleware_rejects_on_entry() -> None:
    ran = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        nonlocal ran
        ran = True

    app = RequestBodyLimitMiddleware(inner, max_body_size=10)
    sent = await run(app, asgi_scope([(b"content-length", b"11")]), [])
    assert sent[0]["status"] == 413
    assert not ran


@pytest.mark.anyio
async def test_understated_or_bad_content_length_does_not_let_a_body_through() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    for value in (b"1", b"abc", b"-3"):
        sent = await run(app, asgi_scope([(b"content-length", value)]), [b"x" * 11])
        assert sent[0]["status"] == 413, value


@pytest.mark.anyio
async def test_middleware_ignores_other_scopes() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    app = RequestBodyLimitMiddleware(inner, max_body_size=0)
    await app({"type": "websocket"}, None, None)  # type: ignore[arg-type]
    await app({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["websocket", "lifespan"]


def test_middleware_rejects_negative_limit() -> None:
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(read_all, max_body_size=-1)


def test_middleware_in_front_of_starlette(test_client_factory: TestClientFactory) -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", echo_size, methods=["POST"])]), max_body_size=5)
    client = test_client_factory(app)
    assert ok(client, "/", 5)
    assert not ok(client, "/", 6)


@pytest.mark.anyio
async def test_response_already_started_sends_no_second_response() -> None:
    async def streaming(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hello", "more_body": True})
        while True:
            message = await receive()
            if not message.get("more_body", False):  # pragma: no cover
                break

    app = RequestBodyLimitMiddleware(streaming, max_body_size=10)
    sent = await run(app, asgi_scope(), [b"x" * 6, b"x" * 6])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_stream_stays_stopped_after_limit() -> None:
    attempts: list[str] = []

    async def stubborn(scope: Scope, receive: Receive, send: Send) -> None:
        with pytest.raises(Exception) as first:
            await receive()
            await receive()
        attempts.append(type(first.value).__name__)
        with pytest.raises(Exception):
            await receive()
        await PlainTextResponse("late")(scope, receive, send)

    app = RequestBodyLimitMiddleware(stubborn, max_body_size=1)
    sent = await run(app, asgi_scope(), [b"ab", b"cd"])
    assert attempts == ["RequestBodyTooLarge"]
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


def test_unrelated_errors_still_propagate(test_client_factory: TestClientFactory) -> None:
    async def boom(request: Request) -> PlainTextResponse:
        raise RuntimeError("boom")

    client = test_client_factory(Starlette(routes=[Route("/", boom)], max_body_size=5))
    with pytest.raises(RuntimeError):
        client.get("/")


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    started: list[bool] = []

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        started.append(True)
        yield

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=0, lifespan=lifespan)
    with test_client_factory(app) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("x" * 100)
            assert session.receive_text() == "x" * 100
    assert started == [True]


# Multipart


async def form_endpoint(request: Request) -> JSONResponse:
    async with request.form() as form:
        return JSONResponse({"keys": sorted(form.keys())})


def test_multipart_counts_file_data_and_overhead(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    assert client.post("/", files={"f": ("a.txt", b"x" * 100)}).status_code == 200
    # The file alone is under the limit, the encoded body is not.
    response = client.post("/", files={"f": ("a.txt", b"x" * 990)})
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_multipart_streamed_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=500)
    client = test_client_factory(app)
    boundary = b"boundary"
    head = b'--boundary\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\n'

    def body() -> Any:
        yield head
        for _ in range(10):
            yield b"x" * 100
        yield b"\r\n--boundary--\r\n"

    response = client.post(
        "/", content=body(), headers={"content-type": f"multipart/form-data; boundary={boundary.decode()}"}
    )
    assert response.status_code == 413


def test_urlencoded_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", data={"a": "1"}).status_code == 200
    assert client.post("/", data={"a": "1" * 20}).status_code == 413


class TrackedFile(SpooledTemporaryFile):  # type: ignore[type-arg]
    instances: list[TrackedFile] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        TrackedFile.instances.append(self)


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    TrackedFile.instances = []
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", TrackedFile)
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=1000)
    head = b'--b\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n\r\n'
    chunks = [head % (b"a", b"a") + b"x" * 10 + b"\r\n", head % (b"b", b"b")]
    chunks += [b"y" * 100 for _ in range(20)] + [b"\r\n--b--\r\n"]
    scope = asgi_scope([(b"content-type", b"multipart/form-data; boundary=b")])
    sent = await run(app, scope, chunks)  # no Content-Length: the parser starts before the limit is crossed
    assert sent[0]["status"] == 413
    assert len(TrackedFile.instances) == 2
    assert all(f.closed for f in TrackedFile.instances)


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_any_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    TrackedFile.instances = []
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", TrackedFile)

    async def stream() -> Any:
        yield b'--b\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\nabc'
        raise ConnectionResetError("interrupted")

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(ConnectionResetError):
        await parser.parse()
    assert len(TrackedFile.instances) == 1
    assert TrackedFile.instances[0].closed


def test_no_content_length_header_is_not_rejected_early(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(max_body_size=5))
    assert client.get("/nowhere").status_code == 404


@pytest.mark.anyio
async def test_non_request_messages_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((await receive())["type"])

    await run(RequestBodyLimitMiddleware(inner, max_body_size=0), asgi_scope(), [])
    assert seen == ["http.disconnect"]


def test_custom_route_is_checked_before_handling(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import BaseRoute, Match

    class Custom(BaseRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            return Match.FULL, {}

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
            calls.append("custom")

    client = test_client_factory(Starlette(routes=[Custom()], max_body_size=5))
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert calls == []


@pytest.mark.anyio
async def test_inner_limit_below_already_received_bytes() -> None:
    inner = Router(routes=[Route("/", no_read, methods=["POST"], max_body_size=3)])

    async def reads_first(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await inner(scope, receive, send)

    sent = await run(RequestBodyLimitMiddleware(reads_first, max_body_size=100), asgi_scope(), [b"x" * 5])
    assert sent[0]["status"] == 413
    assert calls == []
