from __future__ import annotations

import io
from tempfile import SpooledTemporaryFile
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

TOO_LARGE = "Content Too Large"


def chunks(*parts: bytes) -> Any:
    def gen() -> Any:
        yield from parts

    return gen()


async def call_asgi(
    app: Any,
    parts: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "POST",
    path: str = "/",
) -> tuple[list[Message], int]:
    """Drive `app` with a body delivered in separate ASGI messages. Returns (sent, receive calls)."""
    sent: list[Message] = []
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls <= len(parts):
            return {"type": "http.request", "body": parts[calls - 1], "more_body": calls < len(parts)}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await anyio.sleep(0)  # A real server's send yields.
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers or [],
    }
    await app(scope, receive, send)
    return sent, calls


def assert_sent_413(sent: list[Message]) -> None:
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


async def read_body(request: Request) -> PlainTextResponse:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def assert_413(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == TOO_LARGE
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_413(client.post("/", content=b"x" * 11))


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


@pytest.mark.anyio
async def test_streamed_body_without_content_length() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent, _ = await call_asgi(app, [b"x" * 6, b"x" * 4])
    assert sent[1]["body"] == b"10"
    sent, calls = await call_asgi(app, [b"x" * 6, b"x" * 5, b"x"])
    assert_sent_413(sent)
    assert calls == 2  # The body stream is stopped at the first chunk over the limit.


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert_413(client.post("/", content=b"x" * 50, headers={"content-length": "5"}))


def test_application_limit_catches_exceptions_in_endpoint(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        try:
            await request.body()
        except Exception:
            return PlainTextResponse("handled", status_code=400)
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    assert_413(test_client_factory(app).post("/", content=b"xxxx"))


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=4)
    client = test_client_factory(app)
    assert client.post("/", content=b"xxxx").text == "4"
    assert_413(client.post("/", content=b"xxxxx"))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=4),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"xxxx").text == "4"
    assert_413(client.post("/m/", content=b"xxxxx"))
    assert client.post("/free", content=b"x" * 1000).text == "1000"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=2),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"xx").text == "2"
    assert_413(client.post("/small", content=b"xxx"))
    assert client.post("/other", content=b"x" * 1000).text == "1000"


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=2),
            Route("/inherited", read_body, methods=["POST"]),
            Mount(
                "/m",
                routes=[
                    Route("/raised", read_body, methods=["POST"], max_body_size=50),
                    Route("/inherited", read_body, methods=["POST"]),
                ],
                max_body_size=20,
            ),
            Mount("/r", app=Router([Route("/x", read_body, methods=["POST"])], max_body_size=30)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raised", content=b"x" * 100).text == "100"
    assert_413(client.post("/raised", content=b"x" * 101))
    assert_413(client.post("/lowered", content=b"xxx"))
    assert client.post("/inherited", content=b"x" * 10).text == "10"
    assert_413(client.post("/inherited", content=b"x" * 11))
    assert client.post("/m/inherited", content=b"x" * 20).text == "20"
    assert_413(client.post("/m/inherited", content=b"x" * 21))
    assert client.post("/m/raised", content=b"x" * 50).text == "50"
    assert_413(client.post("/m/raised", content=b"x" * 51))
    assert client.post("/r/x", content=b"x" * 30).text == "30"
    assert_413(client.post("/r/x", content=b"x" * 31))
    # Streamed bodies are counted against the same inner limits.
    assert client.post("/raised", content=chunks(b"x" * 60, b"x" * 40)).text == "100"
    assert_413(client.post("/lowered", content=chunks(b"x", b"xx")))


def test_nested_application_limit(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)
    app = Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/inner/", content=b"x" * 50).text == "50"
    assert_413(client.post("/inner/", content=b"x" * 51))


def test_content_length_fail_fast(test_client_factory: TestClientFactory) -> None:
    calls: list[str] = []

    async def endpoint(request: Request) -> PlainTextResponse:
        calls.append("endpoint")  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            async def spy_receive() -> Message:
                calls.append("receive")  # pragma: no cover
                return await receive()  # pragma: no cover

            await self.app(scope, spy_receive, send)

    app = Starlette(
        routes=[
            Route("/", endpoint, methods=["POST"]),
            Route("/mw", endpoint, methods=["POST"], middleware=[Middleware(Spy)], max_body_size=5),
            Mount("/m", routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5),
            Mount("/raw", app=PlainTextResponse("raw"), max_body_size=5),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path, size in [("/", 11), ("/mw", 6), ("/m/", 6), ("/raw", 6)]:
        response = client.post(path, content=b"x" * size, headers={"content-length": str(size)})
        assert_413(response)
    assert calls == []


def test_content_length_fail_fast_waits_for_routing(test_client_factory: TestClientFactory) -> None:
    # The route raises the application limit, so a declared length over the
    # application limit is not rejected early.
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).text == "50"


def test_content_length_no_match_uses_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["GET"], max_body_size=1000)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # No route: application limit is used, 413 rather than 404.
    assert_413(client.post("/missing", content=b"x" * 11))
    assert client.post("/missing", content=b"x" * 5).status_code == 404
    # Method not allowed: the route's own limit does not apply either.
    assert_413(client.post("/", content=b"x" * 11))
    assert client.post("/", content=b"x" * 5).status_code == 405


def test_invalid_content_length_is_ignored(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"xx", headers={"content-length": "abc"})
    assert response.text == "2"


def test_standalone_middleware(test_client_factory: TestClientFactory) -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])]), max_body_size=8)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 8).text == "8"
    assert_413(client.post("/", content=b"x" * 9))
    assert_413(client.post("/", content=chunks(b"xxxxx", b"xxxx")))
    assert_413(client.post("/", content=b"x" * 20, headers={"content-length": "1"}))


def test_standalone_middleware_rejects_on_entry(test_client_factory: TestClientFactory) -> None:
    called = False

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True  # pragma: no cover

    # Even when an inner route would raise the limit, the standalone middleware rejects first.
    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=5))
    assert_413(client.post("/", content=b"x" * 6))
    assert not called

    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=5))
    assert_413(client.post("/", content=b"x" * 6))


def test_standalone_middleware_via_starlette_middleware(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=3)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"xxx").text == "3"
    assert_413(client.post("/", content=b"xxxx"))


def test_standalone_middleware_without_limit_and_non_http_scopes() -> None:
    seen: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    import anyio

    async def run() -> None:
        app = RequestBodyLimitMiddleware(asgi, max_body_size=1)
        for type_ in ("lifespan", "websocket"):
            await app({"type": type_}, None, None)  # type: ignore[arg-type]
        await RequestBodyLimitMiddleware(asgi)({"type": "http", "headers": []}, None, None)  # type: ignore[arg-type]

    anyio.run(run)
    assert seen == ["lifespan", "websocket", "http"]


def test_requests_without_body_are_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").text == "0"
    assert_413(client.post("/", content=b"x"))


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app) as client:  # runs lifespan
        with client.websocket_connect("/ws") as session:
            session.send_text("a long message")
            assert session.receive_text() == "a long message"


@pytest.mark.anyio
async def test_response_already_started() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await receive()
        except BaseException:
            await send({"type": "http.response.body", "body": b"partial"})

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent, calls = await call_asgi(app, [b"xxx", b"xxx", b"xxx"])
    # No second response is sent, and the app's own response is untouched.
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"partial"
    assert calls == 2


def test_unrelated_error_after_exceeding_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        try:
            await request.body()
        except Exception as exc:
            raise ValueError("boom") from exc
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    client = test_client_factory(app, raise_server_exceptions=False)
    assert_413(client.post("/", content=b"xxxx", headers={"content-length": "1"}))


def test_error_without_exceeding_propagates(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        raise ValueError("boom")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    with pytest.raises(ValueError, match="boom"):
        test_client_factory(app).post("/", content=b"x")


@pytest.mark.anyio
async def test_unrelated_error_after_exceeding_gets_413_only() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception:
            pass
        with pytest.raises(Exception):
            await receive()  # The stream stays stopped.
        raise ValueError("boom")

    app = RequestBodyLimitMiddleware(asgi, max_body_size=1)
    sent, _ = await call_asgi(app, [b"xx"])
    assert_sent_413(sent)  # The 413 is the answer; the error is a consequence of the cut-off.


@pytest.mark.anyio
async def test_exception_group_from_task_group_is_recognised() -> None:
    import anyio

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        async def read() -> None:
            await receive()

        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            tg.start_soon(read)

    sent, _ = await call_asgi(RequestBodyLimitMiddleware(asgi, max_body_size=1), [b"xx", b"xx"])
    assert_sent_413(sent)


@pytest.mark.anyio
async def test_disconnect_passes_through() -> None:
    messages: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        messages.append((await receive())["type"])
        messages.append((await receive())["type"])

    await call_asgi(RequestBodyLimitMiddleware(asgi, max_body_size=10), [b"xx"])
    assert messages == ["http.request", "http.disconnect"]


def test_streaming_response_listening_for_disconnect(test_client_factory: TestClientFactory) -> None:
    from starlette.responses import StreamingResponse

    async def endpoint(request: Request) -> StreamingResponse:
        async def gen() -> Any:
            yield b"x"

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    assert_413(test_client_factory(app).post("/", content=b"xxxx", headers={"content-length": "1"}))


def test_custom_500_handler_not_used_for_fail_fast(test_client_factory: TestClientFactory) -> None:
    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        return PlainTextResponse("custom", status_code=500)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        exception_handlers={500: handler},
        max_body_size=3,
    )
    assert_413(test_client_factory(app).post("/", content=b"x" * 3, headers={"content-length": "10"}))


# Multipart


def test_multipart_over_limit(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["file"].read())))  # type: ignore[union-attr]

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    assert client.post("/", files={"file": ("a.txt", io.BytesIO(b"x" * 500))}).text == "500"
    # The file data is below the limit, the multipart framing pushes it over.
    assert_413(client.post("/", files={"file": ("a.txt", io.BytesIO(b"x" * 990))}))
    assert_413(client.post("/", files={"file": ("a.txt", io.BytesIO(b"x" * 5000))}))


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette import formparsers

    created: list[Any] = []
    original = SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    async def endpoint(request: Request) -> PlainTextResponse:
        async with request.form():
            pass  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3000)
    head = b"--b\r\nContent-Disposition: form-data; name=f; filename=a.txt\r\n\r\n" + b"x" * 1000
    sent, _ = await call_asgi(
        app,
        [head, b"x" * 2500, b"x" * 2500, b"\r\n--b--\r\n"],
        headers=[(b"content-type", b"multipart/form-data; boundary=b")],
    )
    assert_sent_413(sent)
    assert len(created) == 1
    assert created[0].closed


@pytest.mark.anyio
async def test_multipart_parser_closes_files_on_any_interruption() -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    created: list[Any] = []

    async def stream() -> Any:
        yield b"--b\r\nContent-Disposition: form-data; name=f; filename=a.txt\r\n\r\nabc"
        raise KeyboardInterrupt

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(KeyboardInterrupt):
        await parser.parse()
    created.extend(parser._files_to_close_on_error)
    assert len(created) == 1
    assert created[0].closed


class PassThrough:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def test_inner_route_raises_limit_behind_wrapped_apps(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Host

    def inner() -> Router:
        return Router([Route("/", read_body, methods=["POST"], max_body_size=100)])

    app = Starlette(
        routes=[
            Mount("/a", app=PassThrough(inner())),
            Mount(
                "/b", app=CORSMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)]))
            ),
            Host("testserver", app=PassThrough(Mount("/c", app=PassThrough(inner())))),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path in ("/a/", "/b/", "/c/"):
        assert client.post(path, content=b"x" * 50).text == "50"
        assert_413(client.post(path, content=b"x" * 101))


def test_wrapped_plain_asgi_app_still_fails_fast(test_client_factory: TestClientFactory) -> None:
    called = False

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        await receive()  # pragma: no cover
        called = True  # pragma: no cover

    app = Starlette(routes=[Mount("/a", app=PassThrough(asgi))], max_body_size=10)
    assert_413(test_client_factory(app).post("/a/", content=b"x" * 11))
    assert not called


@pytest.mark.parametrize("where", ["route", "mount"])
def test_inner_limit_with_exception_from_middleware(test_client_factory: TestClientFactory, where: str) -> None:
    from starlette.exceptions import HTTPException

    class VerifyBody:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            request = Request(scope, receive)
            try:
                await request.body()
            except BaseException:
                raise HTTPException(status_code=400, detail="bad body")
            await self.app(scope, receive, send)  # pragma: no cover

    if where == "route":
        routes: Any = [Route("/x", read_body, methods=["POST"], middleware=[Middleware(VerifyBody)], max_body_size=3)]
        path = "/x"
    else:
        routes = [
            Mount(
                "/x",
                routes=[Route("/", read_body, methods=["POST"])],
                middleware=[Middleware(VerifyBody)],
                max_body_size=3,
            )
        ]
        path = "/x/"
    client = test_client_factory(Starlette(routes=routes))
    assert_413(client.post(path, content=chunks(b"xx", b"xx")))


@pytest.mark.parametrize("length", ["9" * 5000, "0" * 5000 + "2"])
def test_huge_content_length(test_client_factory: TestClientFactory, length: str) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    response = test_client_factory(app).post("/", content=b"xx", headers={"content-length": length})
    if length.startswith("9"):
        assert_413(response)
    else:
        assert response.status_code == 200 or response.status_code == 413  # h11 may reject a mismatched length

    mw = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])]), max_body_size=10)
    if length.startswith("9"):
        assert_413(test_client_factory(mw).post("/", content=b"xx", headers={"content-length": length}))


@pytest.mark.anyio
async def test_interrupted_413_is_not_repeated() -> None:
    import anyio

    sent: list[Message] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as tg:

            async def respond() -> None:
                await anyio.sleep(0.05)
                await send({"type": "http.response.start", "status": 200, "headers": []})

            async def listen() -> None:
                await receive()

            tg.start_soon(respond)
            tg.start_soon(listen)

    async def slow_send(message: Message) -> None:
        sent.append(message)
        await anyio.sleep(0.05)

    async def run() -> None:
        await RequestBodyLimitMiddleware(asgi, max_body_size=1)(
            {"type": "http", "headers": []},
            _one_chunk,
            slow_send,
        )

    await run()
    assert [m["type"] for m in sent].count("http.response.start") == 1


async def _one_chunk() -> Message:
    return {"type": "http.request", "body": b"xx"}


def test_invalid_limits_are_refused() -> None:
    for bad, exc in [(-1, ValueError), ("1024", TypeError), (True, TypeError)]:
        with pytest.raises(exc):
            Starlette(max_body_size=bad)  # type: ignore[arg-type]
        with pytest.raises(exc):
            Router(max_body_size=bad)  # type: ignore[arg-type]
        with pytest.raises(exc):
            RequestBodyLimitMiddleware(PlainTextResponse("x"), max_body_size=bad)  # type: ignore[arg-type]


def test_500_handler_not_called_for_streamed_body_over_app_limit(test_client_factory: TestClientFactory) -> None:
    seen: list[BaseException] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        seen.append(exc)  # pragma: no cover
        return PlainTextResponse("custom", status_code=500)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])], exception_handlers={500: handler}, max_body_size=3
    )
    assert_413(test_client_factory(app).post("/", content=chunks(b"xx", b"xx")))
    assert seen == []


def closure_middleware(app: Any) -> Any:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)

    return wrapped


def test_inner_route_raises_limit_behind_closure_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    def inner() -> Router:
        return Router([Route("/", read_body, methods=["POST"], max_body_size=100)])

    app = Starlette(
        routes=[
            Mount("/a", app=closure_middleware(inner())),
            Host(
                "testserver", app=closure_middleware(Starlette(routes=[Mount("/h", app=closure_middleware(inner()))]))
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path in ("/a/", "/h/"):
        assert client.post(path, content=b"x" * 50).text == "50"
        assert_413(client.post(path, content=b"x" * 101))
    # A route without its own limit behind an opaque wrapper uses the outer limit.
    plain = Starlette(
        routes=[Mount("/a", app=closure_middleware(Router([Route("/", read_body, methods=["POST"])])))],
        max_body_size=10,
    )
    assert_413(test_client_factory(plain).post("/a/", content=b"x" * 11))


@pytest.mark.parametrize("where", ["app", "route", "middleware"])
def test_base_http_middleware_over_limit(test_client_factory: TestClientFactory, where: str) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    class Noop(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            return await call_next(request)

    app: Any = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=3 if where == "route" else None)],
        middleware=[Middleware(Noop)],
        max_body_size=3 if where == "app" else None,
    )
    if where == "middleware":
        app = RequestBodyLimitMiddleware(app, max_body_size=3)
    client = test_client_factory(app)
    assert client.post("/", content=b"xxx").text == "3"
    assert_413(client.post("/", content=chunks(b"xx", b"xx")))
    assert_413(client.post("/", content=b"xxxx"))


def test_mount_limit_on_opaque_app(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Mount("/a", app=closure_middleware(Router([Route("/", read_body, methods=["POST"])])), max_body_size=5)]
    )
    client = test_client_factory(app)
    assert client.post("/a/", content=b"x" * 5).text == "5"
    assert_413(client.post("/a/", content=b"x" * 6))


def test_plain_asgi_app_under_mount_or_host_does_not_run(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    ran: list[str] = []

    async def plain(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append("app code")  # pragma: no cover

    app = Starlette(
        routes=[
            Mount("/raw", app=plain, max_body_size=5),
            Mount("/wrapped", app=closure_middleware(plain), max_body_size=5),
            Host("testserver", app=PassThrough(plain)),
        ],
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert_413(client.post("/raw", content=b"x" * 6))
    assert_413(client.post("/wrapped/", content=b"x" * 6))
    assert_413(client.post("/other", content=b"x" * 6))
    assert ran == []


async def streaming_endpoint(request: Request) -> Any:
    from starlette.responses import StreamingResponse

    async def gen() -> Any:
        yield b"hello"
        await anyio.sleep(0)
        yield b"world"

    return StreamingResponse(gen())


def count_starts(sent: list[Message]) -> list[int]:
    return [m["status"] for m in sent if m["type"] == "http.response.start"]


@pytest.mark.anyio
@pytest.mark.parametrize("level", ["app", "route", "middleware"])
async def test_streaming_response_over_limit_gets_exactly_one_413(level: str) -> None:
    # The response task and the disconnect listener run side by side; the 413 must be
    # sent whole, once, by the layer that owns the limit.
    if level == "app":
        app: Any = Starlette(routes=[Route("/", streaming_endpoint, methods=["POST"])], max_body_size=5)
    elif level == "route":
        app = Starlette(routes=[Route("/", streaming_endpoint, methods=["POST"], max_body_size=5)])
    else:
        app = RequestBodyLimitMiddleware(
            Starlette(routes=[Route("/", streaming_endpoint, methods=["POST"])]), max_body_size=5
        )
    sent, _ = await call_asgi(app, [b"x" * 20])
    assert_sent_413(sent)


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["app", "mount"])
async def test_opaque_streaming_app_with_declared_length_gets_one_413(where: str) -> None:
    async def streaming_asgi(scope: Scope, receive: Receive, send: Send) -> None:
        from starlette.responses import StreamingResponse

        async def gen() -> Any:
            yield b"hello"

        await StreamingResponse(gen())(scope, receive, send)

    app = Starlette(
        routes=[Mount("/a", app=closure_middleware(streaming_asgi), max_body_size=5 if where == "mount" else None)],
        max_body_size=5 if where == "app" else None,
    )
    sent, _ = await call_asgi(app, [b"x" * 20], path="/a/", headers=[(b"content-length", b"20")])
    assert_sent_413(sent)
