from __future__ import annotations

import contextlib
import io
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def size(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    return PlainTextResponse(str(len(await request.body())))


async def no_read(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    return PlainTextResponse("ran")


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert client.get("/").status_code == 405


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", size, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks() -> Any:
        yield b"x" * 6
        yield b"x" * 6

    assert_too_large(client.post("/", content=chunks()))


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert_too_large(response)


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/", content=b"x" * 6))
    assert calls == ["/"]


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", size, methods=["POST"])], max_body_size=5),
            Route("/free", size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/m/", content=b"x" * 6))
    assert client.post("/free", content=b"x" * 600).text == "600"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/small", size, methods=["POST"], max_body_size=5), Route("/free", size, methods=["POST"])]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/small", content=b"x" * 6))
    assert client.post("/free", content=b"x" * 600).text == "600"


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/plain", size, methods=["POST"]),
            Route("/raised", size, methods=["POST"], max_body_size=100),
            Route("/lowered", size, methods=["POST"], max_body_size=2),
            Mount(
                "/mount",
                routes=[
                    Route("/plain", size, methods=["POST"]),
                    Route("/route", size, methods=["POST"], max_body_size=3),
                ],
                max_body_size=100,
            ),
            Mount("/router", app=Router([Route("/", size, methods=["POST"])], max_body_size=50)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/plain", content=b"x" * 11))
    assert client.post("/raised", content=b"x" * 90).text == "90"
    assert_too_large(client.post("/raised", content=b"x" * 101))
    assert_too_large(client.post("/lowered", content=b"x" * 3))
    assert client.post("/mount/plain", content=b"x" * 90).text == "90"
    assert_too_large(client.post("/mount/route", content=b"x" * 4))
    assert client.post("/router/", content=b"x" * 40).text == "40"
    assert_too_large(client.post("/router/", content=b"x" * 51))


def test_content_length_fail_fast_uses_matched_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/plain", no_read, methods=["POST"]),
            Route("/raised", no_read, methods=["POST"], max_body_size=100),
            Route("/lowered", no_read, methods=["POST"], max_body_size=2),
            Mount("/m", routes=[Route("/", no_read, methods=["POST"])], max_body_size=3),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/plain", content=b"x" * 11))
    assert_too_large(client.post("/lowered", content=b"x" * 3))
    assert_too_large(client.post("/m/", content=b"x" * 4))
    assert calls == []
    # The endpoints never read the body: only the declared length matters here.
    assert client.post("/raised", content=b"x" * 50).text == "ran"
    assert calls == ["/raised"]


def test_content_length_fail_fast_without_match_or_method(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", no_read, methods=["POST"], max_body_size=1000)], max_body_size=10)
    client = test_client_factory(app, raise_server_exceptions=False)
    assert_too_large(client.post("/missing", content=b"x" * 11))
    assert_too_large(client.put("/", content=b"x" * 11))  # 405: the route's own limit doesn't apply
    assert client.put("/", content=b"x" * 10).status_code == 405
    assert client.post("/missing", content=b"x" * 10).status_code == 404


def test_content_length_fail_fast_with_middleware_in_between(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", no_read, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []


def test_body_read_through_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    class Reader(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            await request.body()
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", size, methods=["POST"])],
        middleware=[Middleware(Reader)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 50, headers={"content-length": "1"}))


def test_json_endpoint(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse(await request.json())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=20)
    client = test_client_factory(app)
    assert client.post("/", json={"a": 1}).json() == {"a": 1}
    assert_too_large(client.post("/", json={"a": "x" * 50}))


def test_endpoint_swallowing_exceptions_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        try:
            await request.body()
        except BaseException:
            pass
        return PlainTextResponse("swallowed")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    assert_too_large(test_client_factory(app).post("/", content=b"x" * 10))


def test_get_and_websocket_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    async def home(request: Request) -> PlainTextResponse:
        return PlainTextResponse("home")

    app = Starlette(routes=[Route("/", home), WebSocketRoute("/ws", ws)], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").text == "home"
    with client.websocket_connect("/ws") as session:
        session.send_text("a long message")
        assert session.receive_text() == "a long message"


def test_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    started: list[bool] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        started.append(True)
        yield

    app = Starlette(lifespan=lifespan, max_body_size=1)
    with test_client_factory(app):
        assert started == [True]


# Standalone middleware


async def asgi_reader(scope: Scope, receive: Receive, send: Send) -> None:
    calls.append("asgi")
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await PlainTextResponse(str(total))(scope, receive, send)


def test_middleware_with_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(asgi_reader, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=b"x" * 50, headers={"content-length": "1"}))
    assert calls == ["asgi"] * 2


def test_middleware_rejects_declared_length_on_entry(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(asgi_reader, max_body_size=10))
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []


def test_middleware_on_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", size, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))


def test_middleware_ignores_websocket_and_lifespan() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestBodyLimitMiddleware(app, max_body_size=0)

    async def run() -> None:
        for kind in ("websocket", "lifespan"):
            await middleware({"type": kind}, None, None)  # type: ignore[arg-type]

    anyio.run(run)
    assert seen == ["websocket", "lifespan"]


async def run_asgi(app: Any, chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None) -> list[Message]:
    sent: list[Message] = []
    incoming = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
    ]

    async def receive() -> Message:
        if incoming:
            return incoming.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers or [], "query_string": b""}
    await app(scope, receive, send)
    return sent


def test_response_already_started_gets_no_second_response() -> None:
    results: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi", "more_body": True})
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                results.append("stopped")
                break
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent = anyio.run(run_asgi, RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"def"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body", "http.response.body"]
    assert sent[0]["status"] == 200
    assert results == ["stopped"]


def test_response_started_then_exception_raised_is_not_answered_twice() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await receive()  # exceeds: returns a disconnect instead of raising
        await send({"type": "http.response.body", "body": b""})

    sent = anyio.run(run_asgi, RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"def"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


def test_streaming_response_started_before_limit(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> StreamingResponse:
        async def gen() -> Any:
            yield b"one"

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    client = test_client_factory(app)
    assert client.post("/", content=b"abc").text == "one"


# Multipart


async def form_endpoint(request: Request) -> JSONResponse:
    async with request.form() as form:
        return JSONResponse({k: (v if isinstance(v, str) else len(await v.read())) for k, v in form.items()})


def test_multipart_counts_file_data_and_overhead(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=400)
    client = test_client_factory(app)
    assert client.post("/", files={"f": ("a.txt", io.BytesIO(b"x" * 100))}).json() == {"f": 100}
    # The file itself fits within the limit, the multipart framing around it does not.
    assert_too_large(client.post("/", files={"f": ("a.txt", io.BytesIO(b"x" * 380))}))
    assert_too_large(client.post("/", files={"f": ("a.txt", io.BytesIO(b"x" * 5000))}))


def test_multipart_temp_files_closed_when_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette import formparsers

    created: list[Any] = []
    real = formparsers.SpooledTemporaryFile  # type: ignore[attr-defined]

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = real(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=300)
    first = b'--b\r\nContent-Disposition: form-data; name="f"; filename="f.txt"\r\n\r\n' + b"x" * 100
    second = b"y" * 300 + b"\r\n--b--\r\n"
    headers = [(b"content-type", b"multipart/form-data; boundary=b")]
    sent = anyio.run(run_asgi, app, [first, second], headers)
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"
    assert len(created) == 1
    assert created[0].closed


def test_multipart_parser_closes_files_on_any_interruption() -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    body = b'--b\r\nContent-Disposition: form-data; name="f"; filename="f.txt"\r\n\r\n' + b"x" * 100 + b"\r\n--b--\r\n"

    class Interrupted(BaseException):
        pass

    async def stream() -> Any:
        yield body[:90]
        raise Interrupted()

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())

    with pytest.raises(Interrupted):
        anyio.run(parser.parse)
    assert parser._files_to_close_on_error
    assert all(f.closed for f in parser._files_to_close_on_error)


# Causes found in review


class Wrap:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def chunked(count: int, each: int) -> Any:
    for _ in range(count):
        yield b"x" * each


def test_wrapped_router_or_app_in_mount_and_host_can_raise_limit(test_client_factory: TestClientFactory) -> None:
    def inner() -> Router:
        return Router([Route("/", no_read, methods=["POST"], max_body_size=100)])

    app = Starlette(
        routes=[
            Mount("/m", app=Wrap(inner())),
            Mount("/s", app=Wrap(Starlette(routes=[Route("/", no_read, methods=["POST"], max_body_size=100)]))),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 50).text == "ran"
    assert client.post("/s/", content=b"x" * 50).text == "ran"
    assert_too_large(client.post("/m/", content=b"x" * 101))


def test_host_wrapped_router_can_raise_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    inner = Wrap(Router([Route("/", no_read, methods=["POST"], max_body_size=100)]))
    client = test_client_factory(Starlette(routes=[Host("testserver", app=inner)], max_body_size=10))
    assert client.post("/", content=b"x" * 50).text == "ran"


def test_mounted_foreign_app_rejected_by_declared_length(test_client_factory: TestClientFactory) -> None:
    # An app Starlette can't see into may route further, so the declared length is
    # checked at its first receive() or response start, unless a router below settles it.
    app = Starlette(routes=[Mount("/m", app=asgi_reader)], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/m/", content=b"x" * 11))
    assert client.post("/m/", content=b"x" * 10).text == "10"


def test_unmatched_requests_use_the_application_limit(test_client_factory: TestClientFactory) -> None:
    raising = Starlette(
        routes=[Mount("/m", routes=[Route("/", no_read, methods=["POST"])], max_body_size=1000)], max_body_size=10
    )
    client = test_client_factory(raising, raise_server_exceptions=False)
    assert_too_large(client.post("/m/missing", content=b"x" * 11))
    assert_too_large(client.put("/m/", content=b"x" * 11))
    assert client.post("/m/missing", content=b"x" * 10).status_code == 404

    lowering = Starlette(
        routes=[Mount("/m", routes=[Route("/", no_read, methods=["POST"])], max_body_size=3)], max_body_size=10
    )
    assert test_client_factory(lowering).post("/m/missing", content=b"x" * 5).status_code == 404


@pytest.mark.parametrize("inner_limit", [None, 1000])
def test_standalone_limit_cannot_be_raised_from_below(
    test_client_factory: TestClientFactory, inner_limit: int | None
) -> None:
    inner = Starlette(routes=[Route("/", size, methods=["POST"], max_body_size=inner_limit)], max_body_size=inner_limit)
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert_too_large(client.post("/", content=chunked(5, 10)))
    assert_too_large(client.post("/", content=b"x" * 50))
    assert client.post("/", content=b"x" * 10).text == "10"


def test_standalone_limits_stack_and_in_middleware_list(test_client_factory: TestClientFactory) -> None:
    stacked = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(asgi_reader, 1000), 10)
    assert_too_large(test_client_factory(stacked).post("/", content=chunked(5, 10)))
    app = Starlette(
        routes=[Route("/", size, methods=["POST"], max_body_size=1000)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    assert_too_large(test_client_factory(app).post("/", content=chunked(5, 10)))


class Reader(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        await request.body()
        return await call_next(request)


def test_lowered_limit_applies_to_body_read_before_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/route", size, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/", size, methods=["POST"])], max_body_size=5),
        ],
        middleware=[Middleware(Reader)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/route", content=chunked(2, 5)))
    assert_too_large(client.post("/m/", content=chunked(2, 5)))
    assert client.post("/route", content=chunked(1, 5)).text == "5"


def test_limit_through_base_http_middleware_and_task_groups(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            return await call_next(request)

    async def grouped(request: Request) -> PlainTextResponse:
        result: list[int] = []

        async def read() -> None:
            result.append(len(await request.body()))

        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
        return PlainTextResponse(str(result[0]))

    app = Starlette(
        routes=[Route("/", size, methods=["POST"]), Route("/group", grouped, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    assert_too_large(client.post("/", content=chunked(4, 6)))
    assert_too_large(client.post("/", content=chunked(4, 6), headers={"content-length": "1"}))
    assert_too_large(client.post("/group", content=chunked(4, 6)))
    bare = Starlette(routes=[Route("/group", grouped, methods=["POST"])])
    assert_too_large(
        test_client_factory(RequestBodyLimitMiddleware(bare, 10), raise_server_exceptions=False).post(
            "/group", content=chunked(4, 6)
        )
    )


def test_huge_content_length_header(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app, raise_server_exceptions=False)
    assert_too_large(client.post("/", content=b"xxx", headers={"content-length": "9" * 5000}))
    middleware = RequestBodyLimitMiddleware(asgi_reader, 10)
    assert_too_large(test_client_factory(middleware).post("/", content=b"x", headers={"content-length": "9" * 5000}))


def test_streaming_after_limit_crossed_raises_no_error(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> StreamingResponse:
        async def gen() -> Any:
            async for chunk in request.stream():
                yield chunk

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5)
    test_client_factory(app).post("/", content=chunked(3, 3))


class WrapInner:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.inner(scope, receive, send)


def closure_wrap(app: Any) -> Any:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)

    return wrapped


@pytest.mark.parametrize("wrap", [Wrap, WrapInner, closure_wrap, lambda app: Wrap(WrapInner(closure_wrap(app)))])
def test_mount_sees_routers_through_any_wrapper(test_client_factory: TestClientFactory, wrap: Any) -> None:
    inner = wrap(Router([Route("/", no_read, methods=["POST"], max_body_size=100)]))
    app = Starlette(routes=[Mount("/m", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 50).text == "ran"
    assert_too_large(client.post("/m/", content=b"x" * 101))


@pytest.mark.parametrize("wrap", [lambda app: app, Wrap, WrapInner, closure_wrap])
def test_mounted_leaf_gets_no_response_through_when_declared_length_is_over(
    test_client_factory: TestClientFactory, wrap: Any
) -> None:
    async def responding_leaf(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("leaf")(scope, receive, send)

    app = Starlette(routes=[Mount("/m", app=wrap(responding_leaf))], max_body_size=10)
    assert_too_large(test_client_factory(app).post("/m/", content=b"x" * 50))


class BusyWrap(Wrap):
    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self.plugins = [object() for _ in range(1000)]


class StrictConfig:
    def __getattr__(self, name: str) -> Any:
        raise KeyError(name)


class ReferencingLeaf:
    def __init__(self) -> None:
        self.router = Router([Route("/", no_read)])
        self.config = StrictConfig()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("leaf")
        await PlainTextResponse("ran")(scope, receive, send)


def test_router_is_found_by_running_not_by_searching_objects(test_client_factory: TestClientFactory) -> None:
    inner = BusyWrap(Router([Route("/", no_read, methods=["POST"], max_body_size=100)]))
    app = Starlette(routes=[Mount("/m", app=inner)], max_body_size=10)
    assert test_client_factory(app).post("/m/", content=b"x" * 50).text == "ran"


def test_mounted_apps_holding_odd_objects_are_not_inspected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host
    from starlette.websockets import WebSocket

    async def ws_app(scope: Scope, receive: Receive, send: Send) -> None:
        websocket = WebSocket(scope, receive, send)
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    leaf = ReferencingLeaf()
    app = Starlette(routes=[Mount("/m", app=leaf), Host("testserver", app=leaf)])
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x").text == "ran"
    assert client.post("/", content=b"x").text == "ran"

    class WsLeaf(ReferencingLeaf):
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await ws_app(scope, receive, send)

    with test_client_factory(Starlette(routes=[Mount("/m", app=WsLeaf())], max_body_size=1)).websocket_connect(
        "/m/"
    ) as session:
        assert session.receive_text() == "hi"
    # A reference to a router the request never goes through doesn't skip the limit.
    limited = Starlette(routes=[Mount("/m", app=leaf)], max_body_size=10)
    assert_too_large(test_client_factory(limited).post("/m/", content=b"x" * 50))
