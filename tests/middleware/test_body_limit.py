from __future__ import annotations

import io
import tempfile
from typing import Any

import pytest

from starlette import formparsers
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def read_body(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"{len(body)}")


async def read_form(request: Request) -> Response:
    calls.append(request.url.path)
    async with request.form() as form:
        return JSONResponse(
            {key: (value if isinstance(value, str) else len(await value.read())) for key, value in form.multi_items()}
        )


def post(client: TestClient, path: str = "/", size: int = 10, **kwargs: Any) -> Any:
    return client.post(path, content=b"x" * size, **kwargs)


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = post(client, size=10)
    assert (response.status_code, response.text) == (200, "10")
    response = post(client, size=11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert response.headers["content-type"].startswith("text/plain")


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert post(test_client_factory(app), size=100_000).text == "100000"


def test_get_without_body(test_client_factory: TestClientFactory) -> None:
    async def homepage(request: Request) -> Response:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", homepage)], max_body_size=0)
    assert test_client_factory(app).get("/").text == "ok"


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks(sizes: list[int]) -> Any:
        for size in sizes:
            yield b"x" * size

    assert client.post("/", content=chunks([5, 5])).text == "10"
    assert client.post("/", content=chunks([5, 6])).status_code == 413


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = post(client, size=50, headers={"content-length": "5"})
    assert response.status_code == 413
    response = post(client, size=50, headers={"content-length": "garbage"})
    assert response.status_code == 413


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert post(client, size=10).status_code == 200
    assert post(client, size=11).status_code == 413
    assert calls == ["/"]


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert post(client, "/", size=100).status_code == 200
    assert post(client, "/m/", size=10).status_code == 200
    assert post(client, "/m/", size=11).status_code == 413
    assert post(client, "/m/", size=11, headers={"content-length": "1"}).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/limited", read_body, methods=["POST"], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert post(client, "/", size=100).status_code == 200
    assert post(client, "/limited", size=10).status_code == 200
    assert post(client, "/limited", size=11).status_code == 413


@pytest.mark.parametrize("kind", ["route", "mount", "router"])
def test_inner_limit_raises_outer(test_client_factory: TestClientFactory, kind: str) -> None:
    route = Route("/", read_body, methods=["POST"])
    inner: Any
    if kind == "route":
        inner = Route("/", read_body, methods=["POST"], max_body_size=100)
        outer_routes = [inner]
        path = "/"
    elif kind == "mount":
        outer_routes = [Mount("/m", routes=[route], max_body_size=100)]
        path = "/m/"
    else:
        outer_routes = [Mount("/m", app=Router(routes=[route], max_body_size=100))]
        path = "/m/"
    client = test_client_factory(Starlette(routes=outer_routes, max_body_size=10))
    assert post(client, path, size=100).text == "100"
    assert post(client, path, size=100, headers={"content-length": "100"}).status_code == 200
    assert post(client, path, size=101).status_code == 413
    assert calls == [path, path]


@pytest.mark.parametrize("kind", ["route", "mount", "router"])
def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory, kind: str) -> None:
    route = Route("/", read_body, methods=["POST"])
    if kind == "route":
        outer_routes: Any = [Route("/", read_body, methods=["POST"], max_body_size=10)]
        path = "/"
    elif kind == "mount":
        outer_routes = [Mount("/m", routes=[route], max_body_size=10)]
        path = "/m/"
    else:
        outer_routes = [Mount("/m", app=Router(routes=[route], max_body_size=10))]
        path = "/m/"
    client = test_client_factory(Starlette(routes=outer_routes, max_body_size=100))
    assert post(client, path, size=10).status_code == 200
    assert post(client, path, size=11).status_code == 413
    assert post(client, path, size=11, headers={"content-length": "11"}).status_code == 413
    assert calls == [path]


def test_inner_limit_without_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=10)])
    client = test_client_factory(app)
    assert post(client, size=10).status_code == 200
    assert post(client, size=11).status_code == 413


def test_content_length_fail_fast_waits_for_routing(test_client_factory: TestClientFactory) -> None:
    seen: list[str] = []

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            seen.append("middleware")
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/big", read_body, methods=["POST"], max_body_size=1000),
            Route("/plain", read_body, methods=["POST"]),
            Route("/get", read_body, methods=["GET"]),
            Mount("/asgi", PlainTextResponse("mounted")),
        ],
        middleware=[Middleware(Spy)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # The declared length is over the app limit, but the matched route raises it.
    assert post(client, "/big", size=100).status_code == 200
    assert calls == ["/big"]
    # Same declared length, no route limit: rejected before the endpoint runs.
    assert post(client, "/plain", size=100).status_code == 413
    # No method, no route.
    assert post(client, "/get", size=100).status_code == 413
    assert post(client, "/missing", size=100).status_code == 413
    assert post(client, "/plain/", size=100).status_code == 413  # redirect
    assert post(client, "/asgi/x", size=100).status_code == 413
    assert calls == ["/big"]
    assert post(client, "/get", size=5).status_code == 405
    assert post(client, "/missing", size=5).status_code == 404
    # A mount to a plain ASGI app is the end of the routing.
    assert post(client, "/asgi/x", size=5).text == "mounted"


def test_fail_fast_does_not_read_body() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        raise AssertionError("must not be called")

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=10)
    sent: list[Message] = []

    async def receive() -> Message:  # pragma: no cover
        raise AssertionError("body must not be read")

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    scope = {"type": "http", "method": "POST", "headers": [(b"content-length", b"11")]}
    anyio.run(wrapped, scope, receive, send)
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


def test_standalone_middleware_plain_asgi(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert post(client, size=10).text == "10"
    assert post(client, size=11).status_code == 413
    assert post(client, size=50, headers={"content-length": "1"}).status_code == 413


def test_standalone_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert post(client, size=10).status_code == 200
    assert post(client, size=11).status_code == 413
    assert calls == ["/"]


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    started: list[bool] = []

    async def lifespan(app: Starlette) -> Any:
        started.append(True)
        yield

    from contextlib import asynccontextmanager

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1, lifespan=asynccontextmanager(lifespan))
    with test_client_factory(app) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("a long message")
            assert session.receive_text() == "a long message"
    assert started == [True]


def test_with_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert post(client, size=10).status_code == 200
    assert post(client, size=11).status_code == 413


def test_endpoint_cannot_swallow_limit(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:  # pragma: no cover
            pass  # pragma: no cover
        return PlainTextResponse("swallowed")  # pragma: no cover

    async def endpoint_error(request: Request) -> Response:
        try:
            await request.body()
        except BaseException:
            return PlainTextResponse("swallowed")
        return PlainTextResponse("fine")  # pragma: no cover

    app = Starlette(
        routes=[
            Route("/a", endpoint, methods=["POST"]),
            Route("/b", endpoint_error, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert post(client, "/a", size=11, headers={"content-length": "1"}).status_code == 413
    # Even if the application answers anyway, the client gets the 413.
    response = post(client, "/b", size=11, headers={"content-length": "1"})
    assert (response.status_code, response.text) == (413, "Content Too Large")


def test_response_already_started() -> None:
    import anyio

    events: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            events.append(message["type"])
            if message["type"] != "http.request":
                break
        await send({"type": "http.response.body", "body": b"early"})

    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
        ]
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), scope, receive, send)
    assert events == ["http.request", "http.disconnect"]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    small = {"file": ("a.txt", io.BytesIO(b"x" * 100))}
    assert client.post("/", files=small, data={"a": "b"}).json() == {"file": 100, "a": "b"}
    # The file data alone fits, the multipart overhead does not.
    edge = {"file": ("a.txt", io.BytesIO(b"x" * 990))}
    assert client.post("/", files=edge).status_code == 413
    big = {"file": ("a.txt", io.BytesIO(b"x" * 5000))}
    assert client.post("/", files=big).status_code == 413


def test_urlencoded_form(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=20)
    client = test_client_factory(app)
    assert client.post("/", data={"a": "b"}).json() == {"a": "b"}
    assert client.post("/", data={"a": "b" * 50}).status_code == 413


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=3000)
    body = b"".join(
        b'--b\r\nContent-Disposition: form-data; name="%s"; filename="%s.txt"\r\n\r\n%s\r\n' % (name, name, b"x" * 1500)
        for name in (b"a", b"b", b"c")
    )
    # Delivered in small pieces, so the files are open when the limit is crossed.
    messages: list[Message] = [
        {"type": "http.request", "body": body[i : i + 1000], "more_body": True} for i in range(0, len(body), 1000)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(created) >= 2
    assert all(file.closed for file in created)
    assert calls == ["/"]


@pytest.mark.anyio
async def test_multipart_parser_closes_files_on_any_interruption() -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    body = b"--b\r\nContent-Disposition: form-data; name=f; filename=f.txt\r\n\r\n" + b"x" * 100

    async def stream() -> Any:
        yield body
        raise KeyboardInterrupt

    parser = MultiPartParser(Headers({"Content-Type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(KeyboardInterrupt):
        await parser.parse()
    assert parser._files_to_close_on_error
    assert all(file.closed for file in parser._files_to_close_on_error)


def test_inner_limit_below_what_was_already_read(test_client_factory: TestClientFactory) -> None:
    class ReadBody:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await Request(scope, receive).body()
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=5)],
        middleware=[Middleware(ReadBody)],
        max_body_size=100,
    )
    response = post(test_client_factory(app), size=10)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert calls == []


def test_streaming_response_started_before_limit_is_judged() -> None:
    import anyio

    from starlette.responses import StreamingResponse

    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"hi"
            async for _ in request.stream():
                pass  # pragma: no cover
            yield b"end"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=25)
    messages: list[Message] = [{"type": "http.request", "body": b"x" * 10, "more_body": True} for _ in range(5)]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "raw_path": b"/", "query_string": b"", "headers": []}
    anyio.run(app, scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


class Passthrough:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def chunks(count: int) -> Any:
    for _ in range(count):
        yield b"x" * 10


def run_asgi(app: Any, headers: list[tuple[bytes, bytes]], body_chunks: list[bytes]) -> tuple[list[Message], Any]:
    import anyio

    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(body_chunks) - 1}
        for i, chunk in enumerate(body_chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
    }
    error = None
    try:
        anyio.run(app, scope, receive, send)
    except BaseException as exc:  # pragma: no cover
        error = exc
    return sent, error


@pytest.mark.parametrize("wrap", [Passthrough, lambda app: Passthrough(Passthrough(app))])
def test_mount_and_host_of_wrapped_router_use_matched_limit(test_client_factory: TestClientFactory, wrap: Any) -> None:
    from starlette.routing import Host

    for outer in (
        lambda app: Mount("/m", app=app),
        lambda app: Host("testserver", app=app),
    ):
        inner = Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
        app = Starlette(routes=[outer(wrap(inner))], max_body_size=10)
        client = test_client_factory(app)
        path = "/m/" if outer(inner).__class__ is Mount else "/"
        assert client.post(path, content=chunks(5)).text == "50"
        assert client.post(path, content=b"x" * 50).text == "50"
        assert client.post(path, content=b"x" * 101).status_code == 413


def test_mount_of_wrapped_router_limit_lowered(test_client_factory: TestClientFactory) -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/m", app=Passthrough(inner))], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 50).text == "50"


def test_mount_of_wrapped_starlette_app(test_client_factory: TestClientFactory) -> None:
    sub = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    app = Starlette(routes=[Mount("/m", app=Passthrough(sub))], max_body_size=10)
    assert test_client_factory(app).post("/m/", content=b"x" * 50).text == "50"


def test_standalone_limit_is_not_raised_from_inside(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert client.post("/", content=b"x" * 50).status_code == 413
    assert client.post("/", content=chunks(5)).status_code == 413


def test_stacked_standalone_middlewares(test_client_factory: TestClientFactory) -> None:
    inner = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])]), max_body_size=1000)
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert client.post("/", content=b"x" * 50).status_code == 413
    assert client.post("/", content=chunks(5)).status_code == 413
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=2000))
    assert client.post("/", content=chunks(5)).text == "50"
    assert client.post("/", content=b"x" * 1001).status_code == 413


def test_standalone_middleware_and_route_limit_both_apply(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=chunks(5)).status_code == 413
    assert client.post("/", content=b"x" * 50).status_code == 413


def test_is_disconnected_is_not_slowed_down() -> None:
    import anyio

    async def endpoint(request: Request) -> Response:
        polls = 0
        while not await request.is_disconnected():
            polls += 1  # pragma: no cover
            if polls > 50:  # pragma: no cover
                return PlainTextResponse("never noticed")
            await anyio.sleep(0)  # pragma: no cover
        return PlainTextResponse("noticed")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    sent, error = run_asgi(app, [], [])
    assert error is None
    assert sent[1]["body"] == b"noticed"


def test_exception_groups_and_other_errors_still_give_413() -> None:
    import anyio

    async def task_group_reader(scope: Scope, receive: Receive, send: Send) -> None:
        async def drain() -> None:
            while (await receive()).get("more_body"):
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    async def reraiser(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while (await receive()).get("more_body"):
                pass
        except BaseException as exc:
            raise RuntimeError("wrapped") from exc
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    for inner in (task_group_reader, reraiser):
        sent, error = run_asgi(RequestBodyLimitMiddleware(inner, max_body_size=10), [], [b"x" * 6] * 3)
        assert error is None
        assert sent[0]["status"] == 413

    async def endpoint(request: Request) -> Response:
        async def drain() -> None:
            await request.body()

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    sent, error = run_asgi(app, [], [b"x" * 6] * 3)
    assert error is None
    assert sent[0]["status"] == 413


def test_error_handlers_do_not_see_the_limit(test_client_factory: TestClientFactory) -> None:
    seen: list[str] = []

    async def on_error(request: Request, exc: Exception) -> Response:
        seen.append(type(exc).__name__)  # pragma: no cover
        return PlainTextResponse("boom", status_code=500)  # pragma: no cover

    class Catching:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            try:
                await self.app(scope, receive, send)
            except Exception:  # pragma: no cover
                seen.append("middleware")

    class Dispatch(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        exception_handlers={Exception: on_error},
        middleware=[Middleware(Catching), Middleware(Dispatch)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).status_code == 413
    assert client.post("/", content=b"x" * 50, headers={"content-length": "1"}).status_code == 413
    assert seen == []


@pytest.mark.parametrize("app_kind", ["middleware", "starlette"])
def test_huge_content_length(app_kind: str) -> None:
    if app_kind == "middleware":
        app: Any = RequestBodyLimitMiddleware(PlainTextResponse("ok"), max_body_size=10)
    else:
        app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    for value in (b"9" * 5000, b"0" * 5000 + b"5"):
        sent, error = run_asgi(app, [(b"content-length", value)], [b"x" * 5])
        assert error is None
        expected = 413 if value[0:1] == b"9" else 200
        assert sent[0]["status"] == expected


def test_standalone_middleware_ignores_other_scopes() -> None:
    import anyio

    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=0)
    anyio.run(wrapped, {"type": "websocket"}, None, None)  # type: ignore[arg-type]
    assert seen == ["websocket"]


def test_receive_after_the_limit_is_still_cut_off() -> None:
    reads = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reads
        try:
            while True:
                await receive()
                reads += 1  # pragma: no branch
        except BaseException:
            with pytest.raises(BaseException):
                await receive()
            raise

    sent, error = run_asgi(RequestBodyLimitMiddleware(app, max_body_size=10), [], [b"x" * 6] * 3)
    assert error is None
    assert sent[0]["status"] == 413
    assert reads == 1


def closure_wrapped(app: Any) -> Any:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)

    return asgi


class PrivateAttribute:
    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)  # pragma: no cover


@pytest.mark.parametrize("raising", ["route", "router"])
def test_declared_length_waits_for_routing_behind_visible_wrappers(
    test_client_factory: TestClientFactory, raising: str
) -> None:
    from starlette.routing import Host

    for outer, path in ((lambda app: Mount("/m", app=app), "/m/"), (lambda app: Host("testserver", app=app), "/")):
        if raising == "route":
            inner = Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
        else:
            inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
        client = test_client_factory(Starlette(routes=[outer(Passthrough(Passthrough(inner)))], max_body_size=10))
        assert client.post(path, content=chunks(5)).text == "50"
        assert client.post(path, content=b"x" * 50).text == "50"
        assert client.post(path, content=b"x" * 101).status_code == 413
        calls.clear()


def test_hidden_router_raises_counted_bodies_and_mount_limit_raises_declared_ones(
    test_client_factory: TestClientFactory,
) -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(
        routes=[
            Mount("/hidden", app=closure_wrapped(inner)),
            Mount("/supported", app=closure_wrapped(inner), max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # A router the mount cannot see: its limit applies to counted bodies only.
    assert client.post("/hidden/", content=chunks(5)).text == "50"
    assert client.post("/hidden/", content=b"x" * 50).status_code == 413
    # The limit on the mount itself is the supported way over an opaque wrapper.
    assert client.post("/supported/", content=b"x" * 50).text == "50"


def test_mount_with_routes_and_middleware_sees_its_router(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount(
                "/m",
                routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
                middleware=[Middleware(Passthrough)],
            )
        ],
        max_body_size=10,
    )
    assert test_client_factory(app).post("/m/", content=b"x" * 50).text == "50"


def test_plain_apps_under_mount_and_host_are_rejected_on_entry(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    for plain in (asgi, closure_wrapped(PrivateAttribute(asgi)), PrivateAttribute(asgi)):
        for route, path in ((Mount("/asgi", app=plain), "/asgi/x"), (Host("testserver", app=plain), "/")):
            client = test_client_factory(Starlette(routes=[route], max_body_size=10))
            assert client.post(path, content=b"x" * 50).status_code == 413
    assert calls == []
    client = test_client_factory(Starlette(routes=[Mount("/asgi", app=asgi)], max_body_size=10))
    assert client.post("/asgi/x", content=b"x" * 5).text == "ok"


class NotARouter:
    """Looks like it wraps a router, but answers the request itself."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("answered")
        await PlainTextResponse("answered")(scope, receive, send)


def test_visible_router_that_does_not_settle_the_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Mount("/n", app=NotARouter(Router(routes=[])))], max_body_size=10)
    client = test_client_factory(app)
    # The check could not be made at the mount, so it is made when the app answers.
    assert client.post("/n/", content=b"x" * 50).status_code == 413
    assert client.post("/n/", content=b"x" * 5).text == "answered"


def test_mounted_app_with_hostile_attributes_is_served(test_client_factory: TestClientFactory) -> None:
    class Config:
        def __getattr__(self, name: str) -> Any:
            raise KeyError(name)  # pragma: no cover

    config = Config()

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse(str(config is not None))(scope, receive, send)

    from starlette.routing import Host

    for route, path in ((Mount("/m", app=asgi), "/m/"), (Host("testserver", app=asgi), "/")):
        assert test_client_factory(Starlette(routes=[route])).get(path).text == "True"


def test_polling_sees_the_end_after_an_oversized_body() -> None:
    async def endpoint(request: Request) -> Response:
        polls = 0
        while not await request.is_disconnected():
            polls += 1
            if polls > 50:
                return PlainTextResponse("never noticed")  # pragma: no cover
        return PlainTextResponse("gone")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    sent, error = run_asgi(app, [], [b"x" * 6] * 3)
    assert error is None
    assert sent[0]["status"] == 413


def test_errors_after_the_response_started_reach_the_server() -> None:
    class AppBug(Exception):
        pass

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive()).get("more_body"):
            pass  # pragma: no cover
        raise AppBug("failed")

    sent, error = run_asgi(RequestBodyLimitMiddleware(app, max_body_size=10), [], [b"x" * 6] * 3)
    assert isinstance(error, AppBug)
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_limit_signal_in_a_task_group_after_the_response_started() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

        async def drain() -> None:
            await Request(scope, receive).body()

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)

    sent, error = run_asgi(RequestBodyLimitMiddleware(app, max_body_size=10), [], [b"x" * 6] * 3)
    assert error is None
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_mounted_app_without_instance_dict(test_client_factory: TestClientFactory) -> None:
    class Slotted:
        __slots__ = ()

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await PlainTextResponse("slotted")(scope, receive, send)

    app = Starlette(routes=[Mount("/s", app=Slotted())], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/s/", content=b"x" * 5).text == "slotted"
    assert client.post("/s/", content=b"x" * 50).status_code == 413


def test_wrapper_whose_app_is_a_plain_function_is_the_end_of_routing(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=Passthrough(Passthrough(asgi)))], max_body_size=10)
    assert test_client_factory(app).post("/m/", content=b"x" * 50).status_code == 413
    assert calls == []
