from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def read_body(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


async def asgi_read(scope: Scope, receive: Receive, send: Send) -> None:
    calls.append("asgi")
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await PlainTextResponse(f"read {size}")(scope, receive, send)


def chunks(*sizes: int) -> Iterator[bytes]:
    def gen() -> Iterator[bytes]:
        for size in sizes:
            yield b"x" * size

    return gen()


async def run_asgi(
    app: Starlette | RequestBodyLimitMiddleware | Router,
    headers: list[tuple[bytes, bytes]],
    bodies: list[bytes],
    path: str = "/",
) -> list[Message]:
    """Drive an app by hand, which allows for a lying Content-Length."""
    sent: list[Message] = []
    queue = list(bodies)

    async def receive() -> Message:
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        await anyio.sleep_forever()  # pragma: no cover
        raise AssertionError  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
    }
    await app(scope, receive, send)
    return sent


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=chunks(5, 6))
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert response.headers["content-type"].startswith("text/plain")


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_exception_handlers_do_not_see_the_limit(test_client_factory: TestClientFactory) -> None:
    seen: list[Exception] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:  # pragma: no cover
        seen.append(exc)
        return PlainTextResponse("custom", status_code=500)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        exception_handlers={Exception: handler},
        max_body_size=3,
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    assert client.post("/", content=chunks(2, 2)).status_code == 413
    assert seen == []


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=4)
    client = test_client_factory(app)
    assert client.post("/", content=b"xxxx").status_code == 200
    assert client.post("/", content=b"xxxxx").status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/limited", routes=[Route("/", read_body, methods=["POST"])], max_body_size=4),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/limited/", content=b"xxxx").status_code == 200
    assert client.post("/limited/", content=b"xxxxx").status_code == 413
    assert client.post("/free", content=b"x" * 1000).status_code == 200


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_body, methods=["POST"], max_body_size=4),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/limited", content=b"xxxx").status_code == 200
    assert client.post("/limited", content=b"xxxxx").status_code == 413
    assert client.post("/free", content=b"x" * 1000).status_code == 200


def test_inner_limits_raise_and_lower_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=2),
            Route("/inherited", read_body, methods=["POST"]),
            Mount("/mount", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount("/router", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)),
            Mount("/lowmount", routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=2),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    big = b"x" * 50
    for path in ("/raised", "/mount/", "/router/", "/lowmount/"):
        assert client.post(path, content=big).status_code == 200, path
        assert client.post(path, content=chunks(60, 30)).status_code == 200, path
    assert client.post("/lowered", content=b"xxx").status_code == 413
    assert client.post("/inherited", content=b"x" * 11).status_code == 413
    assert client.post("/inherited", content=b"x" * 10).status_code == 200
    assert client.post("/mount/", content=b"x" * 101).status_code == 413
    assert client.post("/router/", content=b"x" * 101).status_code == 413


def test_lowering_after_bytes_were_already_read(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")  # pragma: no cover

    class Reader:
        def __init__(self, app: Router) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await receive()  # consume 5 bytes before reaching the limited route
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"], max_body_size=2)],
        middleware=[],
        max_body_size=100,
    )
    app.add_middleware(Reader)  # type: ignore[arg-type]
    assert test_client_factory(app).post("/", content=b"xxxxx").status_code == 413


def test_content_length_fail_fast_in_application() -> None:
    async def run() -> None:
        app = Starlette(
            routes=[
                Route("/", read_body, methods=["POST"]),
                Route("/raised", read_body, methods=["POST"], max_body_size=100),
                Route("/lowered", read_body, methods=["POST"], max_body_size=2),
                Mount("/asgi", app=asgi_read),
                Mount("/inner", routes=[Route("/", read_body, methods=["POST"], max_body_size=100)]),
                Host("example.org", app=asgi_read),
            ],
            max_body_size=10,
        )
        header = [(b"content-length", b"50"), (b"host", b"testserver")]
        # over the application limit: rejected, no application code ran
        sent = await run_asgi(app, header, [b"x" * 50], "/")
        assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
        sent = await run_asgi(app, header, [b"x" * 50], "/lowered")
        assert sent[0]["status"] == 413
        sent = await run_asgi(app, header, [b"x" * 50], "/asgi/x")
        assert sent[0]["status"] == 413
        # no route, wrong method: the application's limit applies
        sent = await run_asgi(app, header, [b"x" * 50], "/nothing")
        assert sent[0]["status"] == 413
        sent = await run_asgi(app, header, [b"x" * 50], "/raised")
        assert sent[0]["status"] == 200
        sent = await run_asgi(app, header, [b"x" * 50], "/inner/")
        assert sent[0]["status"] == 200
        assert calls == ["/raised", "/inner/"]

        # a method that does not match: the application's limit, not the route's
        get_app = Starlette(routes=[Route("/", read_body, methods=["GET"], max_body_size=100)], max_body_size=10)
        sent = await run_asgi(get_app, header, [b"x" * 50], "/")
        assert sent[0]["status"] == 413

    anyio.run(run)


def test_content_length_is_not_trusted() -> None:
    async def run() -> None:
        app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
        # understated header
        sent = await run_asgi(app, [(b"content-length", b"3")], [b"x" * 6, b"x" * 6])
        assert sent[0]["status"] == 413
        # absent header
        sent = await run_asgi(app, [], [b"x" * 6, b"x" * 6])
        assert sent[0]["status"] == 413
        # junk header is ignored
        sent = await run_asgi(app, [(b"content-length", b"abc")], [b"x" * 6])
        assert sent[0]["status"] == 200

    anyio.run(run)


def test_middleware_standalone() -> None:
    async def run() -> None:
        app = RequestBodyLimitMiddleware(asgi_read, max_body_size=10)
        sent = await run_asgi(app, [], [b"x" * 10])
        assert sent[0]["status"] == 200 and sent[1]["body"] == b"read 10"
        sent = await run_asgi(app, [], [b"x" * 6, b"x" * 6])
        assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
        assert sent[0]["status"] == 413 and sent[1]["body"] == b"Content Too Large"
        # rejected on entry: the app never ran
        calls.clear()
        sent = await run_asgi(app, [(b"content-length", b"11")], [b"x" * 11])
        assert sent[0]["status"] == 413
        assert calls == []

    anyio.run(run)


def test_middleware_other_scopes_unaffected() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    app = RequestBodyLimitMiddleware(inner, max_body_size=0)
    anyio.run(app, {"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    anyio.run(app, {"type": "websocket"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]


def test_middleware_without_body(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    assert test_client_factory(app).get("/").text == "read 0"


def test_negative_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(asgi_read, max_body_size=-1)


def test_response_already_started() -> None:
    async def streaming(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi", "more_body": True})
        while True:
            message = await receive()
            if not message.get("more_body"):  # pragma: no cover
                break

    async def run() -> None:
        app = RequestBodyLimitMiddleware(streaming, max_body_size=5)
        sent = await run_asgi(app, [], [b"x" * 3, b"x" * 3])
        assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
        assert sent[0]["status"] == 200

    anyio.run(run)


def test_application_swallowing_the_error_still_gets_413() -> None:
    async def swallowing(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while True:
                await receive()
        except BaseException:
            await PlainTextResponse("fine")(scope, receive, send)

    async def run() -> None:
        sent = await run_asgi(RequestBodyLimitMiddleware(swallowing, max_body_size=5), [], [b"x" * 3, b"x" * 3])
        assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
        assert sent[-1]["body"] == b"Content Too Large"

    anyio.run(run)


def test_application_returning_without_response_gets_413() -> None:
    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
            await receive()
        except BaseException:
            pass

    async def run() -> None:
        sent = await run_asgi(RequestBodyLimitMiddleware(silent, max_body_size=5), [], [b"x" * 3, b"x" * 3])
        assert sent[0]["status"] == 413

    anyio.run(run)


def test_unrelated_errors_propagate() -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    async def run() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            await run_asgi(RequestBodyLimitMiddleware(broken, max_body_size=5), [], [b"x"])

    anyio.run(run)


def test_multipart_counts_encoding_overhead(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> PlainTextResponse:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["file"].read())))  # type: ignore[union-attr]

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300)
    client = test_client_factory(app)
    assert client.post("/", files={"file": ("a.txt", b"x" * 50)}).text == "50"
    # The file data alone fits within the limit, the multipart framing does not.
    response = client.post("/", files={"file": ("a.txt", b"x" * 290)})
    assert response.status_code == 413


def test_multipart_temp_files_closed_when_limit_is_crossed(
    test_client_factory: TestClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tempfile import SpooledTemporaryFile

    created: list[SpooledTemporaryFile[bytes]] = []
    real = SpooledTemporaryFile

    def tracking(*args, **kwargs):  # type: ignore[no-untyped-def]
        file = real(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)

    async def upload(request: Request) -> PlainTextResponse:
        async with request.form():
            pass  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300)
    head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'

    async def run() -> None:
        sent = await run_asgi(
            app,
            [(b"content-type", b"multipart/form-data; boundary=b")],  # no Content-Length
            [head + b"x" * 100, b"x" * 200, b"\r\n--b--\r\n"],
        )
        assert sent[0]["status"] == 413

    anyio.run(run)
    assert len(created) == 1 and created[0].closed


def test_limit_through_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"xxxxx").status_code == 200
    assert client.post("/", content=chunks(3, 3)).status_code == 413


def test_disconnect_is_passed_through() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.disconnect"
        await PlainTextResponse("gone")(scope, receive, send)

    async def run() -> None:
        scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            sent.append(message)

        await RequestBodyLimitMiddleware(inner, max_body_size=1)(scope, receive, send)
        assert sent[0]["status"] == 200

    anyio.run(run)


async def _drive(
    app: Any, headers: list[tuple[bytes, bytes]], bodies: list[bytes], path: str = "/", method: str = "POST"
) -> list[Message]:
    """Drive an app with a server whose receive checkpoints and which only disconnects after the response."""
    done = anyio.Event()
    queue = list(bodies)
    sent: list[Message] = []

    async def receive() -> Message:
        await anyio.sleep(0)
        if queue:
            return {"type": "http.request", "body": queue.pop(0), "more_body": bool(queue)}
        await done.wait()  # pragma: no cover
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
    }
    with anyio.fail_after(3):
        await app(scope, receive, send)
    return sent


def _statuses(sent: list[Message]) -> list[int]:
    return [m["status"] for m in sent if m["type"] == "http.response.start"]


@pytest.mark.parametrize("where", ["app", "route"])
def test_is_disconnected_before_reading_keeps_the_body(where: str) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        assert not await request.is_disconnected()
        return PlainTextResponse(str(len(await request.body())))

    if where == "app":
        app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=100)
    else:
        app = Starlette(routes=[Route("/", endpoint, methods=["POST"], max_body_size=100)])
    sent = anyio.run(_drive, app, [], [b"hello"])
    assert sent[1]["body"] == b"5"


class _Buffering:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replay_receive() -> Message:
            return replay.pop() if replay else await receive()

        await self.app(scope, replay_receive, send)


@pytest.mark.parametrize("level", ["app", "route", "mount"])
def test_middleware_that_already_read_the_body_does_not_hang(level: str) -> None:
    from starlette.middleware import Middleware

    if level == "mount":
        app = Starlette(
            routes=[
                Mount(
                    "/m",
                    routes=[Route("/", read_body, methods=["POST"])],
                    middleware=[Middleware(_Buffering)],
                    max_body_size=100,
                )
            ]
        )
        path = "/m/"
    else:
        route = Route("/", read_body, methods=["POST"], max_body_size=100 if level == "route" else None)
        app = Starlette(
            routes=[route], middleware=[Middleware(_Buffering)], max_body_size=100 if level == "app" else None
        )
        path = "/"
    sent = anyio.run(_drive, app, [], [b"hello"], path)
    assert sent[1]["body"] == b"read 5"


def test_endpoint_answering_before_the_client_sends_its_body() -> None:
    class Duplex:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            message = await receive()
            await send({"type": "http.response.body", "body": message["body"]})

    duplex = Duplex()

    async def run() -> list[Message]:
        sent: list[Message] = []
        started = anyio.Event()

        async def receive() -> Message:
            await started.wait()
            return {"type": "http.request", "body": b"hi", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)
            started.set()

        scope = {"type": "http", "method": "POST", "path": "/", "raw_path": b"/", "query_string": b"", "headers": []}
        app = Starlette(routes=[Route("/", duplex, methods=["POST"], max_body_size=100)], max_body_size=100)
        with anyio.fail_after(3):
            await app(scope, receive, send)
        return sent

    assert anyio.run(run)[1]["body"] == b"hi"


def _unmatched_app(limit: int) -> Starlette:
    inner = Router(routes=[Route("/get", read_body, methods=["GET"])], max_body_size=limit)
    return Starlette(
        routes=[
            Mount("/api", routes=[Route("/get", read_body, methods=["GET"])], max_body_size=limit),
            Mount("/r", app=inner),
        ],
        max_body_size=10,
    )


@pytest.mark.parametrize("path", ["/api/get", "/api/nothing", "/r/get", "/r/nothing"])
def test_no_route_or_method_inside_a_raising_mount_uses_the_outer_limit(path: str) -> None:
    sent = anyio.run(_drive, _unmatched_app(1000), [(b"content-length", b"50")], [b"x" * 50], path)
    assert _statuses(sent) == [413]


def test_router_wrapped_by_middleware_can_raise_the_limit() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/m", app=GZipMiddleware(inner))], max_body_size=10)
    sent = anyio.run(_drive, app, [(b"content-length", b"50")], [b"x" * 50], "/m/")
    assert _statuses(sent) == [200]


def test_standalone_limit_cannot_be_raised_from_below() -> None:
    inner_routes = [Route("/", read_body, methods=["POST"], max_body_size=1000)]
    apps: list[Any] = [
        RequestBodyLimitMiddleware(Starlette(routes=inner_routes), max_body_size=10),
        RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=1000), 10),
        RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(asgi_read, max_body_size=1000), max_body_size=10),
        Starlette(routes=inner_routes, middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)]),
    ]
    for app in apps:
        sent = anyio.run(run_asgi, app, [], [b"x" * 5, b"x" * 30])
        assert _statuses(sent) == [413]


@pytest.mark.parametrize("value", [b"9" * 5000, b"0" * 5000 + b"3"])
def test_huge_content_length(value: bytes) -> None:
    for app in (
        Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
        Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=10)]),
        RequestBodyLimitMiddleware(asgi_read, max_body_size=10),
    ):
        sent = anyio.run(run_asgi, app, [(b"content-length", value)], [b"x" * 3])
        assert _statuses(sent) == ([413] if value.startswith(b"9") else [200])


@pytest.mark.parametrize("value", [float("nan"), 1.5, True, "10", -1])
def test_invalid_limits_are_rejected(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        Starlette(max_body_size=value)
    with pytest.raises((TypeError, ValueError)):
        RequestBodyLimitMiddleware(asgi_read, max_body_size=value)


@pytest.mark.parametrize("path", ["/api/get", "/api/nothing", "/r/get", "/r/nothing"])
def test_no_route_or_method_inside_a_lowering_mount_uses_the_application_limit(path: str) -> None:
    app = Starlette(
        routes=[
            Mount("/api", routes=[Route("/get", read_body, methods=["GET"])], max_body_size=5),
            Mount("/r", app=Router(routes=[Route("/get", read_body, methods=["GET"])], max_body_size=5)),
        ],
        max_body_size=100,
    )
    # 50 is over the mount's limit (5) but under the application's (100): not a 413
    sent = anyio.run(_drive, app, [(b"content-length", b"50")], [b"x" * 50], path)
    assert _statuses(sent) in ([404], [405])
    sent = anyio.run(_drive, app, [(b"content-length", b"500")], [b"x" * 50], path)
    assert _statuses(sent) == [413]


def test_content_length_zero_is_served() -> None:
    for header in (b"0", b"000"):
        for app in (
            Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
            RequestBodyLimitMiddleware(asgi_read, max_body_size=10),
        ):
            sent = anyio.run(run_asgi, app, [(b"content-length", header)], [b""])
            assert _statuses(sent) == [200]


def test_no_route_or_method_inside_a_limited_mount_without_application_limit() -> None:
    app = Starlette(
        routes=[
            Mount("/api", routes=[Route("/get", read_body, methods=["GET"])], max_body_size=5),
            Mount("/r", app=Router(routes=[Route("/get", read_body, methods=["GET"])], max_body_size=5)),
        ]
    )
    for path, status in [("/api/get", 405), ("/api/nothing", 404), ("/r/get", 405), ("/r/nothing", 404)]:
        sent = anyio.run(_drive, app, [(b"content-length", b"50")], [b"x" * 50], path)
        assert _statuses(sent) == [status], path


class _ReadsBodyAndAnswers:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):  # pragma: no branch
                break
        await PlainTextResponse(f"answered {total}")(scope, receive, send)  # pragma: no cover


def test_lowering_limit_covers_the_middleware_of_the_mount_or_router() -> None:
    mount = Mount(
        "/m",
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(_ReadsBodyAndAnswers)],
        max_body_size=5,
    )
    router = Router(
        routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(_ReadsBodyAndAnswers)], max_body_size=5
    )
    for app in (
        Starlette(routes=[mount], max_body_size=100),
        Starlette(routes=[Mount("/m", app=router)], max_body_size=100),
        Starlette(routes=[mount]),
    ):
        sent = anyio.run(run_asgi, app, [], [b"x" * 2, b"x" * 50], "/m/")
        assert _statuses(sent) == [413]


def test_server_receive_that_yields_after_dequeuing_loses_nothing() -> None:
    async def run() -> list[Message]:
        queue = [b"a" * 5, b"b" * 5]
        sent: list[Message] = []

        async def receive() -> Message:
            body = queue.pop(0)
            await anyio.sleep(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}

        async def send(message: Message) -> None:
            sent.append(message)

        app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
        scope = {"type": "http", "method": "POST", "path": "/", "raw_path": b"/", "query_string": b"", "headers": []}
        await app(scope, receive, send)
        return sent

    assert anyio.run(run)[1]["body"] == b"read 10"


@pytest.mark.parametrize("mounted", [False, True])
def test_starlette_app_keeps_its_limit_for_unmatched_requests_when_mounted(mounted: bool) -> None:
    sub = Starlette(routes=[Route("/get", read_body, methods=["GET"])], max_body_size=10)
    app = Starlette(routes=[Mount("/sub", app=sub)]) if mounted else sub
    prefix = "/sub" if mounted else ""
    for path in ("/get", "/nothing"):
        sent = anyio.run(_drive, app, [(b"content-length", b"50")], [b"x" * 50], prefix + path)
        assert _statuses(sent) == [413], path


def test_http_endpoint_wrong_method_uses_the_application_limit() -> None:
    from starlette.endpoints import HTTPEndpoint

    class GetOnly(HTTPEndpoint):
        async def get(self, request: Request) -> PlainTextResponse:  # pragma: no cover
            return PlainTextResponse("ok")

    raising = Starlette(routes=[Route("/e", GetOnly, max_body_size=100)], max_body_size=10)
    sent = anyio.run(_drive, raising, [(b"content-length", b"50")], [b"x" * 50], "/e")
    assert _statuses(sent) == [413]
    lowering = Starlette(routes=[Route("/e", GetOnly, max_body_size=5)], max_body_size=100)
    sent = anyio.run(_drive, lowering, [(b"content-length", b"50")], [b"x" * 50], "/e")
    assert _statuses(sent) == [405]
    # HEAD falls back to get, and a partial of the class is still an HTTPEndpoint
    import functools

    partial = Starlette(routes=[Route("/e", functools.partial(GetOnly), max_body_size=5)], max_body_size=100)
    sent = anyio.run(_drive, partial, [(b"content-length", b"50")], [b"x" * 50], "/e")
    assert _statuses(sent) == [405]
    sent = anyio.run(_drive, partial, [(b"content-length", b"50")], [b"x" * 50], "/e", "HEAD")
    assert _statuses(sent) == [413]


@pytest.mark.parametrize("standalone", [False, True])
def test_application_error_next_to_the_limit_error_is_not_swallowed(standalone: bool) -> None:
    class DatabaseDown(Exception): ...

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        async def read() -> None:
            while True:
                if not (await receive()).get("more_body"):  # pragma: no cover
                    break

        async def fail() -> None:
            raise DatabaseDown()

        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            tg.start_soon(fail)

    app: Any = RequestBodyLimitMiddleware(endpoint, max_body_size=5) if standalone else None
    if app is None:
        app = Starlette(routes=[Mount("/", app=endpoint)], max_body_size=5)

    async def run() -> None:
        with pytest.raises(BaseException) as info:
            await run_asgi(app, [], [b"x" * 50])
        names = {type(e).__name__ for e in getattr(info.value, "exceptions", [info.value])}
        assert "DatabaseDown" in names

    anyio.run(run)


def test_http_endpoint_unhandled_method_with_explicit_methods_and_odd_names() -> None:
    from starlette.endpoints import HTTPEndpoint

    class GetOnly(HTTPEndpoint):
        async def get(self, request: Request) -> PlainTextResponse:  # pragma: no cover
            return PlainTextResponse("ok")

    raising = Starlette(routes=[Route("/e", GetOnly, methods=["GET", "POST"], max_body_size=100)], max_body_size=10)
    lowering = Starlette(routes=[Route("/e", GetOnly, methods=["GET", "POST"], max_body_size=5)], max_body_size=100)
    headers = [(b"content-length", b"50")]
    assert _statuses(anyio.run(_drive, raising, headers, [b"x" * 50], "/e")) == [413]
    assert _statuses(anyio.run(_drive, lowering, headers, [b"x" * 50], "/e")) == [405]

    open_app = Starlette(routes=[Route("/e", GetOnly, max_body_size=100)], max_body_size=10)
    for method in ("METHOD_NOT_ALLOWED", "DISPATCH", "__INIT__"):
        assert _statuses(anyio.run(_drive, open_app, headers, [b"x" * 50], "/e", method)) == [413]


def test_mount_hand_off_rejects_declared_length_when_nothing_below_can_raise_the_limit() -> None:
    class Wrapper:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("wrapper")
            await PlainTextResponse("answered")(scope, receive, send)

    headers = [(b"content-length", b"50")]
    closed = Starlette(routes=[Mount("/m", app=Wrapper(Router(routes=[])), max_body_size=10)])
    assert _statuses(anyio.run(_drive, closed, headers, [b"x" * 50], "/m/")) == [413]
    assert calls == []
    raised = Starlette(routes=[Mount("/m", app=Wrapper(Router(routes=[], max_body_size=100)), max_body_size=10)])
    assert _statuses(anyio.run(_drive, raised, headers, [b"x" * 50], "/m/")) == [200]
    nested = Starlette(
        routes=[Mount("/m", app=Wrapper(Router(routes=[Route("/", read_body, max_body_size=100)])), max_body_size=10)]
    )
    assert _statuses(anyio.run(_drive, nested, headers, [b"x" * 50], "/m/")) == [200]


def test_http_endpoint_unhandled_method_counts_bytes_against_the_application_limit() -> None:
    from starlette.endpoints import HTTPEndpoint

    class GetOnly(HTTPEndpoint):
        async def get(self, request: Request) -> PlainTextResponse:  # pragma: no cover
            return PlainTextResponse("ok")

    async def handler(request: Request, exc: Exception) -> PlainTextResponse:
        return PlainTextResponse(str(len(await request.body())), status_code=405)  # pragma: no cover

    app = Starlette(
        routes=[Route("/e", GetOnly, max_body_size=100)],
        exception_handlers={405: handler},
        max_body_size=10,
    )
    for method in ("POST", "METHOD_NOT_ALLOWED"):
        sent = anyio.run(_drive, app, [], [b"x" * 5, b"x" * 45], "/e", method)
        assert _statuses(sent) == [413], method
