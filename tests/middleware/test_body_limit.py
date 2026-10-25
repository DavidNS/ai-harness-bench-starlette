from __future__ import annotations

from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Host, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def echo_size(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return JSONResponse({"size": len(body)})


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    calls.clear()


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_get_without_body_is_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size)], max_body_size=0)
    assert test_client_factory(app).get("/").json() == {"size": 0}


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks() -> Any:
        yield b"x" * 6
        yield b"x" * 6

    assert client.post("/", content=chunks()).status_code == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert response.status_code == 413


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5),
            Route("/free", echo_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert client.post("/free", content=b"x" * 600).status_code == 200


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/a", echo_size, methods=["POST"], max_body_size=5),
            Route("/b", echo_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/a", content=b"x" * 6).status_code == 413
    assert client.post("/b", content=b"x" * 600).status_code == 200


@pytest.mark.parametrize("declared", [True, False])
def test_inner_limit_raises_outer(test_client_factory: TestClientFactory, declared: bool) -> None:
    app = Starlette(
        routes=[
            Route("/small", echo_size, methods=["POST"]),
            Route("/big", echo_size, methods=["POST"], max_body_size=100),
            Mount("/m", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=100),
            Mount("/r", app=Router([Route("/", echo_size, methods=["POST"])], max_body_size=100)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)

    def post(path: str, size: int) -> int:
        content: Any = b"x" * size if declared else iter([b"x" * size])
        return client.post(path, content=content).status_code

    assert post("/small", 11) == 413
    for path in ("/big", "/m/", "/r/"):
        assert post(path, 100) == 200
        assert post(path, 101) == 413


def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/a", echo_size, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5),
            Route("/b", echo_size, methods=["POST"]),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/a", content=b"x" * 6).status_code == 413
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert client.post("/b", content=b"x" * 6).status_code == 200
    assert client.post("/b", content=b"x" * 101).status_code == 413


def test_content_length_fail_fast_never_runs_application_code(test_client_factory: TestClientFactory) -> None:
    seen: list[str] = []

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            seen.append("middleware")
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/", echo_size, methods=["POST"]),
            Route("/route", echo_size, methods=["POST"], max_body_size=5, middleware=[Middleware(Spy)]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path in ("/", "/route", "/missing"):
        assert client.post(path, content=b"x" * 50).status_code == 413
    # 405: the application limit applies when no method matches.
    assert client.put("/", content=b"x" * 50).status_code == 413
    assert client.put("/", content=b"x" * 5).status_code == 405
    assert calls == []
    assert seen == []


def test_content_length_fail_fast_waits_for_inner_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"], max_body_size=100)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).status_code == 200


def test_unmatched_route_uses_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/nope", content=b"x" * 11).status_code == 413
    assert client.post("/nope", content=b"x" * 5).status_code == 404


def test_mounted_asgi_app_content_length(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=asgi)], max_body_size=10)
    assert test_client_factory(app).post("/m/x", content=b"x" * 11).status_code == 413
    assert calls == []


def test_limit_applies_to_middleware_reading_body(test_client_factory: TestClientFactory) -> None:
    class ReadBody(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"])],
        middleware=[Middleware(ReadBody)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=iter([b"x" * 20])).status_code == 413


def test_base_http_middleware_downstream(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=iter([b"x" * 20])).status_code == 413
    assert client.post("/", content=b"x" * 5).status_code == 200


def test_exception_handlers_cannot_swallow(test_client_factory: TestClientFactory) -> None:
    async def handler(request: Request, exc: Exception) -> Response:
        return PlainTextResponse("handled", status_code=500)  # pragma: no cover

    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:  # pragma: no cover
            return PlainTextResponse("swallowed")
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"])],
        exception_handlers={Exception: handler},
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.post("/", content=iter([b"x" * 20]))
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_invalid_max_body_size() -> None:
    with pytest.raises(ValueError):
        Starlette(max_body_size=-1)
    with pytest.raises(ValueError):
        Route("/", echo_size, max_body_size=-1)
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(Router(), max_body_size=True)


def test_base_http_middleware_limit_crossed_while_streaming_response(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"first"
            await request.body()
            yield b"second"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # Nothing has reached the server yet when the body crosses the limit.
    assert client.post("/", content=iter([b"x" * 20])).status_code == 413


# Standalone middleware


async def raw_app(scope: Scope, receive: Receive, send: Send) -> None:
    calls.append("raw")
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await PlainTextResponse(str(size))(scope, receive, send)


def test_middleware_with_plain_asgi(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(raw_app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=iter([b"x" * 6, b"x" * 6]))
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_middleware_rejects_on_entry(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(raw_app, max_body_size=10))
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert calls == []


def test_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=iter([b"x" * 11])).status_code == 413
    assert client.post("/", content=b"x" * 10).status_code == 200


@pytest.mark.anyio
async def test_middleware_lifespan_and_websocket_untouched() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=0)
    for kind in ("lifespan", "websocket"):
        await wrapped({"type": kind}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]


@pytest.mark.anyio
async def test_response_already_started_sends_no_second_response() -> None:
    final: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                final.append(message)
                break
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    messages: list[Message] = []
    chunks = [b"x" * 6, b"x" * 6, b"x" * 6]

    started = anyio.Event()

    async def receive() -> Message:
        await started.wait()  # the body only arrives once the response has started
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": True}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)
        started.set()

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)
    assert [m["type"] for m in messages] == ["http.response.start"] + ["http.response.body"] * 2
    assert messages[0]["status"] == 200
    # The body stream was stopped: the app saw it end, with nothing past the limit.
    assert final == [{"type": "http.request", "body": b"", "more_body": False}]


@pytest.mark.anyio
async def test_receive_stays_stopped_after_limit() -> None:
    errors = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal errors
        try:
            await receive()
        except BaseException:
            errors += 1
            try:
                await receive()
            except BaseException:
                errors += 1
                raise

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 20, "more_body": True}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)
    assert errors == 2
    assert sent[0]["status"] == 413


# Multipart


def multipart_app(max_body_size: int | None) -> Starlette:
    async def upload(request: Request) -> Response:
        async with request.form() as form:
            return JSONResponse({"keys": sorted(form.keys())})

    return Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=max_body_size)


def test_multipart_counts_file_data_and_overhead(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(multipart_app(max_body_size=200))
    assert client.post("/", files={"f": ("a.txt", b"x" * 10)}).status_code == 200
    # The file itself fits in the limit, but the encoded body doesn't.
    assert client.post("/", files={"f": ("a.txt", b"x" * 190)}).status_code == 413
    response = client.post("/", files={"f": ("a.txt", b"x" * 5000)})
    assert response.status_code == 413
    assert response.text == "Content Too Large"


@pytest.mark.anyio
async def test_multipart_streamed_closes_temp_files(monkeypatch: pytest.MonkeyPatch) -> None:
    import starlette.formparsers as formparsers

    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile  # type: ignore[attr-defined]

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)
    boundary = "xyz"
    chunks = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode()
        + b"x" * 100
    ] + [b"x" * 100] * 10

    async def receive() -> Message:
        return {"type": "http.request", "body": chunks.pop(0), "more_body": True}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }
    await multipart_app(max_body_size=400)(scope, receive, send)
    assert sent[0]["status"] == 413
    assert created and all(file.closed for file in created)


@pytest.mark.anyio
async def test_multipart_parser_closes_files_on_any_interruption() -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    created: list[Any] = []

    async def stream() -> Any:
        yield (b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n' + b"x" * 10)
        raise anyio.get_cancelled_exc_class()()

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(BaseException):
        await parser.parse()
    created.extend(parser._files_to_close_on_error)
    assert created and all(file.closed for file in created)


@pytest.mark.anyio
async def test_dropped_responses_after_limit_and_other_errors_propagate() -> None:
    async def swallowing(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except BaseException:
            await PlainTextResponse("swallowed")(scope, receive, send)

    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 20, "more_body": True}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(swallowing, max_body_size=10)(scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413

    with pytest.raises(ValueError):
        await RequestBodyLimitMiddleware(failing, max_body_size=10)(scope, receive, send)


@pytest.mark.anyio
async def test_disconnect_message_is_not_counted() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.disconnect"
        await PlainTextResponse("ok")(scope, receive, send)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(app, max_body_size=0)(scope, receive, send)
    assert sent[0]["status"] == 200


# Limits do not leak between layers


def streamed(size: int) -> Any:
    return iter([b"x" * size])


class Passthrough:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


class ReadBody(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        await request.body()
        return await call_next(request)  # type: ignore[no-any-return]


class RawReplay:
    """Reads the whole body, then replays it downstream."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replayed() -> Message:
            return replay.pop(0) if replay else {"type": "http.disconnect"}

        await self.app(scope, replayed, send)


@pytest.mark.parametrize("declared", [True, False])
def test_standalone_middleware_limit_is_final(test_client_factory: TestClientFactory, declared: bool) -> None:
    inner = Starlette(routes=[Route("/", echo_size, methods=["POST"], max_body_size=100)], max_body_size=100)
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert client.post("/", content=b"x" * 50 if declared else streamed(50)).status_code == 413
    nested = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(raw_app, max_body_size=100), max_body_size=10)
    content: Any = b"x" * 50 if declared else streamed(50)
    assert test_client_factory(nested).post("/", content=content).status_code == 413


@pytest.mark.parametrize("declared", [True, False])
def test_standalone_middleware_in_stack_is_not_raised_by_route(
    test_client_factory: TestClientFactory, declared: bool
) -> None:
    app = Starlette(
        routes=[Route("/", echo_size, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    content: Any = b"x" * 50 if declared else streamed(50)
    assert test_client_factory(app).post("/", content=content).status_code == 413


@pytest.mark.parametrize("declared", [True, False])
def test_inner_router_raises_limit_under_host_and_wrapped_mount(
    test_client_factory: TestClientFactory, declared: bool
) -> None:
    app = Starlette(
        routes=[
            Mount("/m", app=Passthrough(Router([Route("/", echo_size, methods=["POST"])], max_body_size=100))),
            Host("api.example.org", app=Router([Route("/", echo_size, methods=["POST"])], max_body_size=100)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app, base_url="http://api.example.org")
    for path in ("/", "/m/"):
        content: Any = b"x" * 50 if declared else streamed(50)
        assert client.post(path, content=content).status_code == 200


@pytest.mark.parametrize("middleware", [ReadBody, RawReplay])
@pytest.mark.parametrize("path", ["/small", "/m/"])
def test_inner_lowered_limit_applies_to_body_already_read(
    test_client_factory: TestClientFactory, middleware: Any, path: str
) -> None:
    app = Starlette(
        routes=[
            Route("/small", echo_size, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5),
        ],
        middleware=[Middleware(middleware)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post(path, content=b"x" * 50).status_code == 413
    assert client.post(path, content=streamed(50)).status_code == 413
    assert client.post(path, content=streamed(5)).status_code == 200


def test_huge_content_length_is_rejected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    headers = {"content-length": "9" * 5000}
    assert test_client_factory(app).post("/", headers=headers).status_code == 413
    standalone = RequestBodyLimitMiddleware(raw_app, max_body_size=10)
    assert test_client_factory(standalone).post("/", headers=headers).status_code == 413


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.5, "10", True, -1])
def test_non_integer_limits_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        Starlette(max_body_size=value)
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(raw_app, max_body_size=value)


@pytest.mark.anyio
async def test_streaming_response_completes_when_body_arrives_mid_response() -> None:
    started = anyio.Event()
    done = anyio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def stream() -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            started.set()
            for part in (b"a", b"b", b"c"):
                await anyio.sleep(0.01)
                await send({"type": "http.response.body", "body": part, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def listen(cancel_scope: anyio.CancelScope) -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            cancel_scope.cancel()  # pragma: no cover

        async with anyio.create_task_group() as tg:
            tg.start_soon(listen, tg.cancel_scope)
            await stream()
            tg.cancel_scope.cancel()

    sent: list[Message] = []

    async def receive() -> Message:
        await started.wait()
        if done.is_set():
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"x" * 20, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            done.set()

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    with anyio.fail_after(5):
        await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)
    assert b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body") == b"abc"


def test_error_raised_after_limit_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except BaseException as exc:
            raise RuntimeError("cleanup failed") from exc
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    response = test_client_factory(app).post("/", content=streamed(20))
    assert response.status_code == 413
    assert response.text == "Content Too Large"


class OtherAttr:
    def __init__(self, app: Any) -> None:
        self._inner = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._inner(scope, receive, send)


def closure_wrapper(app: Any) -> Any:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)

    return wrapped


@pytest.mark.parametrize("wrapper", [OtherAttr, closure_wrapper])
@pytest.mark.parametrize("declared", [True, False])
def test_inner_router_is_found_behind_any_wrapper(
    test_client_factory: TestClientFactory, wrapper: Any, declared: bool
) -> None:
    def router() -> Any:
        return wrapper(Router([Route("/", echo_size, methods=["POST"])], max_body_size=100))

    app = Starlette(
        routes=[Mount("/m", app=router()), Host("api.example.org", app=router())],
        max_body_size=10,
    )
    client = test_client_factory(app, base_url="http://api.example.org")
    for path in ("/m/", "/"):
        content: Any = b"x" * 50 if declared else streamed(50)
        assert client.post(path, content=content).status_code == 200
