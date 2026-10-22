from __future__ import annotations

import anyio
import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

ran: list[str] = []


async def read_body(request: Request) -> Response:
    ran.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


async def plain_app(scope: Scope, receive: Receive, send: Send) -> None:
    ran.append(scope["path"])
    request = Request(scope, receive)
    body = await request.body()
    await PlainTextResponse(f"read {len(body)}")(scope, receive, send)


@pytest.fixture(autouse=True)
def reset_ran() -> None:
    ran.clear()


async def call_asgi(
    app: object, chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None, method: str = "POST"
) -> tuple[list[Message], list[str]]:
    """Drive an ASGI app by hand, so Content-Length can be absent or wrong."""
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers or [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }
    pending: list[Message] = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        await anyio.sleep_forever()  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)  # type: ignore[operator]
    return sent, [m["type"] for m in sent]


def status_and_body(sent: list[Message]) -> tuple[int, bytes]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


# --- Starlette application -------------------------------------------------------------


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 1_000_000).text == "read 1000000"


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_app_limit_without_content_length_or_understated() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent, _ = anyio.run(call_asgi, app, [b"x" * 6, b"x" * 6])
    assert status_and_body(sent) == (413, b"Content Too Large")
    sent, _ = anyio.run(call_asgi, app, [b"x" * 6, b"x" * 6], [(b"content-length", b"5")])
    assert status_and_body(sent) == (413, b"Content Too Large")


def test_app_limit_exact_chunks_ok() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent, _ = anyio.run(call_asgi, app, [b"x" * 5, b"x" * 5])
    assert status_and_body(sent) == (200, b"read 10")


def test_get_without_body_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=1)
    assert test_client_factory(app).get("/").text == "read 0"


def test_websocket_and_lifespan_unaffected() -> None:
    events: list[str] = []

    async def ws(websocket):  # type: ignore[no-untyped-def]
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    from contextlib import asynccontextmanager

    from starlette.routing import WebSocketRoute

    @asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        events.append("up")
        yield

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], lifespan=lifespan, max_body_size=1)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as session:
            assert session.receive_text() == "hi"
    assert events == ["up"]


def test_invalid_content_length_header_ignored() -> None:
    app = RequestBodyLimitMiddleware(plain_app, max_body_size=10)
    sent, _ = anyio.run(call_asgi, app, [b"abc"], [(b"content-length", b"abc")])
    assert status_and_body(sent) == (200, b"read 3")


# --- Router / Mount / Route ------------------------------------------------------------


def test_router_limit() -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = TestClient(router)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_route_limit() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=5)])
    client = TestClient(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_route_raises_app_limit() -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
        ],
        max_body_size=5,
    )
    client = TestClient(app)
    assert client.post("/small", content=b"x" * 50).status_code == 413
    assert client.post("/big", content=b"x" * 50).text == "read 50"
    assert ran == ["/big"]
    assert client.post("/big", content=b"x" * 101).status_code == 413


def test_route_lowers_app_limit() -> None:
    app = Starlette(
        routes=[
            Route("/open", read_body, methods=["POST"]),
            Route("/tight", read_body, methods=["POST"], max_body_size=5),
        ],
        max_body_size=100,
    )
    client = TestClient(app)
    assert client.post("/open", content=b"x" * 50).status_code == 200
    assert client.post("/tight", content=b"x" * 50).status_code == 413


def test_mount_router_limits() -> None:
    app = Starlette(
        routes=[
            Mount("/up", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount("/down", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Mount("/inherit", routes=[Route("/", read_body, methods=["POST"])]),
        ],
        max_body_size=20,
    )
    client = TestClient(app)
    assert client.post("/up/", content=b"x" * 50).status_code == 200
    assert client.post("/down/", content=b"x" * 10).status_code == 413
    assert client.post("/inherit/", content=b"x" * 10).status_code == 200
    assert client.post("/inherit/", content=b"x" * 30).status_code == 413


def test_mount_app_limit_plain_asgi() -> None:
    app = Starlette(routes=[Mount("/m", app=plain_app, max_body_size=5)])
    client = TestClient(app)
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    ran.clear()
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert ran == []


def test_mounted_starlette_inner_limit_raises() -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/m", app=inner, max_body_size=5)])
    client = TestClient(app)
    assert client.post("/m/", content=b"x" * 50).status_code == 200


def test_inner_router_limit_overrides_outer_router() -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/m", app=inner)], max_body_size=5)
    client = TestClient(app)
    assert client.post("/m/", content=b"x" * 50).status_code == 200


# --- Content-Length fail-fast ----------------------------------------------------------


def test_content_length_fail_fast_route() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    sent, types = anyio.run(call_asgi, app, [], [(b"content-length", b"6")])
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert ran == []


def test_content_length_fail_fast_uses_route_limit() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=5)
    sent, _ = anyio.run(call_asgi, app, [b"x" * 50], [(b"content-length", b"50")])
    assert status_and_body(sent) == (200, b"read 50")


def test_content_length_fail_fast_no_route_and_no_method() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["GET"], max_body_size=100)], max_body_size=5)
    sent, _ = anyio.run(call_asgi, app, [], [(b"content-length", b"6")])  # POST -> 405 uses app limit
    assert status_and_body(sent) == (413, b"Content Too Large")
    # a declared length under the limit still gets the 405
    sent, _ = anyio.run(call_asgi, app, [], [(b"content-length", b"2")])
    assert status_and_body(sent)[0] == 405

    async def nothing(request: Request) -> Response:  # pragma: no cover
        raise AssertionError

    sent, _ = anyio.run(call_asgi, Starlette(max_body_size=5), [], [(b"content-length", b"6")])
    assert status_and_body(sent) == (413, b"Content Too Large")
    sent, _ = anyio.run(call_asgi, Starlette(max_body_size=5), [], [(b"content-length", b"1")])
    assert status_and_body(sent)[0] == 404


def test_content_length_fail_fast_mount() -> None:
    app = Starlette(routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])])], max_body_size=5)
    sent, _ = anyio.run(call_asgi, app, [], [(b"content-length", b"6")], "POST")
    assert status_and_body(sent)[0] == 413  # path "/" is unmatched: app limit
    assert ran == []


def test_content_length_redirect_uses_limit() -> None:
    app = Starlette(routes=[Route("/x/", read_body, methods=["POST"])], max_body_size=5)
    client = TestClient(app, follow_redirects=False)
    assert client.post("/x", content=b"x" * 6).status_code == 413
    assert client.post("/x", content=b"x").status_code == 307


def test_middleware_content_length_rejects_on_entry() -> None:
    app = RequestBodyLimitMiddleware(plain_app, max_body_size=5)
    sent, _ = anyio.run(call_asgi, app, [], [(b"content-length", b"6")])
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert ran == []


# --- Standalone middleware -------------------------------------------------------------


def test_middleware_plain_asgi_app() -> None:
    client = TestClient(RequestBodyLimitMiddleware(plain_app, max_body_size=5))
    assert client.post("/", content=b"x" * 5).text == "read 5"
    response = client.post("/", content=b"x" * 6)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_middleware_counts_stream_without_header() -> None:
    app = RequestBodyLimitMiddleware(plain_app, max_body_size=5)
    sent, _ = anyio.run(call_asgi, app, [b"abc", b"def"])
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert [m["type"] for m in sent].count("http.response.start") == 1


def test_middleware_non_http_passthrough() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=1), {"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_swallowed_exception_does_not_send_second_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while (await receive()).get("more_body"):
                pass
        except RequestBodyTooLarge:
            with pytest.raises(RequestBodyTooLarge):
                await receive()
        await PlainTextResponse("late")(scope, receive, send)

    sent, types = anyio.run(call_asgi, RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"def"])
    assert types == ["http.response.start", "http.response.body"]
    assert status_and_body(sent) == (413, b"Content Too Large")


# --- Response already started ----------------------------------------------------------


def test_response_started_then_limit_crossed_disconnects() -> None:
    results: list[object] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
        results.append((await receive())["type"])
        await send({"type": "http.response.body", "body": b"partial"})

    sent, types = anyio.run(call_asgi, RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"def", b"g"])
    assert types == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert results == ["http.disconnect"]


def test_response_started_in_starlette_streaming() -> None:
    async def endpoint(request: Request) -> Response:
        async def gen():  # type: ignore[no-untyped-def]
            yield b"a"
            async for _ in request.stream():
                pass
            yield b"b"

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5)
    sent, types = anyio.run(call_asgi, app, [b"abc", b"def"])
    assert types.count("http.response.start") == 1
    assert status_and_body(sent)[0] in (200, 413)


def test_non_limit_errors_still_propagate() -> None:
    async def boom(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        anyio.run(call_asgi, RequestBodyLimitMiddleware(boom, max_body_size=5), [b"a"])


# --- Multipart -------------------------------------------------------------------------


async def upload(request: Request) -> Response:
    async with request.form() as form:
        return PlainTextResponse(str(len(form)))


def test_multipart_counted_in_full(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", upload, methods=["POST"])])
    content = b"x" * 100
    client = TestClient(app)
    request = client.build_request("POST", "/", files={"f": ("f.txt", content)})
    request.read()
    body_len = len(request.content)
    assert content and body_len > 100  # boundaries and headers are overhead

    for limit, expected in ((body_len, 200), (body_len - 1, 413)):
        app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=limit)
        response = TestClient(app).post("/", files={"f": ("f.txt", content)})
        assert response.status_code == expected


def test_multipart_files_closed_on_body_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[UploadFile] = []
    original = UploadFile.__init__

    def tracking_init(self: UploadFile, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        created.append(self)

    monkeypatch.setattr(UploadFile, "__init__", tracking_init)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=200)
    head = b'--b\r\nContent-Disposition: form-data; name="f"; filename="f.txt"\r\n\r\nabc'
    headers = [(b"content-type", b"multipart/form-data; boundary=b")]
    sent, _ = anyio.run(call_asgi, app, [head, b"x" * 200], headers)
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert len(created) == 1
    assert created[0].file.closed


def test_multipart_parser_closes_files_on_any_exception() -> None:
    boundary = b"b"
    body = b'--b\r\nContent-Disposition: form-data; name="f"; filename="f.txt"\r\n\r\nabc'

    async def stream():  # type: ignore[no-untyped-def]
        yield body
        raise RuntimeError("interrupted")

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RuntimeError):
        anyio.run(parser.parse)
    assert boundary
    assert len(parser._files_to_close_on_error) == 1
    assert parser._files_to_close_on_error[0].closed
