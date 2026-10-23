from __future__ import annotations

import sys
from collections.abc import Iterator
from tempfile import SpooledTemporaryFile
from typing import Any
from unittest import mock

import pytest

from starlette.applications import Starlette
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

if sys.version_info < (3, 11):  # pragma: no cover
    from exceptiongroup import ExceptionGroup

TOO_LARGE = "Content Too Large"


async def read_body(request: Request) -> PlainTextResponse:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


class Spy:
    """Endpoint recording whether application code ran."""

    def __init__(self) -> None:
        self.calls = 0

    async def endpoint(self, request: Request) -> PlainTextResponse:
        self.calls += 1
        return await read_body(request)


async def call_asgi(
    app: Any,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    path: str = "/",
) -> list[Message]:
    """Drive an ASGI app directly, so the headers can differ from the real body."""
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }
    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    sent: list[Message] = []

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    try:
        await app(scope, receive, send)
    except Exception:  # pragma: no cover
        pass
    return sent


def status_and_body(sent: list[Message]) -> tuple[int, bytes]:
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, sent
    return starts[0]["status"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


# Application ----------------------------------------------------------------------------------------------


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == TOO_LARGE
    assert response.headers["content-type"].startswith("text/plain")


def test_zero_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["GET", "POST"])], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").text == "0"
    assert client.post("/", content=b"x").status_code == 413


def test_invalid_limit() -> None:
    for bad in (-1, 1.5, "1", True):
        with pytest.raises(ValueError):
            Starlette(max_body_size=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            RequestBodyLimitMiddleware(Starlette(), max_body_size=bad)  # type: ignore[arg-type]


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    def gen() -> Iterator[bytes]:
        for _ in range(5):
            yield b"x" * 4

    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=gen())
    assert response.status_code == 413
    assert response.text == TOO_LARGE


@pytest.mark.anyio
async def test_understated_content_length_does_not_let_body_through() -> None:
    spy = Spy()
    app = Starlette(routes=[Route("/", spy.endpoint, methods=["POST"])], max_body_size=10)
    sent = await call_asgi(app, [b"x" * 6, b"x" * 6], headers=[(b"content-length", b"3")])
    assert status_and_body(sent) == (413, TOO_LARGE.encode())


@pytest.mark.anyio
async def test_invalid_content_length_is_ignored() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent = await call_asgi(app, [b"x" * 5], headers=[(b"content-length", b"abc")])
    assert status_and_body(sent) == (200, b"5")


def test_json_is_limited(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        await request.json()
        return PlainTextResponse("ok")  # pragma: no cover

    client = test_client_factory(Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10))
    assert client.post("/", json={"a": "b" * 50}).status_code == 413  # fail fast
    assert client.post("/", content=iter([b'{"a": "' + b"b" * 50 + b'"}'])).status_code == 413  # counted


def test_error_handlers_are_not_invoked(test_client_factory: TestClientFactory) -> None:
    handled: list[Exception] = []

    def handler(request: Request, exc: Exception) -> PlainTextResponse:
        handled.append(exc)  # pragma: no cover
        return PlainTextResponse("handler", status_code=500)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        exception_handlers={Exception: handler},
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=True)
    response = client.post("/", content=iter([b"x" * 20]))
    assert response.status_code == 413
    assert response.text == TOO_LARGE
    assert handled == []


def test_endpoint_swallowing_the_error_cannot_send_a_second_response(
    test_client_factory: TestClientFactory,
) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        try:
            await request.body()
        except Exception:
            pass
        return PlainTextResponse("sneaky")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    response = test_client_factory(app).post("/", content=iter([b"x" * 20]))
    assert response.status_code == 413
    assert response.text == TOO_LARGE


@pytest.mark.anyio
async def test_started_response_gets_no_second_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        messages = []
        while True:
            message = await receive()
            messages.append(message["type"])
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await send({"type": "http.response.body", "body": ",".join(messages).encode()})

    sent = await call_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [b"xxx", b"xxx", b"xxx"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"http.request,http.disconnect"


def test_no_body_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    events: list[str] = []

    async def lifespan(app: Starlette) -> Any:
        events.append("start")
        yield
        events.append("stop")

    from contextlib import asynccontextmanager

    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    app = Starlette(
        routes=[Route("/", read_body, methods=["GET", "POST"]), WebSocketRoute("/ws", ws)],
        lifespan=asynccontextmanager(lifespan),
        max_body_size=0,
    )
    with test_client_factory(app) as client:
        assert client.get("/").text == "0"
        with client.websocket_connect("/ws") as session:
            session.send_text("x" * 100)
            assert session.receive_text() == "x" * 100
    assert events == ["start", "stop"]


# Content-Length fail-fast ---------------------------------------------------------------------------------


def test_content_length_fail_fast_route(test_client_factory: TestClientFactory) -> None:
    spy = Spy()
    app = Starlette(routes=[Route("/", spy.endpoint, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert spy.calls == 0


@pytest.mark.anyio
async def test_fail_fast_does_not_read_the_body() -> None:
    spy = Spy()
    app = Starlette(routes=[Route("/", spy.endpoint, methods=["POST"])], max_body_size=10)
    received = 0

    async def receive() -> Message:
        nonlocal received
        received += 1  # pragma: no cover
        return {"type": "http.request"}  # pragma: no cover

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-length", b"11")],
        "query_string": b"",
        "root_path": "",
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert received == 0 and spy.calls == 0


def test_fail_fast_waits_for_routing_inner_route_raises(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/big", content=b"x" * 50).text == "50"  # declared length above the app limit
    assert client.post("/big", content=b"x" * 101).status_code == 413
    assert client.post("/", content=b"x" * 50).status_code == 413


def test_fail_fast_unmatched_and_wrong_method_use_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/missing", content=b"x" * 11).status_code == 413
    assert client.post("/missing", content=b"x" * 5).status_code == 404
    # 405: the route's own (higher) limit does not apply, the application's does
    assert client.put("/", content=b"x" * 11).status_code == 413
    assert client.put("/", content=b"x" * 5).status_code == 405
    assert client.post("/", content=b"x" * 500).text == "500"


def test_fail_fast_route_without_own_limit_uses_outer(test_client_factory: TestClientFactory) -> None:
    spy = Spy()
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/", spy.endpoint, methods=["POST"])], max_body_size=20)],
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 21).status_code == 413
    assert spy.calls == 0
    assert client.post("/m/", content=b"x" * 20).text == "20"


def test_fail_fast_middleware_standalone_rejects_on_entry() -> None:
    entered: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.append(True)  # pragma: no cover

    import anyio

    sent = anyio.run(
        call_asgi, RequestBodyLimitMiddleware(app, max_body_size=3), [b"xxxx"], [(b"content-length", b"4")]
    )
    assert status_and_body(sent) == (413, TOO_LARGE.encode())
    assert entered == []


def test_custom_default_not_run_for_oversized_unmatched(test_client_factory: TestClientFactory) -> None:
    ran: list[bool] = []

    async def default(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append(True)  # pragma: no cover

    router = Router(default=default, max_body_size=3)
    client = test_client_factory(router)
    assert client.post("/x", content=b"xxxx").status_code == 413
    assert ran == []


# Router, Mount, Route -------------------------------------------------------------------------------------


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=b"x" * 11).text == TOO_LARGE
    assert client.post("/", content=iter([b"x" * 6, b"x" * 6])).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 10).text == "10"
    assert client.post("/m/", content=b"x" * 11).status_code == 413
    assert client.post("/m/", content=iter([b"x" * 11])).status_code == 413
    assert client.post("/free", content=b"x" * 1000).text == "1000"


def test_mount_of_plain_asgi_app_fails_fast(test_client_factory: TestClientFactory) -> None:
    ran: list[bool] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append(True)  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=asgi, max_body_size=3)])
    assert test_client_factory(app).post("/m/x", content=b"xxxx").status_code == 413
    assert ran == []


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 5).text == "5"
    assert client.post("/small", content=b"x" * 6).status_code == 413
    assert client.post("/small", content=iter([b"x" * 6])).status_code == 413
    assert client.post("/other", content=b"x" * 600).text == "600"


def test_route_limit_runs_before_route_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    seen: list[bool] = []

    class Spying:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(True)  # pragma: no cover
            await self.app(scope, receive, send)  # pragma: no cover

    app = Starlette(routes=[Route("/", read_body, methods=["POST"], middleware=[Middleware(Spying)], max_body_size=3)])
    assert test_client_factory(app).post("/", content=b"xxxx").status_code == 413
    assert seen == []


def test_inner_limits_raise_and_lower_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=3),
            Mount(
                "/mount",
                max_body_size=50,
                routes=[
                    Route("/", read_body, methods=["POST"]),
                    Route("/lowered", read_body, methods=["POST"], max_body_size=2),
                ],
            ),
            Mount("/router", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=30)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)

    def post(path: str, size: int, stream: bool = False) -> int:
        content: Any = iter([b"x" * size]) if stream else b"x" * size
        return client.post(path, content=content).status_code

    for stream in (False, True):
        assert post("/", 10, stream) == 200 and post("/", 11, stream) == 413
        assert post("/raised", 100, stream) == 200 and post("/raised", 101, stream) == 413
        assert post("/lowered", 3, stream) == 200 and post("/lowered", 4, stream) == 413
        assert post("/mount/", 50, stream) == 200 and post("/mount/", 51, stream) == 413
        assert post("/mount/lowered", 2, stream) == 200 and post("/mount/lowered", 3, stream) == 413
        assert post("/router/", 30, stream) == 200 and post("/router/", 31, stream) == 413


def test_inner_limit_without_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=4)])
    assert test_client_factory(app).post("/", content=iter([b"x" * 5])).text == TOO_LARGE


def test_lowered_limit_below_already_received_bytes(test_client_factory: TestClientFactory) -> None:
    class ReadsFirst:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            message = await receive()
            first = True

            async def replay() -> Message:  # pragma: no cover
                nonlocal first
                if first:
                    first = False
                    return message
                return await receive()

            await self.app(scope, replay, send)

    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=2)],
        middleware=[Middleware(ReadsFirst)],
        max_body_size=100,
    )
    assert test_client_factory(app).post("/", content=b"xxxx").status_code == 413


# Standalone middleware ------------------------------------------------------------------------------------


def test_standalone_middleware_with_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        await PlainTextResponse(str(len(await request.body())))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=4))
    assert client.post("/", content=b"xxxx").text == "4"
    response = client.post("/", content=iter([b"xx", b"xxx"]))
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert client.post("/", content=b"xxxxx").status_code == 413


def test_standalone_middleware_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(
        RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    )
    assert client.post("/", content=b"x" * 5000).text == "5000"


def test_standalone_middleware_in_starlette_middleware_list(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=3)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"xxx").text == "3"
    assert client.post("/", content=b"xxxx").status_code == 413


def test_standalone_middleware_ignores_non_http(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            websocket = WebSocket(scope, receive, send)
            await websocket.accept()
            await websocket.send_text(await websocket.receive_text())
            await websocket.close()
        else:  # pragma: no cover
            pass

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=1))
    with client.websocket_connect("/") as session:
        session.send_text("long message")
        assert session.receive_text() == "long message"


# Multipart ------------------------------------------------------------------------------------------------

BOUNDARY = "a7f7ac8d4e2e437c877bb7b8d7cc549c"


def multipart_content(file_size: int) -> tuple[bytes, dict[str, str]]:
    content = (
        (
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="field"\r\n\r\n'
            "value\r\n"
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="file"; filename="a.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + b"f" * file_size
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )
    return content, {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


async def form_endpoint(request: Request) -> PlainTextResponse:
    async with request.form() as form:
        return PlainTextResponse(str(len(await form["file"].read())))  # type: ignore[union-attr]


def test_multipart_counts_encoding_overhead(test_client_factory: TestClientFactory) -> None:
    content, headers = multipart_content(10)
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=len(content))
    client = test_client_factory(app)
    assert client.post("/", content=content, headers=headers).text == "10"

    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=len(content) - 1)
    client = test_client_factory(app)
    # The file data alone (10 bytes) is far below the limit; the multipart framing pushes it over.
    response = client.post("/", content=content, headers=headers)
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    response = client.post("/", content=iter([content]), headers=headers)
    assert response.status_code == 413


@pytest.mark.anyio
async def test_multipart_large_file_rejected_and_tempfiles_closed() -> None:
    created: list[SpooledTemporaryFile[bytes]] = []

    class Tracking(SpooledTemporaryFile):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    content, headers = multipart_content(5000)
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=2000)
    chunks = [content[i : i + 500] for i in range(0, len(content), 500)]
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    with mock.patch("starlette.formparsers.SpooledTemporaryFile", Tracking):
        sent = await call_asgi(app, chunks, raw_headers)
    assert status_and_body(sent) == (413, TOO_LARGE.encode())
    assert len(created) == 1
    assert created[0].closed


def test_multipart_tempfiles_closed_on_any_interruption(test_client_factory: TestClientFactory) -> None:
    created: list[SpooledTemporaryFile[bytes]] = []

    class Tracking(SpooledTemporaryFile):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    content, headers = multipart_content(100)
    interrupted: list[Exception] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        sent_first = False

        async def flaky_receive() -> Message:
            nonlocal sent_first
            if not sent_first:
                sent_first = True
                return {"type": "http.request", "body": content[:-30], "more_body": True}
            raise RuntimeError("interrupted")

        request = Request(scope, flaky_receive)
        try:
            await request.form()
        except RuntimeError as exc:
            interrupted.append(exc)
        await PlainTextResponse("done")(scope, receive, send)

    with mock.patch("starlette.formparsers.SpooledTemporaryFile", Tracking):
        assert test_client_factory(app).post("/", content=content, headers=headers).text == "done"
    assert len(interrupted) == 1
    assert len(created) == 1 and created[0].closed


# Edge cases -----------------------------------------------------------------------------------------------


def test_exception_group_of_rejections_is_recognised() -> None:
    from starlette.middleware.body_limit import RequestBodyTooLarge, _is_too_large

    assert _is_too_large(RequestBodyTooLarge())
    assert _is_too_large(ExceptionGroup("g", [RequestBodyTooLarge(), ExceptionGroup("h", [RequestBodyTooLarge()])]))
    assert not _is_too_large(ExceptionGroup("g", [RequestBodyTooLarge(), ValueError()]))
    assert not _is_too_large(ValueError())


def test_retrying_the_read_after_a_rejection_keeps_failing(test_client_factory: TestClientFactory) -> None:
    attempts: list[str] = []

    async def endpoint(request: Request) -> PlainTextResponse:
        for _ in range(2):
            try:
                await request.body()
            except Exception as exc:
                attempts.append(type(exc).__name__)
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    response = test_client_factory(app).post("/", content=iter([b"xxxx"]))
    assert response.status_code == 413
    assert attempts == ["RequestBodyTooLarge", "RequestBodyTooLarge"]


def test_other_exceptions_still_propagate(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        raise ValueError("boom")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    response = test_client_factory(app, raise_server_exceptions=False).post("/", content=b"x")
    assert response.status_code == 500


def test_custom_route_class_is_checked_before_handling(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import BaseRoute, Match

    ran: list[bool] = []

    class CustomRoute(BaseRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            return Match.FULL, {}

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            ran.append(True)  # pragma: no cover

    client = test_client_factory(Starlette(routes=[CustomRoute()], max_body_size=3))
    assert client.post("/", content=b"xxxx").status_code == 413
    assert ran == []


def test_host_route_checks_declared_length(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Host("testserver", app=asgi)], max_body_size=3)
    client = test_client_factory(app)
    assert client.post("/", content=b"xxx").text == "ok"
    assert client.post("/", content=b"xxxx").status_code == 413


@pytest.mark.anyio
async def test_receive_after_exceeding_on_started_response_keeps_disconnecting() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(4):
            seen.append((await receive())["type"])
        await send({"type": "http.response.body", "body": b""})

    await call_asgi(RequestBodyLimitMiddleware(app, max_body_size=2), [b"xxx", b"xxx"])
    assert seen == ["http.disconnect"] * 4


@pytest.mark.anyio
async def test_client_disconnect_passes_through() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((await receive())["type"])
        await PlainTextResponse("ok")(scope, receive, send)

    await call_asgi(RequestBodyLimitMiddleware(app, max_body_size=2), [])
    assert seen == ["http.disconnect"]
