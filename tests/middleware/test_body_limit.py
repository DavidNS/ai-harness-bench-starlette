from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

ran: list[str] = []


async def read_body(request: Request) -> PlainTextResponse:
    ran.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


@pytest.fixture(autouse=True)
def clear_ran() -> None:
    ran.clear()


def chunks(*sizes: int) -> Iterator[bytes]:
    def gen() -> Iterator[bytes]:
        for size in sizes:
            yield b"x" * size

    return gen()


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 10_000).text == "10000"


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert ran == ["/"]


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(5, 5)).status_code == 200
    assert client.post("/", content=chunks(5, 5, 1)).status_code == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert response.status_code == 413


def test_no_body_and_non_http_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").text == "0"


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert client.post("/", content=chunks(3, 3)).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert client.post("/free", content=b"x" * 600).status_code == 200


def test_mount_limit_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Mount("/m", asgi, max_body_size=5)]))
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert ran == ["asgi"]


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=5)])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert client.post("/", content=chunks(3, 3)).status_code == 413


def test_inner_limit_raises_and_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=3),
            Route("/inherited", read_body, methods=["POST"]),
            Mount(
                "/m",
                routes=[
                    Route("/raised", read_body, methods=["POST"], max_body_size=100),
                    Route("/inherited", read_body, methods=["POST"]),
                ],
                max_body_size=20,
            ),
            Mount("/r", Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for body in (b"x" * 50, chunks(25, 25)):
        assert client.post("/raised", content=body).status_code == 200
    assert client.post("/lowered", content=b"xxxx").status_code == 413
    assert client.post("/lowered", content=b"xxx").status_code == 200
    assert client.post("/inherited", content=b"x" * 11).status_code == 413
    assert client.post("/m/inherited", content=b"x" * 20).status_code == 200
    assert client.post("/m/inherited", content=b"x" * 21).status_code == 413
    assert client.post("/m/raised", content=b"x" * 100).status_code == 200
    assert client.post("/r/", content=b"x" * 50).status_code == 200
    assert client.post("/r/", content=b"x" * 51).status_code == 413


def test_content_length_fail_fast_in_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/inherited", read_body, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/inherited", content=b"x" * 11).status_code == 413
    # Raised by the matched route: not rejected at the application's limit.
    assert client.post("/raised", content=b"x" * 11).status_code == 200
    assert ran == ["/raised"]
    # Unmatched route and method are judged by the application's limit.
    assert client.post("/missing", content=b"x" * 11).status_code == 413
    assert client.post("/missing", content=b"x").status_code == 404
    assert client.put("/inherited", content=b"x" * 11).status_code == 413
    assert client.put("/inherited", content=b"x").status_code == 405
    assert client.put("/raised", content=b"x" * 11).status_code == 413
    assert client.post("/inherited/", content=b"x" * 11).status_code == 413


def test_fail_fast_does_not_read_body() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        raise AssertionError("body must not be read")  # pragma: no cover

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    import anyio

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"content-length", b"11")],
        "http_version": "1.1",
    }
    anyio.run(app, scope, receive, send)  # type: ignore[arg-type]
    assert messages[0]["status"] == 413
    assert ran == []


def test_middleware_standalone(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append("asgi")
        request = Request(scope, receive)
        await PlainTextResponse(str(len(await request.body())))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert ran == ["asgi"]  # Content-Length rejected on entry.
    assert client.post("/", content=chunks(6, 6)).status_code == 413
    assert client.post("/", content=b"x" * 50, headers={"content-length": "1"}).status_code == 413


def test_middleware_in_starlette_app(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=chunks(6, 6)).status_code == 413


def test_response_already_started() -> None:
    sent: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
        await send({"type": "http.response.body", "body": b"partial"})

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"xxxxxx", "more_body": True}

    async def send(message: dict[str, object]) -> None:
        sent.append(str(message["type"]) + str(message.get("status", "")))

    import anyio

    anyio.run(
        RequestBodyLimitMiddleware(asgi, max_body_size=10),  # type: ignore[arg-type]
        {
            "type": "http",
            "headers": [],
        },
        receive,
        send,
    )
    assert sent == ["http.response.start200", "http.response.body"]


def test_app_error_after_exceeding_is_a_413(test_client_factory: TestClientFactory) -> None:
    async def swallow(request: Request) -> PlainTextResponse:
        try:
            await request.body()
        except Exception:
            return PlainTextResponse("handled", status_code=400)
        return PlainTextResponse("no")  # pragma: no cover

    app = Starlette(routes=[Route("/", swallow, methods=["POST"])], max_body_size=3)
    response = test_client_factory(app).post("/", content=chunks(2, 2))
    assert (response.status_code, response.text) == (413, "Content Too Large")


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
            session.send_text("hello world")
            assert session.receive_text() == "hello world"


def multipart_app(max_body_size: int | None) -> Starlette:
    files: list[object] = []

    async def upload(request: Request) -> PlainTextResponse:
        async with request.form() as form:
            files.extend(form.values())
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=max_body_size)
    app.state.files = files
    return app


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(multipart_app(400))
    small = {"file": ("a.txt", b"a" * 100)}
    assert client.post("/", files=small).status_code == 200
    # The file content alone fits, the multipart framing does not.
    boundary_overhead = {"file": ("a.txt", b"a" * 390)}
    assert client.post("/", files=boundary_overhead).status_code == 413


def test_multipart_tempfiles_closed_when_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    import anyio

    created: list[Any] = []

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = tempfile.SpooledTemporaryFile(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)
    app = multipart_app(300)
    head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
    pieces = [head, b"a" * 100, b"b" * 100, b"c" * 100, b"\r\n--b--\r\n"]
    sent: list[Any] = []

    async def receive() -> Any:
        piece = pieces.pop(0)
        return {"type": "http.request", "body": piece, "more_body": bool(pieces)}

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
    }
    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413
    assert created and all(file.closed for file in created)


def test_multipart_tempfiles_closed_on_cancellation_like_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    import anyio

    from starlette.formparsers import MultiPartParser

    created: list[Any] = []

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = tempfile.SpooledTemporaryFile(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)

    async def stream() -> Any:
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\ndata'
        raise RuntimeError("interrupted")

    from starlette.datastructures import Headers

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RuntimeError):
        anyio.run(parser.parse)
    assert created and all(file.closed for file in created)


def test_invalid_content_length_is_ignored(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 5, headers={"content-length": "abc"})
    assert response.status_code in (200, 400)  # The test client may sanitise it; never a crash.


def test_lowered_limit_after_body_was_read() -> None:
    import anyio

    sent: list[Any] = []
    pieces = [b"x" * 12, b"x" * 12]

    async def receive() -> Any:
        return {"type": "http.request", "body": pieces.pop(0), "more_body": bool(pieces)}

    async def send(message: Any) -> None:
        sent.append(message)

    async def reads_first(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await inner(scope, receive, send)

    inner = Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=10)])
    app = Starlette(routes=[Mount("/", reads_first)], max_body_size=100)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }
    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413
    assert ran == []


def test_unrelated_errors_and_non_http_pass_through() -> None:
    import anyio

    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    async def receive() -> Any:  # pragma: no cover
        raise AssertionError

    async def send(message: Any) -> None:  # pragma: no cover
        raise AssertionError

    middleware = RequestBodyLimitMiddleware(broken, max_body_size=1)
    with pytest.raises(RuntimeError, match="boom"):
        anyio.run(middleware, {"type": "http", "headers": []}, receive, send)
    with pytest.raises(RuntimeError, match="boom"):
        anyio.run(middleware, {"type": "websocket", "headers": []}, receive, send)


def test_receive_after_exceeding_is_disconnect() -> None:
    import anyio

    seen: list[Any] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((await receive())["type"])
        seen.append((await receive())["type"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Any:
        return {"type": "http.request", "body": b"xx", "more_body": True}

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    anyio.run(RequestBodyLimitMiddleware(asgi, max_body_size=1), {"type": "http", "headers": []}, receive, send)
    assert seen == ["http.disconnect", "http.disconnect"]
    assert sent[0]["status"] == 413


def test_client_disconnect_passes_through() -> None:
    import anyio

    seen: list[Any] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((await receive())["type"])

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:  # pragma: no cover
        raise AssertionError

    anyio.run(RequestBodyLimitMiddleware(asgi, max_body_size=1), {"type": "http", "headers": []}, receive, send)
    assert seen == ["http.disconnect"]


def test_lifespan_scope_passes_through() -> None:
    import anyio

    seen: list[Any] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Any:  # pragma: no cover
        raise AssertionError

    async def send(message: Any) -> None:  # pragma: no cover
        raise AssertionError

    anyio.run(RequestBodyLimitMiddleware(asgi, max_body_size=1), {"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


def test_multipart_tempfile_closed_when_body_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    import anyio

    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    created: list[Any] = []

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = tempfile.SpooledTemporaryFile(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)

    async def stream() -> Any:
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\ndata'

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    anyio.run(parser.parse)
    assert created and all(file.closed for file in created)


def test_mount_wrapped_router_inner_route_raises_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = GZipMiddleware(Router(routes=[Route("/up", read_body, methods=["POST"], max_body_size=100)]))
    app = Starlette(routes=[Mount("/api", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/api/up", content=b"x" * 50).text == "50"
    assert client.post("/api/up", content=b"x" * 101).status_code == 413


def test_mount_wrapped_starlette_inner_raises_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.cors import CORSMiddleware

    sub = Starlette(routes=[Route("/up", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/api", app=CORSMiddleware(sub, allow_origins=["*"]))], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/api/up", content=b"x" * 50).text == "50"


def test_mount_without_matching_route_uses_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/x", read_body, methods=["POST"])], max_body_size=100)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/m/missing", content=b"x" * 50).status_code == 413
    assert client.post("/m/x", content=b"x" * 50).status_code == 200


def test_host_fail_fast(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    async def no_read(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append("host")
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Host("testserver", app=no_read)], max_body_size=10))
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert ran == []
    assert client.post("/", content=b"x").text == "ok"


def test_invalid_max_body_size() -> None:
    with pytest.raises(ValueError):
        Starlette(max_body_size=-1)
    with pytest.raises(TypeError):
        Route("/", read_body, max_body_size="10")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RequestBodyLimitMiddleware(Router(), max_body_size=True)


def test_started_response_exceeded_no_server_error(test_client_factory: TestClientFactory) -> None:
    class StartThenRead(PlainTextResponse):
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"start", "more_body": True})
            async for _ in Request(scope, receive).stream():
                pass  # pragma: no cover
            await send({"type": "http.response.body", "body": b""})  # pragma: no cover

    async def echo(request: Request) -> PlainTextResponse:
        return StartThenRead("")

    for app in (
        Starlette(routes=[Route("/", echo, methods=["POST"])], max_body_size=10),
        Starlette(routes=[Route("/", echo, methods=["POST"], max_body_size=10)]),
        Starlette(routes=[Mount("/", routes=[Route("/", echo, methods=["POST"])], max_body_size=10)]),
        RequestBodyLimitMiddleware(Starlette(routes=[Route("/", echo, methods=["POST"])]), max_body_size=10),
    ):
        response = test_client_factory(app).post("/", content=chunks(6, 6, 6))
        assert response.status_code == 200


def test_send_after_rejection_raises() -> None:
    import anyio

    outcome: list[Any] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await send({"type": "http.response.body", "body": b"x", "more_body": True})
        except OSError:
            outcome.append("closed")
            raise

    async def receive() -> Any:
        return {"type": "http.request", "body": b"xx", "more_body": True}

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    anyio.run(RequestBodyLimitMiddleware(asgi, max_body_size=1), {"type": "http", "headers": []}, receive, send)
    assert outcome == ["closed"]
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]


def test_unrelated_error_after_started_and_exceeded_propagates() -> None:
    import anyio

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        raise RuntimeError("bug")

    async def receive() -> Any:
        return {"type": "http.request", "body": b"xx", "more_body": True}

    async def send(message: Any) -> None:
        pass

    with pytest.raises(RuntimeError, match="bug"):
        anyio.run(RequestBodyLimitMiddleware(asgi, max_body_size=1), {"type": "http", "headers": []}, receive, send)


def test_mounted_subapp_limit_overrides_mount_limit(test_client_factory: TestClientFactory) -> None:
    raised = Starlette(routes=[Route("/up", read_body, methods=["POST"])], max_body_size=100)
    lowered = Starlette(routes=[Route("/up", read_body, methods=["POST"])], max_body_size=5)
    app = Starlette(
        routes=[Mount("/raise", app=raised, max_body_size=50), Mount("/lower", app=lowered, max_body_size=50)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raise/up", content=b"x" * 80).text == "80"
    assert client.post("/raise/up", content=chunks(40, 40)).text == "80"
    assert client.post("/lower/up", content=b"x" * 20).status_code == 413


def test_unmatched_uses_application_limit_only(test_client_factory: TestClientFactory) -> None:
    async def reader(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse(str(len(await Request(scope, receive).body())))(scope, receive, send)

    unlimited = Starlette(routes=[Mount("/m", routes=[Route("/x", read_body, methods=["POST"])], max_body_size=5)])
    client = test_client_factory(unlimited)
    assert client.post("/m/missing", content=b"x" * 50).status_code == 404
    inner = Router(routes=[Route("/x", read_body, methods=["POST"])], max_body_size=5)
    assert (
        test_client_factory(Starlette(routes=[Mount("/m", app=inner)])).put("/m/x", content=b"x" * 50).status_code
        == 405
    )

    limited = Starlette(
        routes=[Mount("/m", app=Router(routes=[], default=reader), max_body_size=1000)], max_body_size=10
    )
    client = test_client_factory(limited)
    assert client.post("/m/zzz", content=b"x" * 500).status_code == 413
    assert client.post("/m/zzz", content=chunks(*[10] * 50)).status_code == 413
    assert client.post("/m/zzz", content=b"x" * 10).text == "10"


@pytest.mark.parametrize("where", ["app", "route", "middleware"])
def test_rejection_written_once_with_slow_send_and_disconnect_listener(where: str) -> None:
    import anyio

    from starlette.middleware import Middleware
    from starlette.responses import StreamingResponse

    async def endpoint(request: Request) -> StreamingResponse:
        async def gen() -> Any:
            yield b"x"  # pragma: no cover

        return StreamingResponse(gen())

    route = Route("/", endpoint, methods=["POST"], max_body_size=10 if where == "route" else None)
    app = Starlette(
        routes=[route],
        max_body_size=10 if where == "app" else None,
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)] if where == "middleware" else [],
    )
    pieces = [b"x" * 6, b"x" * 6]
    handed: list[Any] = []
    completed: list[Any] = []

    async def receive() -> Any:
        if pieces:
            return {"type": "http.request", "body": pieces.pop(0), "more_body": bool(pieces)}
        await anyio.sleep_forever()  # pragma: no cover

    async def send(message: Any) -> None:
        handed.append(message)
        await anyio.sleep(0.01)
        completed.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "asgi": {"spec_version": "2.3"},
    }

    async def main() -> None:
        with anyio.fail_after(3):
            await app(scope, receive, send)

    anyio.run(main)
    assert [m["status"] for m in handed if m["type"] == "http.response.start"] == [413]
    assert [m["body"] for m in handed if m["type"] == "http.response.body"] == [b"Content Too Large"]


def _drive(app: Any, pieces: list[bytes], path: str, method: str = "POST", delay: float = 0) -> list[Any]:
    import anyio

    pieces = list(pieces)
    sent: list[Any] = []

    async def receive() -> Any:
        if pieces:
            return {"type": "http.request", "body": pieces.pop(0), "more_body": bool(pieces)}
        await anyio.sleep_forever()  # pragma: no cover

    async def send(message: Any) -> None:
        await anyio.sleep(delay)
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }

    async def main() -> None:
        with anyio.fail_after(3):
            await app(scope, receive, send)

    anyio.run(main)
    return sent


def test_unmatched_after_body_read_by_mounted_app_gets_413() -> None:
    inner = Router(routes=[Route("/x", read_body, methods=["PUT"])])

    async def reads_first(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await inner(scope, receive, send)

    app = Starlette(routes=[Mount("/m", reads_first, max_body_size=100)], max_body_size=10)
    for path in ("/m/missing", "/m/x"):
        sent = _drive(app, [b"x" * 12, b"x" * 12], path)
        assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
        assert [m["body"] for m in sent if m["type"] == "http.response.body"] == [b"Content Too Large"]


def test_standalone_router_limit_applies_to_unmatched() -> None:
    async def reader(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse(str(len(await Request(scope, receive).body())))(scope, receive, send)

    router = Router(routes=[Route("/x", read_body, methods=["POST"])], default=reader, max_body_size=5)
    sent = _drive(router, [b"x" * 50], "/other")
    assert sent[0]["status"] == 413


def test_rejection_can_be_cancelled_by_the_server() -> None:
    import time

    import anyio

    pieces = [b"x" * 6]

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})  # pragma: no cover

    async def receive() -> Any:
        return {"type": "http.request", "body": pieces[0], "more_body": True}

    async def send(message: Any) -> None:
        await anyio.sleep(5)

    async def main() -> None:
        with anyio.move_on_after(0.2):
            await RequestBodyLimitMiddleware(asgi, max_body_size=5)({"type": "http", "headers": []}, receive, send)

    began = time.monotonic()
    anyio.run(main)
    assert time.monotonic() - began < 2


def test_single_413_when_body_exceeded_before_unmatched_routing() -> None:
    inner = Router(routes=[Route("/x", read_body, methods=["POST"])])

    async def reads_first(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive())["type"] == "http.request":
            pass
        await inner(scope, receive, send)

    app = Starlette(routes=[Mount("/m", reads_first, max_body_size=100)], max_body_size=10)
    sent = _drive(app, [b"x" * 60, b"x" * 60, b"x" * 60], "/m/missing")
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413


@pytest.mark.parametrize("record_first", [True, False])
@pytest.mark.parametrize("gzip", [True, False])
@pytest.mark.parametrize("where", ["app", "route", "middleware"])
def test_listener_reads_body_while_response_pending(where: str, gzip: bool, record_first: bool) -> None:
    import anyio

    from starlette.middleware import Middleware
    from starlette.middleware.gzip import GZipMiddleware
    from starlette.responses import StreamingResponse

    async def endpoint(request: Request) -> StreamingResponse:
        async def gen() -> Any:
            yield b"x"  # pragma: no cover

        return StreamingResponse(gen())

    middleware = [Middleware(GZipMiddleware, minimum_size=1)] if gzip else []
    if where == "middleware":
        middleware.append(Middleware(RequestBodyLimitMiddleware, max_body_size=10))
    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"], max_body_size=10 if where == "route" else None)],
        max_body_size=10 if where == "app" else None,
        middleware=middleware,
    )
    pieces = [b"x" * 6, b"x" * 6]
    taken: list[str] = []

    async def receive() -> Any:
        if pieces:
            return {"type": "http.request", "body": pieces.pop(0), "more_body": bool(pieces)}
        await anyio.sleep_forever()  # pragma: no cover

    async def send(message: Any) -> None:
        if record_first:
            taken.append(message["type"])
        await anyio.sleep(0.01)
        if not record_first:
            taken.append(message["type"])

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"accept-encoding", b"gzip")],
        "asgi": {"spec_version": "2.3"},
    }

    async def main() -> None:
        with anyio.fail_after(3):
            await app(scope, receive, send)

    anyio.run(main)
    assert taken == ["http.response.start", "http.response.body"]


@pytest.mark.parametrize("where", ["app", "route", "middleware"])
def test_client_gone_during_413_raises_plain_oserror(where: str) -> None:
    import anyio

    app: Any = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=10 if where == "route" else None)],
        max_body_size=10 if where == "app" else None,
    )
    if where == "middleware":
        app = RequestBodyLimitMiddleware(app, max_body_size=10)
    pieces = [b"x" * 6] * 3

    async def receive() -> Any:
        return {"type": "http.request", "body": pieces.pop(0), "more_body": bool(pieces)}

    async def send(message: Any) -> None:
        raise OSError("client gone")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }
    with pytest.raises(OSError, match="client gone"):
        anyio.run(app, scope, receive, send)
