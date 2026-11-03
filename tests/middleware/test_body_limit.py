from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_size(request: Request) -> Response:
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def ran(request: Request) -> Response:
    return PlainTextResponse("ran")


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/", content=b"x" * 11))
    assert client.get("/").status_code == 405


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_size, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks() -> Any:
        yield b"x" * 6
        yield b"x" * 6

    assert_too_large(client.post("/", content=chunks()))


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 50, headers={"content-length": "5"}))


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).json() == {"size": 5}
    assert_too_large(client.post("/", content=b"x" * 6))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_size, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_size, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/m/", content=b"x" * 5).json() == {"size": 5}
    assert_too_large(client.post("/m/", content=b"x" * 6))


def test_mount_of_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    called = False

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        response = await read_size(Request(scope, receive))
        await response(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Mount("/m", asgi, max_body_size=5)]))
    assert_too_large(client.post("/m/", content=b"x" * 6))
    assert not called
    assert client.post("/m/", content=b"x" * 5).json() == {"size": 5}


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"], max_body_size=5)])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).json() == {"size": 5}
    assert_too_large(client.post("/", content=b"x" * 6))


def test_inner_limits_override_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_size, methods=["POST"], max_body_size=100),
            Route("/lower", read_size, methods=["POST"], max_body_size=3),
            Route("/inherit", read_size, methods=["POST"]),
            Mount(
                "/mount",
                routes=[
                    Route("/raise", read_size, methods=["POST"], max_body_size=100),
                    Route("/", read_size, methods=["POST"]),
                ],
                max_body_size=20,
            ),
            Mount("/router", app=Router([Route("/", read_size, methods=["POST"])], max_body_size=50)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raise", content=b"x" * 60).json() == {"size": 60}
    assert_too_large(client.post("/raise", content=b"x" * 101))
    assert_too_large(client.post("/lower", content=b"x" * 4))
    assert client.post("/lower", content=b"x" * 3).json() == {"size": 3}
    assert client.post("/inherit", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/inherit", content=b"x" * 11))
    assert client.post("/mount/", content=b"x" * 20).json() == {"size": 20}
    assert_too_large(client.post("/mount/", content=b"x" * 21))
    assert client.post("/mount/raise", content=b"x" * 60).json() == {"size": 60}
    assert client.post("/router/", content=b"x" * 50).json() == {"size": 50}
    assert_too_large(client.post("/router/", content=b"x" * 51))


def test_content_length_fail_fast_waits_for_routing(test_client_factory: TestClientFactory) -> None:
    calls: list[str] = []

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("middleware")
            await self.app(scope, receive, send)

    async def endpoint(request: Request) -> Response:
        calls.append("endpoint")
        return PlainTextResponse("ran")

    app = Starlette(
        routes=[
            Route("/raised", endpoint, methods=["POST"], max_body_size=1000),
            Route("/plain", endpoint, methods=["POST"]),
            Route("/mw", endpoint, methods=["POST"], middleware=[Middleware(Spy)]),
        ],
        middleware=[Middleware(Spy)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    body = b"x" * 100

    # An inner route may raise the limit, so the declared length is not rejected up front.
    assert client.post("/raised", content=body).text == "ran"
    calls.clear()

    assert_too_large(client.post("/plain", content=body))
    assert calls == ["middleware"]
    calls.clear()

    # Rejected before the route middleware and endpoint run.
    assert_too_large(client.post("/mw", content=body))
    assert calls == ["middleware"]
    calls.clear()

    # No route and no method fall back to the application's limit.
    assert_too_large(client.post("/missing", content=body))
    assert_too_large(client.request("GET", "/plain", content=body))
    assert client.post("/missing", content=b"x").status_code == 404
    assert client.get("/plain").status_code == 405


def test_route_limit_not_used_for_wrong_method(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", ran, methods=["POST"], max_body_size=1000)], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.request("GET", "/", content=b"x" * 100))


def test_nested_no_match_uses_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/ok", read_size, methods=["POST"])], max_body_size=100)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/m/nope", content=b"x" * 50))
    assert_too_large(client.request("GET", "/m/ok", content=b"x" * 50))
    assert client.post("/m/ok", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/m/nope", content=b"x" * 10).status_code == 404


def test_middleware_fail_fast_on_entry() -> None:
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert_too_large(client.post("/", content=b"x" * 11))
    assert not called
    assert client.post("/", content=b"x" * 10).text == "ok"
    assert called


def test_middleware_with_plain_asgi_app() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await PlainTextResponse(str(len(body)))(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(app, max_body_size=10))

    def chunks() -> Any:
        yield b"x" * 6
        yield b"x" * 6

    assert_too_large(client.post("/", content=chunks()))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.get("/").text == "0"


def test_middleware_inside_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_size, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}


def test_app_swallowing_the_error_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:
            return PlainTextResponse("swallowed")
        return PlainTextResponse("fine")  # pragma: no cover

    client = test_client_factory(Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5))

    def chunks() -> Any:
        yield b"x" * 6

    assert_too_large(client.post("/", content=chunks()))


def test_error_handlers_not_involved(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app, raise_server_exceptions=True)

    def chunks() -> Any:
        yield b"x" * 6

    assert_too_large(client.post("/", content=chunks()))


@pytest.mark.anyio
async def test_response_already_started() -> None:
    sent: list[Message] = []
    errors: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"early", "more_body": True})
        try:
            while True:
                await receive()
        except Exception as exc:
            errors.append(type(exc).__name__)
            raise

    messages = [
        {"type": "http.request", "body": b"x" * 6, "more_body": True},
        {"type": "http.request", "body": b"x" * 6, "more_body": False},
    ]

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)
    assert errors == ["RequestBodyTooLarge"]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert 413 not in [m.get("status") for m in sent]


@pytest.mark.anyio
async def test_stream_stopped_after_limit() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        for _ in range(3):
            try:
                await receive()
            except Exception as exc:
                seen.append(type(exc).__name__)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 6, "more_body": True}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)
    assert seen == ["RequestBodyTooLarge", "RequestBodyTooLarge"]
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


def test_streaming_response_unaffected(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"a"
            yield b"b"

        return StreamingResponse(gen())

    client = test_client_factory(Starlette(routes=[Route("/", endpoint)], max_body_size=1))
    assert client.get("/").text == "ab"


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    started = False

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        nonlocal started
        started = True
        yield

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], lifespan=lifespan, max_body_size=1)
    with test_client_factory(app) as client:
        assert started
        with client.websocket_connect("/ws") as session:
            session.send_text("x" * 100)
            assert session.receive_text() == "x" * 100


def test_multipart_limit(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            return JSONResponse({"names": sorted(form.keys())})

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    small = {"file": ("a.txt", b"x" * 100)}
    assert client.post("/", files=small).json() == {"names": ["file"]}
    # The file data and multipart framing count towards the limit.
    assert_too_large(client.post("/", files={"file": ("a.txt", b"x" * 990)}))
    assert_too_large(client.post("/", data={"a": "b" * 2000}))


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)

    async def endpoint(request: Request) -> Response:
        await request.form()
        return PlainTextResponse("no")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=500)
    boundary = "b"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="f.bin"\r\n\r\n'.encode()
    chunks = [head + b"x" * 100, b"x" * 100, b"x" * 600]
    sent: list[Message] = []

    async def receive() -> Message:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert created[0].closed


@pytest.mark.anyio
async def test_multipart_parser_closes_files_on_any_interruption() -> None:
    head = b'--b\r\nContent-Disposition: form-data; name="f"; filename="f.bin"\r\n\r\n'

    async def stream() -> Any:
        yield head + b"data"
        raise RuntimeError("interrupted")

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RuntimeError):
        await parser.parse()
    assert len(parser._files_to_close_on_error) == 1
    assert parser._files_to_close_on_error[0].closed


def test_route_in_isolation() -> None:
    route = Route("/", read_size, methods=["POST"], max_body_size=5)
    client = TestClient(route)
    assert_too_large(client.post("/", content=b"x" * 6))
    assert client.post("/", content=b"x" * 5).json() == {"size": 5}


calls: list[str] = []


class Spy:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("spy")
        await self.app(scope, receive, send)


def test_unmatched_under_limited_mount_uses_unlimited_application(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Mount("/m", routes=[Route("/ok", ran, methods=["POST"])], max_body_size=10)])
    client = test_client_factory(app)
    assert client.post("/m/nope", content=b"x" * 50).status_code == 404
    assert client.put("/m/ok", content=b"x" * 50).status_code == 405
    assert_too_large(client.post("/m/ok", content=b"x" * 50))


def test_nested_mounts_unmatched_uses_unlimited_application(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount(
                "/a",
                routes=[Mount("/b", routes=[Route("/ok", ran, methods=["POST"])], max_body_size=100)],
                max_body_size=10,
            )
        ]
    )
    client = test_client_factory(app)
    assert client.post("/a/b/nope", content=b"x" * 50).status_code == 404
    assert client.post("/a/b/ok", content=b"x" * 50).text == "ran"


def test_subclasses_without_super_init(test_client_factory: TestClientFactory) -> None:
    class App(Starlette):
        def __init__(self) -> None:
            self.router = Router([Route("/", ran)])
            self.middleware_stack = self.router

    class SelfRoute(Route):
        def __init__(self) -> None:
            self.app = ran_app
            self.methods = None

        def matches(self, scope: Scope) -> Any:
            from starlette.routing import Match

            return Match.FULL, {}

    class SelfRouter(Router):
        def __init__(self) -> None:
            self.routes = [Route("/", ran)]
            self.middleware_stack = self.app

    async def ran_app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ran")(scope, receive, send)

    assert test_client_factory(App()).get("/").text == "ran"
    assert test_client_factory(Starlette(routes=[SelfRoute()])).get("/").text == "ran"
    assert test_client_factory(SelfRouter()).get("/").text == "ran"


def test_mount_and_router_middleware_do_not_run_for_declared_over_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", ran, methods=["POST"])], middleware=[Middleware(Spy)]),
            Mount("/r", app=Router([Route("/", ran, methods=["POST"])], middleware=[Middleware(Spy)])),
            Mount("/ok", routes=[Route("/", ran, methods=["POST"], max_body_size=1000)], middleware=[Middleware(Spy)]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    calls.clear()
    assert_too_large(client.post("/m/", content=b"x" * 100))
    assert_too_large(client.post("/r/", content=b"x" * 100))
    assert calls == []
    assert client.post("/ok/", content=b"x" * 100).text == "ran"
    assert calls == ["spy"]


def test_mount_of_wrapped_router_inner_route_raises_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = GZipMiddleware(Router([Route("/", read_size, methods=["POST"], max_body_size=1000)]))
    client = test_client_factory(Starlette(routes=[Mount("/x", app=inner)], max_body_size=10))
    assert client.post("/x/", content=b"x" * 100).json() == {"size": 100}


@pytest.mark.parametrize("value", [-1, "10", 1.5, True])
def test_invalid_limit_rejected(value: Any) -> None:
    for factory in (
        lambda: Starlette(max_body_size=value),
        lambda: Router(max_body_size=value),
        lambda: Mount("/", routes=[], max_body_size=value),
        lambda: Route("/", ran, max_body_size=value),
        lambda: RequestBodyLimitMiddleware(Router(), max_body_size=value),
    ):
        with pytest.raises((TypeError, ValueError)):
            factory()


def make_server(chunks: list[bytes], *, send_delay: float = 0) -> tuple[Receive, Send, list[Message]]:
    import anyio

    sent: list[Message] = []
    pending = list(chunks)

    async def receive() -> Message:
        if not pending:
            return {"type": "http.disconnect"}  # pragma: no cover
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        if send_delay:
            await anyio.sleep(send_delay)
        sent.append(message)

    return receive, send, sent


def complete_413(sent: list[Message]) -> bool:
    return [m["type"] for m in sent] == ["http.response.start", "http.response.body"] and sent[0]["status"] == 413


SCOPE: Scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}


@pytest.mark.anyio
async def test_413_survives_cancelled_scope_of_the_app() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        with anyio.move_on_after(0.05):
            await Request(scope, receive).body()
        await PlainTextResponse("late")(scope, receive, send)

    receive, send, sent = make_server([b"x" * 6, b"x" * 6], send_delay=0.2)
    await RequestBodyLimitMiddleware(app, max_body_size=10)(dict(SCOPE), receive, send)
    assert complete_413(sent)


@pytest.mark.anyio
async def test_concurrent_readers_get_one_413() -> None:
    import anyio

    async def reader(receive: Receive) -> None:
        try:
            await receive()
        except Exception:
            pass

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader, receive)
            tg.start_soon(reader, receive)

    receive, send, sent = make_server([b"x" * 6, b"x" * 6, b"x" * 6], send_delay=0.05)
    await RequestBodyLimitMiddleware(app, max_body_size=5)(dict(SCOPE), receive, send)
    assert complete_413(sent)


@pytest.mark.anyio
async def test_error_in_task_group_does_not_escape() -> None:
    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(Request(scope, receive).body)

    receive, send, sent = make_server([b"x" * 6, b"x" * 6])
    await RequestBodyLimitMiddleware(app, max_body_size=5)(dict(SCOPE), receive, send)
    assert complete_413(sent)


def test_base_http_middleware_over_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            response: Response = await call_next(request)
            return response

    app = Starlette(
        routes=[Route("/", read_size, methods=["POST"])], middleware=[Middleware(Passthrough)], max_body_size=5
    )

    def chunks() -> Any:
        yield b"x" * 3
        yield b"x" * 3

    assert_too_large(test_client_factory(app).post("/", content=chunks()))


@pytest.mark.anyio
async def test_stream_stopped_for_started_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await receive()
        except Exception:
            pass
        await send({"type": "http.response.body", "body": b"after", "more_body": False})

    receive, send, sent = make_server([b"x" * 6, b"x" * 6])
    await RequestBodyLimitMiddleware(app, max_body_size=10)(dict(SCOPE), receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_endpoint_reraising_other_error_after_413(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.json()
        except Exception as exc:
            raise ValueError("bad payload") from exc
        return PlainTextResponse("ok")  # pragma: no cover

    client = test_client_factory(Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5))

    def chunks() -> Any:
        yield b"x" * 6

    assert_too_large(client.post("/", content=chunks()))


def test_starlette_docstring_kept() -> None:
    assert Starlette.__doc__ == "Creates an Starlette application."


def _buffering_app() -> Starlette:
    from starlette.middleware.base import BaseHTTPMiddleware

    class Buffer(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            response: Response = await call_next(request)
            return response

    async def plain(scope: Scope, receive: Receive, send: Send) -> None:
        response = await read_size(Request(scope, receive))
        await response(scope, receive, send)

    return Starlette(
        routes=[
            Mount("/m", plain, max_body_size=10),
            Route("/r", read_size, methods=["POST"], max_body_size=10),
            Route("/plain", read_size, methods=["POST"]),
        ],
        middleware=[Middleware(Buffer)],
        max_body_size=1000,
    )


def test_limit_lowered_after_body_buffered_before_routing(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(_buffering_app())

    def chunks() -> Any:
        yield b"x" * 100
        yield b"x" * 100

    assert_too_large(client.post("/m/", content=chunks()))
    assert_too_large(client.post("/r", content=chunks()))
    assert client.post("/plain", content=chunks()).json() == {"size": 200}


def test_declared_over_limit_with_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            response: Response = await call_next(request)
            return response

    app = Starlette(
        routes=[Route("/", read_size, methods=["POST"])], middleware=[Middleware(Passthrough)], max_body_size=10
    )
    assert_too_large(test_client_factory(app).post("/", content=b"x" * 100))


@pytest.mark.anyio
async def test_cancellation_not_held_off_by_slow_client() -> None:
    import time

    import anyio

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()

    receive, send, _ = make_server([b"x" * 6, b"x" * 6], send_delay=3)
    start = time.monotonic()
    with anyio.move_on_after(0.1):
        await RequestBodyLimitMiddleware(app, max_body_size=10)(dict(SCOPE), receive, send)
    assert time.monotonic() - start < 1.5


@pytest.mark.anyio
async def test_slow_client_gets_complete_413_without_cancellation() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()

    receive, send, sent = make_server([b"x" * 6, b"x" * 6], send_delay=0.5)
    await RequestBodyLimitMiddleware(app, max_body_size=10)(dict(SCOPE), receive, send)
    assert complete_413(sent)


@pytest.mark.anyio
async def test_is_disconnected_poll_slow_client_gets_complete_413() -> None:
    async def endpoint(request: Request) -> Response:
        for _ in range(5):
            await request.is_disconnected()
        return PlainTextResponse("done")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    receive, send, sent = make_server([b"x" * 6, b"x" * 6, b"x" * 6], send_delay=0.5)
    await app(dict(SCOPE), receive, send)
    assert complete_413(sent)


@pytest.mark.anyio
async def test_413_sent_when_limit_crossed_not_when_app_finishes() -> None:
    import time

    import anyio

    from starlette.background import BackgroundTask

    async def slow() -> None:
        await anyio.sleep(1)

    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:
            pass

        async def events() -> Any:
            while True:  # pragma: no branch
                yield b"data: tick\n\n"
                await anyio.sleep(0.01)

        if request.query_params.get("stream"):
            return StreamingResponse(events(), media_type="text/event-stream")
        await anyio.sleep(0.5)
        return PlainTextResponse("ok", background=BackgroundTask(slow))

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    for query in (b"", b"stream=1"):
        receive, send, sent = make_server([b"x" * 6, b"x" * 6])
        times: list[float] = []
        start = time.monotonic()

        async def timed_send(message: Message) -> None:
            times.append(time.monotonic() - start)
            await send(message)

        with anyio.move_on_after(0.3):
            await app({**SCOPE, "query_string": query}, receive, timed_send)
        assert complete_413(sent)
        assert times[0] < 0.2
