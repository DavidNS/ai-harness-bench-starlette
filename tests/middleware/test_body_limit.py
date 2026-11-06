from __future__ import annotations

import sys
from tempfile import SpooledTemporaryFile
from typing import Any

import anyio
import pytest

if sys.version_info < (3, 11):  # pragma: no cover
    from exceptiongroup import BaseExceptionGroup

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def read_body(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


async def ignore_body(request: Request) -> Response:
    calls.append(request.url.path)
    return PlainTextResponse("ignored")


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    calls.clear()


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/", content=b"x" * 11))


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).text == "read 100000"


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks() -> Any:
        for _ in range(4):
            yield b"abcd"

    assert_too_large(client.post("/", content=chunks()))


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 50, headers={"content-length": "5"}))


def test_content_length_fails_fast_before_endpoint(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []
    assert client.post("/", content=b"x" * 5).text == "ignored"


def test_invalid_content_length_is_ignored() -> None:
    received: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        received.append(len(message["body"]))
        await PlainTextResponse("ok")(scope, receive, send)

    scope = {"type": "http", "headers": [(b"content-length", b"abc")]}
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"xx"}

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert received == [2]


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/", content=b"x" * 11))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/small", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small/", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/small/", content=b"x" * 11))
    assert client.post("/free", content=b"x" * 1000).text == "read 1000"


def test_mount_limit_on_asgi_app_fails_fast(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Mount("/m", app=asgi, max_body_size=10)]))
    assert_too_large(client.post("/m/x", content=b"x" * 11))
    assert calls == []
    assert client.post("/m/x", content=b"x" * 10).text == "ok"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=10),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/small", content=b"x" * 11))
    assert client.post("/free", content=b"x" * 1000).text == "read 1000"


def test_route_limit_without_other_limits_fails_fast(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", ignore_body, methods=["POST"], max_body_size=10)]))
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []


def test_inner_limits_raise_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/route", ignore_body, methods=["POST"], max_body_size=100),
            Mount(
                "/mount",
                routes=[
                    Route("/", ignore_body, methods=["POST"]),
                    Route("/r", read_body, methods=["POST"], max_body_size=1000),
                ],
                max_body_size=50,
            ),
            Mount("/router", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=200)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # the declared Content-Length is checked against the limit of the matched route
    assert client.post("/route", content=b"x" * 100).text == "ignored"
    assert_too_large(client.post("/route", content=b"x" * 101))
    assert client.post("/mount/", content=b"x" * 50).text == "ignored"
    assert_too_large(client.post("/mount/", content=b"x" * 51))
    assert client.post("/mount/r", content=b"x" * 1000).text == "read 1000"
    assert_too_large(client.post("/mount/r", content=b"x" * 1001))
    assert client.post("/router/", content=b"x" * 200).text == "read 200"
    assert_too_large(client.post("/router/", content=b"x" * 201))


def test_inner_limits_raise_with_streamed_body(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=10)
    client = test_client_factory(app)

    def chunks(n: int) -> Any:
        for _ in range(n):
            yield b"x" * 10

    assert client.post("/", content=chunks(10)).text == "read 100"
    assert_too_large(client.post("/", content=chunks(11)))


def test_inner_limits_lower_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/route", ignore_body, methods=["POST"], max_body_size=10),
            Mount("/mount", routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=20),
            Mount("/router", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=30)),
            Route("/outer", read_body, methods=["POST"]),
        ],
        max_body_size=1000,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/route", content=b"x" * 11))
    assert_too_large(client.post("/mount/", content=b"x" * 21))
    assert_too_large(client.post("/router/", content=b"x" * 31))
    assert calls == []
    assert client.post("/outer", content=b"x" * 1000).text == "read 1000"
    assert_too_large(client.post("/outer", content=b"x" * 1001))


def test_nested_starlette_app(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    client = test_client_factory(Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10))
    assert client.post("/inner/", content=b"x" * 100).text == "read 100"
    assert_too_large(client.post("/inner/", content=b"x" * 101))


def test_standalone_middleware_with_asgi_app() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("app")
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await PlainTextResponse(str(len(body)))(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == ["app"]  # content-length fail-fast, the app did not run the second time

    def chunks() -> Any:
        for _ in range(3):
            yield b"x" * 5

    assert_too_large(client.post("/", content=chunks()))
    assert client.get("/").text == "0"


def test_standalone_middleware_without_limit() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app))
    assert client.post("/", content=b"x" * 10_000).text == "ok"


def test_standalone_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/", content=b"x" * 11))


def test_exceeding_is_not_swallowed_by_endpoint(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:  # pragma: no cover
            return PlainTextResponse("swallowed")
        return PlainTextResponse("done")  # pragma: no cover

    client = test_client_factory(Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3))
    assert_too_large(client.post("/", content=b"x" * 10, headers={"content-length": "1"}))


def test_exceeding_with_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    async def dispatch(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        return response  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=dispatch)],
        max_body_size=3,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 10, headers={"content-length": "1"}))


def test_endpoint_swallowing_the_error_still_gets_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        for _ in range(2):  # the body stream stays stopped after the error was swallowed
            with pytest.raises(BaseException):
                await receive()

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=3))
    assert_too_large(client.post("/", content=b"x" * 10, headers={"content-length": "1"}))


def test_response_already_started() -> None:
    sent: list[Message] = []
    after: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            await send({"type": "http.response.body", "body": message["body"], "more_body": True})
            after.append("chunk")

    chunks = [{"type": "http.request", "body": b"abcd", "more_body": True}] * 5

    async def receive() -> Message:
        return chunks.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), {"type": "http", "headers": []}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"] + ["http.response.body"] * 2
    assert not any(m.get("status") == 413 for m in sent)
    assert after == ["chunk", "chunk"]
    assert len(chunks) == 2  # the body stream was stopped


def test_requests_without_body_and_other_scopes(test_client_factory: TestClientFactory) -> None:
    events: list[str] = []

    async def lifespan(app: Any) -> Any:
        events.append("lifespan")
        yield

    from contextlib import asynccontextmanager

    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(
        routes=[Route("/", JSONResponse_endpoint), WebSocketRoute("/ws", ws, name="ws")],
        lifespan=asynccontextmanager(lifespan),
        max_body_size=1,
    )
    with test_client_factory(app) as client:
        assert client.get("/").text == "ok"
        with client.websocket_connect("/ws") as session:
            session.send_text("hello world, longer than the limit")
            assert session.receive_text() == "hello world, longer than the limit"
    assert events == ["lifespan"]


async def JSONResponse_endpoint(request: Request) -> Response:
    return PlainTextResponse("ok")


def multipart_body(file_size: int) -> tuple[bytes, str]:
    boundary = "xxboundaryxx"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + b"f" * file_size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


async def form_endpoint(request: Request) -> Response:
    async with request.form() as form:
        upload: Any = form["file"]
        return PlainTextResponse(f"file {upload.size}")


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    body, content_type = multipart_body(100)
    limit = len(body)
    headers = {"content-type": content_type}
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=limit)
    client = test_client_factory(app)
    assert client.post("/", content=body, headers=headers).text == "file 100"

    # the file is within the limit, but the multipart overhead is not
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=limit - 1)
    assert_too_large(test_client_factory(app).post("/", content=body, headers=headers))


@pytest.mark.anyio
async def test_multipart_streamed_over_limit_closes_files(monkeypatch: pytest.MonkeyPatch) -> None:

    created: list[Any] = []
    original = SpooledTemporaryFile

    def spy(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", spy)

    body, content_type = multipart_body(1000)
    messages: list[Message] = [
        {"type": "http.request", "body": body[i : i + 100], "more_body": i + 100 < len(body)}
        for i in range(0, len(body), 100)
    ]
    total = len(messages)
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=500)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", content_type.encode())],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert created[0].closed
    assert len(messages) == total - 6  # the body stream was stopped at the 6th chunk, over 500 bytes


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
async def test_multipart_closes_files_when_interrupted(exc: BaseException, monkeypatch: pytest.MonkeyPatch) -> None:
    import starlette.formparsers as formparsers
    from starlette.datastructures import Headers

    created: list[Any] = []
    original = SpooledTemporaryFile

    def spy(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", spy)
    body, content_type = multipart_body(100)

    async def stream() -> Any:
        yield body[:-20]
        raise exc

    parser = formparsers.MultiPartParser(Headers({"content-type": content_type}), stream())
    with pytest.raises(type(exc)):
        await parser.parse()
    assert len(created) == 1
    assert created[0].closed


def test_unrelated_exception_groups_propagate() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise BaseExceptionGroup("group", [RuntimeError("boom")])

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=3))
    with pytest.raises(BaseExceptionGroup):
        client.get("/")


def test_content_length_checked_on_first_receive_without_leaf(test_client_factory: TestClientFactory) -> None:
    async def default(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("default")
        await receive()  # pragma: no cover
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    app = Starlette(routes=[], max_body_size=3)
    app.router.default = default
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 10))
    assert calls == ["default"]


@pytest.mark.anyio
async def test_disconnect_message_passes_through() -> None:
    seen: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(await receive())

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        raise NotImplementedError  # pragma: no cover

    await RequestBodyLimitMiddleware(app, max_body_size=1)({"type": "http", "headers": []}, receive, send)
    assert seen == [{"type": "http.disconnect"}]


class PassThrough:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def _streamed(n: int) -> Any:
    for _ in range(n):
        yield b"x" * 10


def test_wrapped_inner_limit_raises_with_and_without_content_length() -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    wrapped_router = PassThrough(Router([Route("/", read_body, methods=["POST"])], max_body_size=100))
    for app in (
        Starlette(routes=[Mount("/m", app=PassThrough(inner))], max_body_size=10),
        Starlette(routes=[Mount("/m", app=PassThrough(wrapped_router))], max_body_size=10),
    ):
        client = TestClient(app)
        assert client.post("/m/", content=_streamed(5)).text == "read 50"
        assert client.post("/m/", content=b"x" * 50).text == "read 50"
        assert_too_large(client.post("/m/", content=b"x" * 101))


def test_standalone_middleware_around_starlette_defers_to_inner_limit() -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    client = TestClient(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert client.post("/", content=_streamed(5)).text == "read 50"
    assert client.post("/", content=b"x" * 50).text == "read 50"
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = TestClient(app)
    assert client.post("/", content=b"x" * 50).text == "read 50"


def test_host_content_length_fails_fast() -> None:
    from starlette.routing import Host

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(Starlette(routes=[Host("testserver", app=asgi)], max_body_size=10))
    assert_too_large(client.post("/", content=b"x" * 50))
    assert calls == []
    assert client.post("/", content=b"x" * 5).text == "ok"


def test_unchecked_content_length_is_checked_when_response_starts() -> None:
    async def default(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    app = Starlette(max_body_size=10)
    app.router.default = default
    assert_too_large(TestClient(app).post("/", content=b"x" * 50))


def test_inner_lower_limit_counts_bytes_already_read() -> None:
    class Replay:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            body = b""
            more = True
            while more:
                message = await receive()
                body += message.get("body", b"")
                more = message.get("more_body", False)

            async def replay() -> Message:  # pragma: no cover
                return {"type": "http.request", "body": body, "more_body": False}

            await self.app(scope, replay, send)

    for routes in (
        [Route("/", read_body, methods=["POST"], max_body_size=10)],
        [Mount("/", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)],
    ):
        app = Starlette(routes=routes, middleware=[Middleware(Replay)], max_body_size=1000)
        assert_too_large(TestClient(app).post("/", content=_streamed(50)))

    async def dispatch(request: Request, call_next: Any) -> Response:
        await request.body()
        response: Response = await call_next(request)
        return response  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=10)],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=dispatch)],
        max_body_size=1000,
    )
    assert_too_large(TestClient(app).post("/", content=_streamed(50)))


def test_negative_limit_does_not_affect_requests_without_body() -> None:
    inner = Starlette(routes=[Route("/", read_body)])
    assert TestClient(RequestBodyLimitMiddleware(inner, max_body_size=-1)).get("/").status_code == 200


def test_deeply_wrapped_router_and_opaque_app_holding_a_router() -> None:
    inner: Any = Router([Route("/", read_body, methods=["POST"])], max_body_size=100)
    for _ in range(20):
        inner = PassThrough(inner)
    client = TestClient(Starlette(routes=[Mount("/m", app=inner)], max_body_size=10))
    assert client.post("/m/", content=b"x" * 50).text == "read 50"

    class Opaque:
        def __init__(self) -> None:
            self.urls = Router([])

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("opaque")  # pragma: no cover
            await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    calls.clear()
    client = TestClient(Starlette(routes=[Mount("/m", app=Opaque())], max_body_size=10))
    assert_too_large(client.post("/m/x", content=b"x" * 50))
    assert calls == []


def test_hostile_attributes_do_not_break_mounting() -> None:
    class Proxy:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("working outside of context")

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Mount("/sub", app=Proxy())])
    assert TestClient(app).get("/sub/x").text == "ok"


def test_unrelated_error_next_to_the_limit_surfaces() -> None:
    async def endpoint(request: Request) -> Response:
        async def read() -> None:
            await request.body()

        async def work() -> None:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0.05)
                raise RuntimeError("database failed")

        async with anyio.create_task_group() as tg:
            tg.start_soon(work)
            tg.start_soon(read)
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    with pytest.raises(BaseException) as info:
        TestClient(app).post("/", content=_streamed(5))
    assert "database failed" in repr(info.value) + repr(getattr(info.value, "exceptions", ""))


class Hiding:
    """Keeps the next app where no layer can see it."""

    def __init__(self, app: Any) -> None:
        self.apps = {"default": app}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.apps["default"](scope, receive, send)


def test_limit_is_a_ceiling_below_an_app_that_cannot_be_seen_into() -> None:
    def make() -> Starlette:
        return Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])

    for app in (
        Starlette(routes=[Mount("/", app=Hiding(make()))], max_body_size=10),
        RequestBodyLimitMiddleware(Hiding(make()), max_body_size=10),
    ):
        client = TestClient(app)
        # the header and the streamed bytes give the same answer: the inner limit can't raise the ceiling
        assert_too_large(client.post("/", content=b"x" * 50))
        assert_too_large(client.post("/", content=_streamed(5)))
        assert client.post("/", content=b"x" * 10).text == "read 10"


def test_inner_limit_may_lower_a_ceiling() -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=5)])
    client = TestClient(Starlette(routes=[Mount("/", app=Hiding(inner))], max_body_size=10))
    assert_too_large(client.post("/", content=b"x" * 8))
    assert client.post("/", content=b"x" * 5).text == "read 5"
