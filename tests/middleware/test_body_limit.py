from __future__ import annotations

from collections.abc import AsyncGenerator
from tempfile import SpooledTemporaryFile

import pytest

from starlette.applications import Starlette
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_body(request: Request) -> PlainTextResponse:
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


async def asgi_post(
    app: ASGIApp, path: str, chunks: list[bytes], headers: list[tuple[bytes, bytes]]
) -> tuple[int | None, bytes, list[Message], int]:
    """Drive an ASGI app directly, so that the `Content-Length` header can lie."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
    }
    pending = [{"type": "http.request", "body": c, "more_body": True} for c in chunks]
    pending.append({"type": "http.request", "body": b"", "more_body": False})
    received: list[Message] = []
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            message = pending.pop(0)
            received.append(message)
            return message
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, sent, len(received)


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert client.get("/").status_code == 405


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"12345").status_code == 200
    assert client.post("/", content=b"123456").status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Route("/", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"123456").status_code == 413
    assert client.post("/m/", content=b"12345").status_code == 200
    assert client.post("/", content=b"x" * 1000).status_code == 200


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Route("/", read_body, methods=["POST"], max_body_size=5))
    assert client.post("/", content=b"12345").status_code == 200
    assert client.post("/", content=b"123456").status_code == 413


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_body, methods=["POST"], max_body_size=100),
            Route("/lower", read_body, methods=["POST"], max_body_size=3),
            Route("/inherit", read_body, methods=["POST"]),
            Mount("/mount", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount("/router", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=100)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    big = b"x" * 50
    assert client.post("/raise", content=big).status_code == 200
    assert client.post("/mount/", content=big).status_code == 200
    assert client.post("/router/", content=big).status_code == 200
    assert client.post("/inherit", content=big).status_code == 413
    assert client.post("/lower", content=b"1234").status_code == 413
    assert client.post("/lower", content=b"123").status_code == 200


@pytest.mark.anyio
async def test_content_length_waits_for_routing() -> None:
    called: list[str] = []

    async def endpoint(request: Request) -> PlainTextResponse:
        called.append("endpoint")
        return PlainTextResponse("ok")

    async def not_found(request: Request, exc: Exception) -> PlainTextResponse:
        called.append("handler")  # pragma: no cover
        return PlainTextResponse("nope", status_code=404)  # pragma: no cover

    app = Starlette(
        routes=[
            Route("/raise", endpoint, methods=["POST"], max_body_size=100),
            Route("/inherit", endpoint, methods=["POST"]),
            Route("/lower", endpoint, methods=["POST"], max_body_size=2),
        ],
        exception_handlers={404: not_found, 405: not_found},
        max_body_size=10,
    )
    headers = [(b"content-length", b"50")]
    # An inner route raising the limit is still reached: the declared length is within its limit.
    status, _, _, _ = await asgi_post(app, "/raise", [], headers)
    assert (status, called) == (200, ["endpoint"])
    called.clear()
    # Rejected before the endpoint or any exception handler runs, without reading the body.
    for path, method_headers in [("/inherit", headers), ("/lower", [(b"content-length", b"5")]), ("/missing", headers)]:
        status, body, _, read = await asgi_post(app, path, [b"x"], method_headers)
        assert (status, body, read) == (413, b"Content Too Large", 0), path
    assert called == []


@pytest.mark.anyio
async def test_content_length_405_uses_app_limit() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["GET"], max_body_size=100)], max_body_size=10)
    status, _, _, _ = await asgi_post(app, "/", [], [(b"content-length", b"50")])
    assert status == 413


@pytest.mark.anyio
async def test_content_length_mounted_asgi_app() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=asgi)], max_body_size=10)
    status, _, _, _ = await asgi_post(app, "/m/x", [], [(b"content-length", b"50")])
    assert status == 413


@pytest.mark.anyio
async def test_understated_and_absent_content_length() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    for headers in ([(b"content-length", b"1")], [], [(b"content-length", b"bogus")]):
        status, body, _, _ = await asgi_post(app, "/", [b"x" * 6, b"x" * 6], headers)
        assert (status, body) == (413, b"Content Too Large")


@pytest.mark.anyio
async def test_middleware_rejects_on_entry() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("must not run")  # pragma: no cover

    app = RequestBodyLimitMiddleware(asgi, max_body_size=10)
    status, body, _, read = await asgi_post(app, "/", [b"x"], [(b"content-length", b"11")])
    assert (status, body, read) == (413, b"Content Too Large", 0)


@pytest.mark.anyio
async def test_middleware_with_plain_asgi_app() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await PlainTextResponse(str(total))(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=10)
    assert (await asgi_post(app, "/", [b"x" * 5], []))[:2] == (200, b"5")
    assert (await asgi_post(app, "/", [b"x" * 5, b"x" * 6], []))[:2] == (413, b"Content Too Large")
    assert (await asgi_post(RequestBodyLimitMiddleware(asgi), "/", [b"x" * 50], []))[:2] == (200, b"50")
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(asgi, max_body_size=-1)


@pytest.mark.anyio
async def test_middleware_passes_other_scopes_and_swallowed_errors() -> None:
    seen: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])
        if scope["type"] == "http":
            try:
                while True:
                    await receive()
            except Exception:
                await PlainTextResponse("handled", status_code=500)(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=1)
    await app({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    await app({"type": "websocket"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]
    # The application hides the error and answers itself: still a 413.
    assert (await asgi_post(app, "/", [b"xx"], []))[:2] == (413, b"Content Too Large")


@pytest.mark.anyio
async def test_response_already_started() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            await receive()

    app = RequestBodyLimitMiddleware(asgi, max_body_size=3)
    status, _, sent, _ = await asgi_post(app, "/", [b"xx", b"xx", b"xx"], [])
    assert status == 200
    assert [m["type"] for m in sent] == ["http.response.start"]  # no 413 on top of it


@pytest.mark.anyio
async def test_stream_stops_after_limit() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        with pytest.raises(Exception):
            await receive()
        with pytest.raises(Exception):
            await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RequestBodyLimitMiddleware(asgi, max_body_size=1)
    status, _, _, read = await asgi_post(app, "/", [b"xx", b"xx"], [])
    assert status == 413
    assert read == 1


def multipart_body(boundary: str, size: int) -> bytes:
    return (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + b"x" * size
        + f"\r\n--{boundary}--\r\n".encode()
    )


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> PlainTextResponse:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["f"].read())))  # type: ignore[union-attr]

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300)
    client = test_client_factory(app)
    headers = {"content-type": "multipart/form-data; boundary=b"}
    # 250 bytes of file data, but the body with its multipart framing is over 300 bytes.
    assert client.post("/", content=multipart_body("b", 100), headers=headers).text == "100"
    body = multipart_body("b", 250)
    assert len(body) > 300
    assert client.post("/", content=body, headers=headers).status_code == 413


@pytest.mark.anyio
async def test_multipart_closes_temp_files_when_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.datastructures import Headers

    files: list[SpooledTemporaryFile[bytes]] = []
    from starlette import formparsers

    original = SpooledTemporaryFile

    def tracking(max_size: int) -> SpooledTemporaryFile[bytes]:
        f: SpooledTemporaryFile[bytes] = original(max_size=max_size)
        files.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)
    body = multipart_body("b", 50)

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body[:150]
        raise KeyboardInterrupt  # not an Exception: used to leave the files open

    headers = Headers({"content-type": "multipart/form-data; boundary=b"})
    with pytest.raises(KeyboardInterrupt):
        await MultiPartParser(headers, stream()).parse()
    assert files and all(f.closed for f in files)


@pytest.mark.anyio
async def test_middleware_reraises_other_errors_and_passes_disconnect() -> None:
    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await asgi_post(RequestBodyLimitMiddleware(failing, max_body_size=10), "/", [], [])

    async def disconnected(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.disconnect"
        await PlainTextResponse("ok")(scope, receive, send)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "headers": []}
    await RequestBodyLimitMiddleware(disconnected, max_body_size=10)(scope, receive, send)
    assert sent[0]["status"] == 200


class Recorder:
    ran: list[str] = []

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        Recorder.ran.append(scope["type"])  # pragma: no cover
        await self.app(scope, receive, send)  # pragma: no cover


CL50 = [(b"content-length", b"50")]


@pytest.mark.anyio
async def test_no_route_or_method_inside_raising_mount_uses_app_limit() -> None:
    called: list[str] = []

    async def handler(request: Request, exc: Exception) -> PlainTextResponse:
        called.append("handler")  # pragma: no cover
        return PlainTextResponse("x", status_code=404)  # pragma: no cover

    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["GET"])], max_body_size=100),
            Mount("/r", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=100)),
        ],
        exception_handlers={404: handler, 405: handler},
        max_body_size=10,
    )
    for path in ("/m/missing", "/m/", "/r/missing"):
        status, body, _, _ = await asgi_post(app, path, [], CL50)
        assert (status, body) == (413, b"Content Too Large"), path
    assert called == []


@pytest.mark.anyio
async def test_mount_and_router_middleware_do_not_run_before_413() -> None:
    from starlette.middleware import Middleware

    Recorder.ran.clear()
    mount = Mount("/m", routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(Recorder)])
    inner = Router([Route("/", read_body, methods=["POST"])], middleware=[Middleware(Recorder)])
    apps = [
        Starlette(routes=[mount], max_body_size=10),
        Starlette(routes=[Mount("/m", app=inner)], max_body_size=10),
    ]
    for app in apps:
        status, _, _, _ = await asgi_post(app, "/m/", [], CL50)
        assert status == 413
    standalone = Router([Route("/", read_body, methods=["POST"])], middleware=[Middleware(Recorder)], max_body_size=10)
    assert (await asgi_post(standalone, "/", [], CL50))[0] == 413
    assert Recorder.ran == []


async def read_forever(receive: Receive) -> None:
    while True:
        await receive()


@pytest.mark.anyio
async def test_started_response_with_exception_group() -> None:
    import anyio

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        async with anyio.create_task_group() as tg:
            for _ in range(3):
                tg.start_soon(read_forever, receive)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=3)
    status, _, sent, _ = await asgi_post(app, "/", [b"xx", b"xx", b"xx"], [])
    assert status == 200
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_starlette_negative_limit_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        Starlette(max_body_size=-1)


@pytest.mark.anyio
async def test_limits_through_host_and_nested_applications() -> None:
    from starlette.routing import BaseRoute, Host, Match

    class Custom(BaseRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            return Match.FULL, {}

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(
        routes=[
            Host("testserver", app=inner),
            Mount("/c", routes=[Custom()]),
            Host("other", app=PlainTextResponse("x")),
        ],
        max_body_size=10,
    )
    host = [(b"host", b"testserver"), (b"content-length", b"50")]
    assert (await asgi_post(app, "/", [b"x"], host))[0] == 200
    assert (await asgi_post(app, "/c/", [], CL50))[0] == 413
    other = [(b"host", b"other"), (b"content-length", b"50")]
    assert (await asgi_post(app, "/", [], other))[0] == 413


class Wrap:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


@pytest.mark.anyio
async def test_inner_lower_limits_are_checked_below_outer_limit_layers() -> None:
    from starlette.middleware import Middleware

    called: list[str] = []

    async def endpoint(request: Request) -> PlainTextResponse:
        called.append("endpoint")  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    route = Route("/", endpoint, methods=["POST"], max_body_size=3)
    mount = Mount("/m", routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    wrapped = Mount("/w", app=Wrap(Router([Route("/", endpoint, methods=["POST"], max_body_size=3)])))
    limit = Middleware(RequestBodyLimitMiddleware, max_body_size=1000)
    apps: list[tuple[ASGIApp, str]] = [
        (Starlette(routes=[route], middleware=[limit]), "/"),
        (Starlette(routes=[route], middleware=[limit], max_body_size=10), "/"),
        (Starlette(routes=[mount], middleware=[limit]), "/m/"),
        (Starlette(routes=[wrapped], max_body_size=100), "/w/"),
        (RequestBodyLimitMiddleware(Starlette(routes=[route], max_body_size=10), max_body_size=1000), "/"),
        (RequestBodyLimitMiddleware(Starlette(max_body_size=10), max_body_size=1000), "/"),
    ]
    for app, path in apps:
        status, body, _, _ = await asgi_post(app, path, [], CL50)
        assert (status, body) == (413, b"Content Too Large"), path
    assert called == []


@pytest.mark.anyio
async def test_no_route_or_method_without_app_limit_is_not_limited() -> None:
    app = Starlette(routes=[Mount("/m", routes=[Route("/", read_body, methods=["GET"])], max_body_size=10)])
    assert (await asgi_post(app, "/m/", [], CL50))[0] == 405
    assert (await asgi_post(app, "/m/missing", [], CL50))[0] == 404
    # Matching a route of the mount still applies the mount's limit.
    app = Starlette(routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)])
    assert (await asgi_post(app, "/m/", [], CL50))[0] == 413
