from __future__ import annotations

from collections.abc import Callable
from tempfile import SpooledTemporaryFile
from typing import Any, cast

import pytest

from starlette.applications import Starlette
from starlette.middleware.body_limit import RequestBodyLimitMiddleware, RequestBodyTooLarge
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

TOO_LARGE = "Content Too Large"


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


def make_scope(path: str = "/", content_length: int | None = None, method: str = "POST") -> Scope:
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }


async def drive(app: Any, chunks: list[bytes], content_length: int | None = None, path: str = "/") -> list[Message]:
    """Run `app` with a body sent in `chunks`, returning the messages it sent."""
    scope = make_scope(path, content_length)
    pending = list(chunks)
    received = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal received
        if pending:
            received += 1
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    drive.received = received  # type: ignore[attr-defined]
    return sent


def status_and_body(sent: list[Message]) -> tuple[int, bytes]:
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    return sent[0]["status"], sent[1]["body"]


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == TOO_LARGE
    assert response.headers["content-type"].startswith("text/plain")


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_no_body_and_other_methods_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["GET", "POST"])], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").text == "read 0"
    assert client.post("/", content=b"x").status_code == 413


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app) as client:  # runs lifespan
        with client.websocket_connect("/ws") as session:
            session.send_text("a long message")
            assert session.receive_text() == "a long message"


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).text == "read 5"
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/open", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/open", content=b"x" * 50).text == "read 50"
    assert client.post("/m/", content=b"x" * 5).text == "read 5"
    response = client.post("/m/", content=b"x" * 6)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 6).status_code == 413
    assert client.post("/free", content=b"x" * 600).text == "read 600"


def test_standalone_route(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Route("/", read_body, methods=["POST"], max_body_size=3))
    assert client.post("/", content=b"abc").text == "read 3"
    assert client.post("/", content=b"abcd").status_code == 413


def test_inner_limits_raise_and_lower_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/default", read_body, methods=["POST"]),
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=2),
            Mount(
                "/mount",
                routes=[
                    Route("/default", read_body, methods=["POST"]),
                    Route("/raised", read_body, methods=["POST"], max_body_size=100),
                ],
                max_body_size=20,
            ),
            Mount(
                "/router",
                app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=40),
                max_body_size=20,
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)

    def post(path: str, size: int) -> int:
        return client.post(path, content=b"x" * size).status_code

    assert (post("/default", 10), post("/default", 11)) == (200, 413)
    assert (post("/raised", 100), post("/raised", 101)) == (200, 413)
    assert (post("/lowered", 2), post("/lowered", 3)) == (200, 413)
    assert (post("/mount/default", 20), post("/mount/default", 21)) == (200, 413)
    assert (post("/mount/raised", 100), post("/mount/raised", 101)) == (200, 413)
    assert (post("/router/", 40), post("/router/", 41)) == (200, 413)


@pytest.mark.anyio
async def test_content_length_fail_fast_is_before_application_code() -> None:
    calls: list[str] = []

    async def endpoint(request: Request) -> Response:
        calls.append("endpoint")  # pragma: no cover
        return Response()  # pragma: no cover

    async def reading(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("app")  # pragma: no cover

    # Standalone middleware: rejected on entry, the receive callable is never touched.
    sent = await drive(RequestBodyLimitMiddleware(reading, max_body_size=10), [], content_length=11)
    assert status_and_body(sent) == (413, TOO_LARGE.encode())
    assert calls == []

    # Application: rejected once routing decided.
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    sent = await drive(app, [], content_length=11)
    assert status_and_body(sent)[0] == 413
    assert calls == []

    # Understated or absent header does not let a body through.
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent = await drive(app, [b"x" * 6, b"x" * 6], content_length=3)
    assert status_and_body(sent)[0] == 413
    sent = await drive(app, [b"x" * 6, b"x" * 6])
    assert status_and_body(sent)[0] == 413

    # Garbage header is ignored.
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent = await drive(app, [b"abc"], content_length=None)
    assert status_and_body(sent)[0] == 200


@pytest.mark.anyio
async def test_content_length_follows_the_limit_that_applies() -> None:
    seen: list[str] = []

    async def endpoint(request: Request) -> Response:
        seen.append(request.url.path)
        return Response()

    app = Starlette(
        routes=[
            Route("/raised", endpoint, methods=["POST"], max_body_size=100),
            Route("/plain", endpoint, methods=["POST"]),
            Route("/get-only", endpoint, methods=["GET"], max_body_size=1000),
            Mount("/m", routes=[Route("/x", endpoint, methods=["POST"])], max_body_size=50),
        ],
        max_body_size=10,
    )

    async def status(path: str, length: int) -> int:
        sent = await drive(app, [b""], content_length=length, path=path)
        return int(sent[0]["status"])

    # An inner route may raise the limit, so the app's limit does not reject first.
    assert await status("/raised", 50) == 200
    assert await status("/raised", 101) == 413
    assert await status("/plain", 11) == 413
    assert await status("/m/x", 40) == 200
    assert await status("/m/x", 51) == 413
    # No route / no method: the application's limit, not the unmatched route's.
    assert await status("/nothing", 11) == 413
    assert await status("/nothing", 10) == 404
    assert await status("/get-only", 11) == 413
    assert await status("/get-only", 10) == 405
    assert seen == ["/raised", "/m/x"]


@pytest.mark.anyio
async def test_content_length_for_mounted_asgi_app_and_router_default() -> None:
    called = False

    async def plain(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True  # pragma: no cover

    app = Starlette(routes=[Mount("/plain", app=plain, max_body_size=5)])
    sent = await drive(app, [], content_length=6, path="/plain/x")
    assert status_and_body(sent)[0] == 413
    assert not called

    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    sent = await drive(router, [], content_length=6, path="/missing")
    assert status_and_body(sent)[0] == 413


def test_standalone_middleware(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await PlainTextResponse(f"read {len(body)}")(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=4))
    assert client.post("/", content=b"abcd").text == "read 4"
    response = client.post("/", content=b"abcde")
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert client.get("/").text == "read 0"


def test_middleware_on_starlette_via_middleware_argument(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=3)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"abc").status_code == 200
    assert client.post("/", content=b"abcd").status_code == 413


def test_streaming_upload_counts_chunks(test_client_factory: TestClientFactory) -> None:
    def chunks() -> Any:
        for _ in range(5):
            yield b"x" * 4

    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=19))
    assert client.post("/", content=chunks()).status_code == 413
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=20))
    assert client.post("/", content=chunks()).text == "read 20"


@pytest.mark.anyio
async def test_swallowed_error_still_gets_413_and_stream_stays_stopped() -> None:
    reads = 0

    async def endpoint(request: Request) -> Response:
        nonlocal reads
        try:
            await request.body()
        except RequestBodyTooLarge:
            pass
        reads += 1
        return PlainTextResponse("handled anyway")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5)
    sent = await drive(app, [b"x" * 6])
    assert status_and_body(sent) == (413, TOO_LARGE.encode())


@pytest.mark.anyio
async def test_response_already_started_gets_no_second_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            await receive()

    sent = await drive(RequestBodyLimitMiddleware(app, max_body_size=5), [b"xxx", b"xxx"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_body_stream_is_stopped() -> None:
    errors = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal errors
        with pytest.raises(RequestBodyTooLarge):
            await receive()
        for _ in range(2):
            with pytest.raises(RequestBodyTooLarge):
                await receive()
            errors += 1
        raise RequestBodyTooLarge()

    sent = await drive(RequestBodyLimitMiddleware(app, max_body_size=1), [b"xx", b"yy", b"zz"])
    assert status_and_body(sent)[0] == 413
    assert errors == 2
    assert drive.received == 1  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_unrelated_exception_after_exceeding_still_reports_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except RequestBodyTooLarge:
            raise ValueError("wrapped")

    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"xx", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    with pytest.raises(ValueError, match="wrapped"):
        await RequestBodyLimitMiddleware(app, max_body_size=1)(make_scope(), receive, send)
    assert messages[0]["status"] == 413


def test_streaming_response_unaffected_when_body_within_limit(test_client_factory: TestClientFactory) -> None:
    async def stream(request: Request) -> Response:
        async def gen() -> Any:
            yield b"a"
            yield b"b"

        return StreamingResponse(gen())

    client = test_client_factory(Starlette(routes=[Route("/", stream, methods=["POST"])], max_body_size=3))
    assert client.post("/", content=b"abc").text == "ab"


# Multipart


def multipart_files(count: int, size: int) -> dict[str, tuple[str, bytes]]:
    return {f"f{i}": (f"f{i}.txt", b"x" * size) for i in range(count)}


async def multipart_endpoint(request: Request) -> Response:
    async with request.form() as form:
        return PlainTextResponse(str(sorted(form.keys())))


def test_multipart_limit_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", multipart_endpoint, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    assert client.post("/", files=multipart_files(1, 100)).status_code == 200
    # The file data alone is under the limit, the multipart framing pushes it over.
    response = client.post("/", files=multipart_files(1, 990))
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert client.post("/", files=multipart_files(5, 400)).status_code == 413


def test_multipart_limit_exact_boundary(test_client_factory: TestClientFactory) -> None:
    probe = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    request = probe.build_request("POST", "/", files=multipart_files(1, 300))
    total = len(request.read())

    app = Starlette(routes=[Route("/", multipart_endpoint, methods=["POST"])], max_body_size=total)
    client = test_client_factory(app)
    assert client.send(client.build_request("POST", "/", files=multipart_files(1, 300))).status_code == 200
    app = Starlette(routes=[Route("/", multipart_endpoint, methods=["POST"])], max_body_size=total - 1)
    client = test_client_factory(app)
    assert client.send(client.build_request("POST", "/", files=multipart_files(1, 300))).status_code == 413


@pytest.fixture
def spooled_files(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    created: list[Any] = []

    class RecordingFile(SpooledTemporaryFile):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", RecordingFile)
    return created


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_limit_crossed(spooled_files: list[Any]) -> None:
    parts = b"".join(
        b"--b\r\n"
        + f'Content-Disposition: form-data; name="f{i}"; filename="f{i}.txt"\r\n\r\n'.encode()
        + b"x" * 500
        + b"\r\n"
        for i in range(4)
    )
    body = parts + b"--b--\r\n"
    chunks = [body[i : i + 400] for i in range(0, len(body), 400)]

    async def endpoint(request: Request) -> Response:
        async with request.form() as form:  # pragma: no cover
            return PlainTextResponse(str(form))

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=1500)

    scope_headers = [(b"content-type", b"multipart/form-data; boundary=b")]
    original = make_scope

    sent: list[Message] = []
    pending = list(chunks)

    async def receive() -> Message:
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = original()
    scope["headers"] = scope_headers
    await app(scope, receive, send)
    assert status_and_body(sent)[0] == 413
    assert len(spooled_files) >= 2
    assert all(file.closed for file in spooled_files)


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_any_interruption(spooled_files: list[Any]) -> None:
    from starlette.formparsers import MultiPartParser

    body = b'--b\r\nContent-Disposition: form-data; name="f"; filename="f.txt"\r\n\r\ndata\r\n--b--\r\n'

    async def stream() -> Any:
        yield body[:-10]
        raise RuntimeError("interrupted")

    from starlette.datastructures import Headers

    parser = MultiPartParser(Headers({"Content-Type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RuntimeError):
        await parser.parse()
    assert len(spooled_files) == 1
    assert spooled_files[0].closed


def test_urlencoded_form_limit(test_client_factory: TestClientFactory) -> None:
    async def form_endpoint(request: Request) -> Response:
        return PlainTextResponse(str(dict(await request.form())))

    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", data={"a": "b"}).status_code == 200
    assert client.post("/", data={"a": "b" * 20}).status_code == 413


def test_json_limit(test_client_factory: TestClientFactory) -> None:
    async def json_endpoint(request: Request) -> Response:
        return PlainTextResponse(str(await request.json()))

    app = Starlette(routes=[Route("/", json_endpoint, methods=["POST"])], max_body_size=12)
    client: Callable[..., Any] = test_client_factory(app).post
    assert client("/", json={"a": 1}).status_code == 200
    assert client("/", json={"a": "x" * 50}).status_code == 413


# Layering: the standalone limit is a hard cap, routed limits commit when routing decides.


def chunked(size: int, step: int = 10) -> Any:
    for _ in range(0, size, step):
        yield b"x" * step


class Passthrough:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def test_standalone_limit_is_a_hard_cap(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=100)
    apps: list[Any] = [
        RequestBodyLimitMiddleware(inner, max_body_size=10),
        RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(inner, max_body_size=100), max_body_size=10),
    ]
    for app in apps:
        client = test_client_factory(app)
        assert client.post("/", content=b"x" * 50).status_code == 413
        assert client.post("/", content=chunked(50)).status_code == 413
        assert client.post("/", content=b"x" * 5).status_code == 200

    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=chunked(50)).status_code == 413
    assert client.post("/", content=b"x" * 50).status_code == 413


def test_lowered_limit_counts_bytes_received_before_routing(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class Reading(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            return cast(Response, await call_next(request))

    app = Starlette(
        routes=[
            Route("/route", read_body, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ],
        middleware=[Middleware(Reading)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/route", content=chunked(5, 5)).text == "read 5"
    assert client.post("/route", content=chunked(50)).status_code == 413
    assert client.post("/m/", content=chunked(50)).status_code == 413


def test_no_route_or_method_uses_application_limit_inside_limited_mount(
    test_client_factory: TestClientFactory,
) -> None:
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/x", read_body, methods=["GET"])], max_body_size=50)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/m/nothing", content=b"x" * 30).status_code == 413
    assert client.put("/m/x", content=b"x" * 30).status_code == 413
    assert client.put("/m/x", content=b"x" * 5).status_code == 405


def test_declared_length_waits_for_routing_behind_wrappers(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Host

    sub = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    inner = Passthrough(Router(routes=[Route("/up", read_body, methods=["POST"], max_body_size=100)]))
    app = Starlette(
        routes=[
            Mount("/sub", app=CORSMiddleware(sub, allow_origins=["*"])),
            Mount("/api", app=inner),
            Host("testserver", app=inner),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/sub/", content=b"x" * 50).text == "read 50"
    assert client.post("/api/up", content=b"x" * 50).text == "read 50"
    assert client.post("/up", content=b"x" * 50).text == "read 50"
    assert client.post("/up", content=b"x" * 101).status_code == 413


def test_rejection_goes_through_the_middleware_between_the_layers(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class Base(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return cast(Response, await call_next(request))

    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"], max_body_size=5),
            Route("/get", read_body, methods=["GET"]),
            Mount("/plain", app=Passthrough(PlainTextResponse("ok"))),
        ],
        middleware=[Middleware(Base)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path, method in (("/", "POST"), ("/get", "PUT"), ("/none", "POST"), ("/plain/x", "POST")):
        response = client.request(method, path, content=b"x" * 11)
        assert (response.status_code, response.text) == (413, TOO_LARGE)
    # Streamed, through the task group of BaseHTTPMiddleware.
    response = client.post("/", content=chunked(50))
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_body_read_in_a_task_group(test_client_factory: TestClientFactory) -> None:
    import anyio

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        async def reader() -> None:
            more = True
            while more:
                message = await receive()
                more = message.get("more_body", False)

        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
        await PlainTextResponse("done")(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=5))
    assert client.post("/", content=b"abc").text == "done"
    response = client.post("/", content=chunked(50))
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_mount_and_router_limits_cover_their_own_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class Reading(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            return cast(Response, await call_next(request))

    inner = [Route("/", read_body, methods=["POST"])]
    app = Starlette(
        routes=[
            Mount("/raised", routes=inner, middleware=[Middleware(Reading)], max_body_size=50),
            Mount("/router", app=Router(inner, middleware=[Middleware(Reading)], max_body_size=50)),
            Mount("/lowered", routes=inner, middleware=[Middleware(Reading)], max_body_size=5),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for prefix in ("/raised", "/router"):
        assert client.post(f"{prefix}/", content=chunked(30)).text == "read 30"
        assert client.post(f"{prefix}/", content=chunked(60)).status_code == 413
    assert client.post("/lowered/", content=chunked(10, 5)).status_code == 413
    assert client.post("/lowered/nothing", content=b"x" * 30).status_code == 413


@pytest.mark.anyio
async def test_mount_limit_stops_the_stream_in_its_middleware() -> None:
    async def draining(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body"):
            pass  # pragma: no cover

    app = Starlette(
        routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)],
        middleware=[],
    )
    app.router.routes[0].app = Passthrough(draining)  # type: ignore[attr-defined]
    sent = await drive(app, [b"x" * 10] * 1000, path="/m/")
    assert sent[0]["status"] == 413
    assert drive.received <= 2  # type: ignore[attr-defined]


def test_no_route_in_raised_mount_checks_received_bytes(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class Reading(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            return cast(Response, await call_next(request))

    app = Starlette(
        routes=[
            Mount(
                "/m",
                routes=[Route("/x", read_body, methods=["GET", "POST"])],
                middleware=[Middleware(Reading)],
                max_body_size=50,
            )
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/m/nothing", content=chunked(30)).status_code == 413
    assert client.put("/m/x", content=chunked(30)).status_code == 413
    assert client.post("/m/x", content=chunked(30)).text == "read 30"
