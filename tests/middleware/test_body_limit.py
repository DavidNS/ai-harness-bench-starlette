from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Host, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def read_body(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


def chunks(*sizes: int) -> Iterator[bytes]:
    for size in sizes:
        yield b"x" * size


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    assert_too_large(client.post("/", content=b"x" * 11))


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).text == "read 100000"


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(4, 4)).text == "read 8"
    assert_too_large(client.post("/", content=chunks(4, 4, 4)))


def test_json_is_limited(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        return PlainTextResponse(str(await request.json()))  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    assert_too_large(test_client_factory(app).post("/", json={"key": "x" * 50}))


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).text == "read 5"
    assert_too_large(client.post("/", content=b"x" * 6))
    assert_too_large(client.post("/", content=chunks(3, 3)))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/free", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/free", content=b"x" * 100).text == "read 100"
    assert client.post("/m/", content=b"x" * 5).text == "read 5"
    assert_too_large(client.post("/m/", content=b"x" * 6))
    assert_too_large(client.post("/m/", content=chunks(3, 3)))


def test_mount_with_foreign_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=asgi, max_body_size=5)])
    assert_too_large(test_client_factory(app).post("/m/", content=b"x" * 6))
    assert calls == []


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 5).text == "read 5"
    assert_too_large(client.post("/small", content=b"x" * 6))
    assert_too_large(client.post("/small", content=chunks(3, 3)))
    assert client.post("/other", content=b"x" * 100).text == "read 100"


def test_standalone_route(test_client_factory: TestClientFactory) -> None:
    route = Route("/", read_body, methods=["POST"], max_body_size=5)
    assert_too_large(test_client_factory(route).post("/", content=b"x" * 6))


@pytest.mark.parametrize("size, status", [(8, 200), (9, 413), (30, 413)])
def test_inner_route_lowers_application_limit(test_client_factory: TestClientFactory, size: int, status: int) -> None:
    app = Starlette(
        routes=[
            Route("/low", read_body, methods=["POST"], max_body_size=8),
            Route("/default", read_body, methods=["POST"]),
        ],
        max_body_size=20,
    )
    client = test_client_factory(app)
    assert client.post("/low", content=b"x" * size).status_code == status
    assert client.post("/low", content=chunks(size)).status_code == status
    assert client.post("/default", content=b"x" * 20).status_code == 200
    assert client.post("/default", content=b"x" * 21).status_code == 413


@pytest.mark.parametrize("size, status", [(30, 200), (50, 200), (51, 413)])
def test_inner_route_raises_application_limit(test_client_factory: TestClientFactory, size: int, status: int) -> None:
    app = Starlette(
        routes=[
            Route("/big", read_body, methods=["POST"], max_body_size=50),
            Route("/default", read_body, methods=["POST"]),
        ],
        max_body_size=20,
    )
    client = test_client_factory(app)
    assert client.post("/big", content=b"x" * size).status_code == status
    assert client.post("/big", content=chunks(size)).status_code == status
    assert_too_large(client.post("/default", content=b"x" * 30))


def test_inner_mount_and_router_override(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount(
                "/up",
                routes=[Route("/", read_body, methods=["POST"])],
                max_body_size=50,
            ),
            Mount(
                "/down",
                app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            ),
            Mount(
                "/route",
                app=Router(
                    routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
                    max_body_size=5,
                ),
            ),
        ],
        max_body_size=20,
    )
    client = test_client_factory(app)
    assert client.post("/up/", content=b"x" * 40).status_code == 200
    assert client.post("/up/", content=b"x" * 51).status_code == 413
    assert client.post("/down/", content=b"x" * 6).status_code == 413
    assert client.post("/down/", content=b"x" * 5).status_code == 200
    # A route inside a limited router can still raise the router's limit.
    assert client.post("/route/", content=b"x" * 60).status_code == 200
    assert client.post("/route/", content=b"x" * 101).status_code == 413


def test_nested_application(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)
    app = Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/inner/", content=b"x" * 40).status_code == 200
    assert client.post("/inner/", content=b"x" * 51).status_code == 413
    assert calls == ["/inner/"]


def test_lowering_after_body_already_read(test_client_factory: TestClientFactory) -> None:
    class ReadFirst:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await Request(scope, receive).body()
            await self.app(scope, receive, send)

    async def endpoint(request: Request) -> Response:
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"], max_body_size=5)],
        middleware=[Middleware(ReadFirst)],
        max_body_size=100,
    )
    assert_too_large(test_client_factory(app).post("/", content=b"x" * 10))
    assert calls == []


def test_middleware_reading_body_is_limited(test_client_factory: TestClientFactory) -> None:
    class ReadBody(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            response: Response = await call_next(request)
            return response

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(ReadBody)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=chunks(6, 6)))
    assert client.post("/", content=b"x" * 10).status_code == 200


def test_body_limit_error_is_not_a_server_error(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=1)
    client = test_client_factory(app, raise_server_exceptions=True)
    assert_too_large(client.post("/", content=chunks(2)))


def test_content_length_fail_fast_in_route(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    assert_too_large(test_client_factory(app).post("/", content=b"x" * 11))
    assert calls == []


def test_content_length_fail_fast_waits_for_inner_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"], max_body_size=50)], max_body_size=5),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 30).status_code == 200
    assert_too_large(client.post("/m/", content=b"x" * 60))
    assert calls == ["/m/"]


def test_content_length_fail_fast_before_route_middleware(
    test_client_factory: TestClientFactory,
) -> None:
    seen: list[str] = []

    class Spy:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            seen.append("spy")  # pragma: no cover
            await self.app(scope, receive, send)  # pragma: no cover

    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"], middleware=[Middleware(Spy)]),
            Route("/own", read_body, methods=["POST"], middleware=[Middleware(Spy)], max_body_size=3),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/own", content=b"x" * 4))
    assert seen == []


def test_content_length_fail_fast_on_no_match_and_wrong_method(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/missing", content=b"x" * 11))
    assert_too_large(client.put("/", content=b"x" * 11))
    # Under the limit, the usual responses are untouched.
    assert client.post("/missing", content=b"x").status_code == 404
    assert client.put("/", content=b"x").status_code == 405
    # The route's own limit does not apply to a method it does not handle.
    assert_too_large(client.put("/", content=b"x" * 50))


def test_content_length_fail_fast_no_match_uses_inner_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Mount("/m", routes=[], max_body_size=3)], max_body_size=100)
    client = test_client_factory(app)
    # No route matches, so the application's limit applies, not the mount's.
    assert client.post("/m/missing", content=b"x" * 4).status_code == 404
    assert client.post("/other", content=b"x" * 4).status_code == 404
    assert_too_large(client.post("/other", content=b"x" * 101))


def test_no_method_match_inside_mount_uses_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=3)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.put("/m/", content=b"x" * 50).status_code == 405
    assert_too_large(client.put("/m/", content=b"x" * 101))


def test_content_length_fail_fast_does_not_run_custom_not_found(test_client_factory: TestClientFactory) -> None:
    async def not_found(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("default")  # pragma: no cover

    app = Router(default=not_found, max_body_size=2)
    assert_too_large(test_client_factory(app).post("/x", content=b"xxx"))
    assert calls == []


async def run_asgi(
    app: Any, headers: list[tuple[bytes, bytes]], body_chunks: list[bytes], delay: float = 0
) -> list[Message]:
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    pending = [{"type": "http.request", "body": c, "more_body": True} for c in body_chunks]
    pending.append({"type": "http.request", "body": b"", "more_body": False})
    sent: list[Message] = []
    received: list[Message] = []

    async def receive() -> Message:
        if delay:
            await anyio.sleep(delay)
        message = pending.pop(0) if pending else {"type": "http.disconnect"}
        received.append(message)
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    run_asgi.received = received  # type: ignore[attr-defined]
    return sent


@pytest.mark.anyio
async def test_absent_or_understated_content_length() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        calls.append("app")
        await read_body(request)
        await PlainTextResponse("done")(scope, receive, send)  # pragma: no cover

    app_limited = RequestBodyLimitMiddleware(app, max_body_size=10)
    for headers in ([], [(b"content-length", b"2")]):
        calls.clear()
        sent = await run_asgi(app_limited, headers, [b"x" * 6, b"x" * 6])
        assert sent[0]["status"] == 413
        assert sent[1]["body"] == b"Content Too Large"
        assert len(sent) == 2


@pytest.mark.anyio
async def test_stream_stops_after_limit() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        async for _ in request.stream():
            pass

    sent = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [], [b"x" * 3, b"x" * 3, b"x" * 3])
    assert sent[0]["status"] == 413
    # The third chunk was never pulled from the server.
    assert len(run_asgi.received) == 2  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_no_second_response_when_response_already_started() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        async for _ in Request(scope, receive).stream():
            pass
        await send({"type": "http.response.body", "body": b"", "more_body": False})  # pragma: no cover

    sent = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [], [b"x" * 3, b"x" * 3])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_app_that_swallows_the_error_still_gets_a_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await Request(scope, receive).body()
        except Exception:
            pass

    sent = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [], [b"x" * 6])
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_standalone_middleware_rejects_on_entry() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("app")  # pragma: no cover

    sent = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [(b"content-length", b"6")], [])
    assert sent[0]["status"] == 413
    assert calls == []
    assert run_asgi.received == []  # type: ignore[attr-defined]


def test_standalone_middleware_with_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(6, 6)))
    assert client.get("/").text == "0"


def test_middleware_in_starlette_stack(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(6, 6)))


def test_requests_without_body_websockets_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    started: list[str] = []

    async def lifespan(app: Starlette) -> Any:
        started.append("up")
        yield

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    async def hello(request: Request) -> Response:
        return PlainTextResponse("hello")

    from contextlib import asynccontextmanager

    app = Starlette(
        routes=[Route("/", hello), WebSocketRoute("/ws", ws)],
        lifespan=asynccontextmanager(lifespan),
        max_body_size=0,
    )
    with test_client_factory(app) as client:
        assert started == ["up"]
        assert client.get("/").text == "hello"
        with client.websocket_connect("/ws") as session:
            session.send_text("x" * 100)
            assert session.receive_text() == "x" * 100


@pytest.mark.anyio
async def test_malformed_content_length_is_ignored() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    sent = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [(b"content-length", b"abc")], [])
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_other_errors_are_not_mistaken_for_the_limit() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [], [])


@pytest.mark.anyio
async def test_disconnect_messages_pass_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.request"
        assert (await receive())["type"] == "http.disconnect"
        await PlainTextResponse("ok")(scope, receive, send)

    sent = await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=5), [], [])
    assert sent[0]["status"] == 200


class EmptyGroupError(Exception):
    exceptions: list[Exception] = []


def test_error_with_empty_exceptions_attribute_is_a_server_error(test_client_factory: TestClientFactory) -> None:
    async def boom(request: Request) -> Response:
        raise EmptyGroupError()

    async def handler(request: Request, exc: Exception) -> Response:
        return PlainTextResponse("handled", status_code=500)

    routes = [Route("/", boom, methods=["POST"])]
    for limit in (None, 100):
        app = Starlette(routes=routes, exception_handlers={Exception: handler}, max_body_size=limit)
        response = test_client_factory(app, raise_server_exceptions=False).post("/")
        assert (response.status_code, response.text) == (500, "handled")

    app = Starlette(routes=routes, max_body_size=100)
    with pytest.raises(EmptyGroupError):
        test_client_factory(app).post("/")


def test_standalone_middleware_limit_cannot_be_raised_from_below(test_client_factory: TestClientFactory) -> None:
    inner_route = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    inner_app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    in_stack = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    for app in (
        RequestBodyLimitMiddleware(inner_route, max_body_size=10),
        RequestBodyLimitMiddleware(inner_app, max_body_size=10),
        in_stack,
    ):
        client = test_client_factory(app)
        assert_too_large(client.post("/", content=b"x" * 50))
        assert_too_large(client.post("/", content=chunks(25, 25)))
        assert client.post("/", content=chunks(5, 5)).text == "read 10"


def test_route_can_still_raise_application_limit_below_middleware_in_stack(
    test_client_factory: TestClientFactory,
) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=50)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=100)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=chunks(25, 25)).text == "read 50"
    assert_too_large(client.post("/", content=chunks(30, 30)))


def test_content_length_waits_for_wrapped_inner_app(test_client_factory: TestClientFactory) -> None:
    class Passthrough:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.app(scope, receive, send)

    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/inner", app=Passthrough(inner))], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/inner/", content=b"x" * 50).text == "read 50"
    assert client.post("/inner/", content=chunks(50)).text == "read 50"
    assert_too_large(client.post("/inner/", content=b"x" * 101))


@pytest.mark.anyio
async def test_limit_crossed_before_response_start_gets_413() -> None:
    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            await anyio.sleep(0.05)
            yield b"data"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("t", 80),
        "client": ("c", 1),
    }
    pending: list[Message] = [
        {"type": "http.request", "body": b"xxx", "more_body": True},
        {"type": "http.request", "body": b"xxx", "more_body": True},
    ]
    done = anyio.Event()
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        await done.wait()  # pragma: no cover
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()

    with anyio.fail_after(5):
        await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


@pytest.mark.anyio
async def test_truncated_response_is_not_completed_through_base_http_middleware() -> None:
    class Pass(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            response: Response = await call_next(request)
            return response

    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"a"
            await request.body()
            yield b"b"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], middleware=[Middleware(Pass)], max_body_size=5)
    sent = await run_asgi(app, [(b"transfer-encoding", b"chunked")], [b"xxx", b"xxx"], 0.01)
    assert not any(m["type"] == "http.response.body" and not m.get("more_body", False) for m in sent)


def test_limits_below_and_above_middleware_in_stack(test_client_factory: TestClientFactory) -> None:
    # The lowest of the middleware's cap and the tree's limit applies.
    app = Starlette(
        routes=[
            Route("/low", read_body, methods=["POST"], max_body_size=5),
            Route("/high", read_body, methods=["POST"], max_body_size=1000),
            Route("/default", read_body, methods=["POST"]),
        ],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=100)],
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/low", content=chunks(50)))
    assert_too_large(client.post("/low", content=b"x" * 50))
    assert_too_large(client.post("/high", content=chunks(50, 60)))
    assert_too_large(client.post("/high", content=b"x" * 110))
    assert client.post("/high", content=chunks(50)).text == "read 50"
    assert client.post("/default", content=b"x" * 100).text == "read 100"


def test_application_limit_with_middleware_fail_fast(test_client_factory: TestClientFactory) -> None:
    async def ignore_body(request: Request) -> Response:
        calls.append("endpoint")  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(
        routes=[
            Route("/", ignore_body, methods=["POST"]),
            Route("/own", ignore_body, methods=["POST"], max_body_size=10),
        ],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=50)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 30))
    assert_too_large(client.post("/own", content=b"x" * 30))
    assert calls == []


def test_application_limit_below_wrapping_middleware(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=100))
    assert_too_large(client.post("/", content=chunks(50)))
    assert_too_large(client.post("/", content=b"x" * 50))
    assert client.post("/", content=b"x" * 5).text == "read 5"


def test_content_length_fail_fast_with_mounted_middleware(test_client_factory: TestClientFactory) -> None:
    async def ignore_body(request: Request) -> Response:
        calls.append("endpoint")  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    mounted = RequestBodyLimitMiddleware(Router(routes=[Route("/", ignore_body, methods=["POST"])]), 100)
    app = Starlette(routes=[Mount("/m", app=mounted)], max_body_size=5)
    assert_too_large(test_client_factory(app).post("/m/", content=b"x" * 50))
    assert calls == []


def test_host_with_foreign_app_fail_fast(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover

    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(
        routes=[Host("foreign", app=asgi), Host("starlette", app=inner)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("http://foreign/", content=b"x" * 11))
    assert calls == []
    assert client.post("http://starlette/", content=b"x" * 50).text == "read 50"
