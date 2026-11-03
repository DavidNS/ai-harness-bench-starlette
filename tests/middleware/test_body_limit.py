from __future__ import annotations

import tempfile
from collections.abc import Callable
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.endpoints import HTTPEndpoint
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Host, Match, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def read_body(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def run_asgi(
    app: Any,
    chunks: list[bytes],
    *,
    path: str = "/",
    method: str = "POST",
    content_length: int | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[list[Message], int]:
    """Drive an ASGI app directly; returns the sent messages and how many body chunks it consumed."""
    headers = [(b"host", b"testserver")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    for key, value in (extra_headers or {}).items():
        headers = [h for h in headers if h[0] != key.encode()]
        headers.append((key.encode(), value.encode()))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    consumed = 0

    async def receive() -> Message:
        nonlocal consumed
        if not pending:
            return {"type": "http.disconnect"}
        consumed += 1
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent, consumed


def status(sent: list[Message]) -> int:
    return int([m for m in sent if m["type"] == "http.response.start"][0]["status"])


def body(sent: list[Message]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


@pytest.mark.anyio
async def test_application_limit_counts_streamed_bytes_without_content_length() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent, consumed = await run_asgi(app, [b"123456", b"78901", b"never"])
    assert status(sent) == 413
    assert body(sent) == b"Content Too Large"
    assert consumed == 2
    sent, _ = await run_asgi(app, [b"12345", b"12345"])
    assert (status(sent), body(sent)) == (200, b"read 10")


@pytest.mark.anyio
async def test_understated_content_length_does_not_let_body_through() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent, _ = await run_asgi(app, [b"x" * 50], content_length=5)
    assert status(sent) == 413


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10))
    assert client.post("/", content=b"x" * 10).status_code == 200
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert response.headers["content-type"].startswith("text/plain")
    assert client.get("/missing").status_code == 404  # requests without a body are unaffected


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100).status_code == 200
    assert client.post("/m/", content=b"x" * 10).status_code == 200
    assert client.post("/m/", content=b"x" * 11).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=10)])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_inner_limits_raise_and_lower_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/raise", read_body, methods=["POST"], max_body_size=100),
            Route("/lower", read_body, methods=["POST"], max_body_size=5),
            Mount(
                "/m",
                routes=[
                    Route("/", read_body, methods=["POST"]),
                    Route("/lower", read_body, methods=["POST"], max_body_size=3),
                ],
                max_body_size=50,
            ),
            Mount("/r", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=200)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/raise", content=b"x" * 100).status_code == 200
    assert client.post("/raise", content=b"x" * 101).status_code == 413
    assert client.post("/lower", content=b"x" * 5).status_code == 200
    assert client.post("/lower", content=b"x" * 6).status_code == 413
    assert client.post("/m/", content=b"x" * 50).status_code == 200
    assert client.post("/m/", content=b"x" * 51).status_code == 413
    assert client.post("/m/lower", content=b"x" * 3).status_code == 200
    assert client.post("/m/lower", content=b"x" * 4).status_code == 413
    assert client.post("/r/", content=b"x" * 200).status_code == 200
    assert client.post("/r/", content=b"x" * 201).status_code == 413


@pytest.mark.anyio
async def test_content_length_fail_fast_waits_for_routing() -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Route("/small", read_body, methods=["POST"], max_body_size=5),
        ],
        max_body_size=10,
    )
    # Over the application limit but within the route's: allowed, and the body is read.
    sent, consumed = await run_asgi(app, [b"x" * 50], path="/big", content_length=50)
    assert status(sent) == 200
    # Over the matched limit: rejected without reading anything and without running the endpoint.
    for path, length in (("/", 11), ("/big", 101), ("/small", 6)):
        calls.clear()
        sent, consumed = await run_asgi(app, [b"x" * length], path=path, content_length=length)
        assert (status(sent), consumed, calls) == (413, 0, [])
    # No route and no method use the application's limit.
    sent, consumed = await run_asgi(app, [b"x" * 11], path="/nope", content_length=11)
    assert (status(sent), consumed) == (413, 0)
    sent, consumed = await run_asgi(app, [b"x" * 11], path="/big", method="PUT", content_length=11)
    assert (status(sent), consumed) == (413, 0)
    sent, consumed = await run_asgi(app, [b"x" * 11], path="/big", method="PUT", content_length=5)
    assert status(sent) == 405


@pytest.mark.anyio
async def test_content_length_fail_fast_in_mount_and_router() -> None:
    seen: list[str] = []

    async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append("ran")
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(
        routes=[Mount("/asgi", app=asgi_app), Mount("/big", app=asgi_app, max_body_size=100)],
        max_body_size=10,
    )
    sent, _ = await run_asgi(app, [b"x" * 11], path="/asgi/x", content_length=11)
    assert status(sent) == 413
    sent, _ = await run_asgi(app, [b"x" * 50], path="/big/x", content_length=50)
    assert status(sent) == 200
    # What is mounted may route again and raise the limit, so the check waits for its first read or send.
    assert seen == ["ran", "ran"]


@pytest.mark.anyio
async def test_invalid_content_length_is_ignored() -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])]), max_body_size=10)
    scope_headers = [(b"content-length", b"abc")]
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "root_path": "",
        "query_string": b"",
        "headers": scope_headers,
        "scheme": "http",
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    assert status(sent) == 200


async def plain_asgi(scope: Scope, receive: Receive, send: Send) -> None:
    calls.append("ran")
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    await PlainTextResponse(f"read {total}")(scope, receive, send)


@pytest.mark.anyio
async def test_standalone_middleware() -> None:
    app = RequestBodyLimitMiddleware(plain_asgi, max_body_size=10)
    sent, _ = await run_asgi(app, [b"x" * 5, b"x" * 5])
    assert (status(sent), body(sent)) == (200, b"read 10")
    sent, consumed = await run_asgi(app, [b"x" * 5, b"x" * 6, b"x"])
    assert (status(sent), body(sent), consumed) == (413, b"Content Too Large", 2)


@pytest.mark.anyio
async def test_standalone_middleware_rejects_content_length_on_entry() -> None:
    app = RequestBodyLimitMiddleware(plain_asgi, max_body_size=10)
    sent, consumed = await run_asgi(app, [b"x" * 11], content_length=11)
    assert (status(sent), consumed, calls) == (413, 0, [])
    # An understated header does not help.
    sent, _ = await run_asgi(app, [b"x" * 11], content_length=1)
    assert status(sent) == 413


@pytest.mark.anyio
async def test_standalone_middleware_ignores_other_scopes() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    app = RequestBodyLimitMiddleware(inner, max_body_size=1)
    for scope_type in ("websocket", "lifespan"):
        await app({"type": scope_type}, None, None)  # type: ignore[arg-type]
    assert seen == ["websocket", "lifespan"]


@pytest.mark.anyio
async def test_no_second_response_when_response_already_started() -> None:
    async def streaming(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hello", "more_body": True})
        while True:
            await receive()

    app = RequestBodyLimitMiddleware(streaming, max_body_size=3)
    sent, consumed = await run_asgi(app, [b"xx", b"xx", b"xx"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert status(sent) == 200
    assert consumed == 2


@pytest.mark.anyio
async def test_swallowed_exception_still_gets_413() -> None:
    async def swallowing(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while True:
                await receive()
        except Exception:
            pass
        await PlainTextResponse("fine")(scope, receive, send)

    app = RequestBodyLimitMiddleware(swallowing, max_body_size=3)
    sent, _ = await run_asgi(app, [b"xxxx"])
    assert (status(sent), body(sent)) == (413, b"Content Too Large")


@pytest.mark.anyio
async def test_starlette_streaming_endpoint_limit() -> None:
    async def stream(request: Request) -> Response:
        async def gen() -> Any:
            async for _ in request.stream():
                yield b"chunk"

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", stream, methods=["POST"])], max_body_size=3)
    sent, _ = await run_asgi(app, [b"xx", b"xx"])
    assert [m["type"] for m in sent].count("http.response.start") <= 1


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app) as client:  # runs lifespan
        with client.websocket_connect("/ws") as session:
            session.send_text("a long message")
            assert session.receive_text() == "a long message"


@pytest.mark.anyio
async def test_receive_keeps_failing_once_exceeded() -> None:
    results: list[str] = []

    async def retrying(scope: Scope, receive: Receive, send: Send) -> None:
        for _ in range(2):
            try:
                await receive()
            except BaseException as exc:
                results.append(type(exc).__name__)
        raise RuntimeError("done")

    app = RequestBodyLimitMiddleware(retrying, max_body_size=1)
    sent, _ = await run_asgi(app, [b"xx"])
    assert status(sent) == 413
    assert results == ["_BodyTooLarge", "_BodyTooLarge"]


@pytest.mark.anyio
async def test_unrelated_errors_propagate() -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await run_asgi(RequestBodyLimitMiddleware(broken, max_body_size=10), [b"x"])


@pytest.mark.anyio
async def test_lowering_the_limit_below_what_was_already_received() -> None:
    inner = RequestBodyLimitMiddleware(plain_asgi, max_body_size=3)

    async def reads_first(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await inner(scope, receive, send)

    app = RequestBodyLimitMiddleware(reads_first, max_body_size=100)
    sent, _ = await run_asgi(app, [b"x" * 5, b"x"])
    assert status(sent) == 413
    assert calls == []


@pytest.mark.anyio
async def test_oversized_content_length_with_huge_digits_and_redirect() -> None:
    app = Starlette(routes=[Route("/slash/", read_body, methods=["POST"])], max_body_size=10)
    sent, _ = await run_asgi(app, [b"x"], path="/slash", extra_headers={"content-length": "9" * 5000})
    assert status(sent) == 413
    sent, _ = await run_asgi(app, [b"x"], path="/slash")
    assert status(sent) == 307
    sent, _ = await run_asgi(app, [b"x" * 11], path="/slash", content_length=11)
    assert status(sent) == 413


@pytest.mark.anyio
async def test_disconnect_messages_pass_through() -> None:
    types: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        for _ in range(2):
            types.append((await receive())["type"])
        await PlainTextResponse("ok")(scope, receive, send)

    await run_asgi(RequestBodyLimitMiddleware(app, max_body_size=10), [b"x"])
    assert types == ["http.request", "http.disconnect"]


async def stream_body(request: Request) -> Response:
    async def gen() -> Any:
        async for _ in request.stream():
            yield b"chunk"

    return StreamingResponse(gen())


GZIP = [Middleware(GZipMiddleware)]


@pytest.mark.anyio
async def test_wrapped_router_under_mount_and_host_defers_content_length_check() -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    wrapped = CORSMiddleware(inner, allow_origins=["*"])
    sub = GZipMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)]))
    app = Starlette(
        routes=[
            Mount("/api", app=wrapped),
            Mount("/sub", app=sub),
            Host("api.example.org", app=wrapped),
        ],
        max_body_size=10,
    )
    for path in ("/api/", "/sub/"):
        sent, _ = await run_asgi(app, [b"x" * 50], path=path, content_length=50)
        assert status(sent) == 200
        sent, _ = await run_asgi(app, [b"x" * 50], path=path)
        assert status(sent) == 200
        sent, consumed = await run_asgi(app, [b"x" * 101], path=path, content_length=101)
        assert (status(sent), consumed) == (413, 0)
    sent, _ = await run_asgi(app, [b"x" * 50], content_length=50, extra_headers={"host": "api.example.org"})
    assert status(sent) == 200


@pytest.mark.anyio
async def test_custom_base_route_gets_content_length_fail_fast() -> None:
    class Custom(BaseRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            if scope["path"] == "/p":
                return Match.PARTIAL, {}
            return (Match.FULL, {}) if scope["path"] == "/c" else (Match.NONE, {})

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("ran")
            await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Custom()], max_body_size=10)
    sent, consumed = await run_asgi(app, [b"x" * 11], path="/c", content_length=11)
    assert (status(sent), consumed, calls) == (413, 0, [])
    sent, consumed = await run_asgi(app, [b"x" * 11], path="/p", content_length=11)
    assert (status(sent), consumed, calls) == (413, 0, [])


@pytest.mark.anyio
async def test_endpoint_without_method_uses_outer_limit() -> None:
    class Endpoint(HTTPEndpoint):
        async def post(self, request: Request) -> Response:
            return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", Endpoint, max_body_size=5)], max_body_size=100)
    sent, _ = await run_asgi(app, [b"x" * 50], method="PUT", content_length=50)
    assert status(sent) == 405
    sent, _ = await run_asgi(app, [b"x" * 50], method="POST", content_length=50)
    assert status(sent) == 413


@pytest.mark.anyio
async def test_limit_behind_buffering_middleware_still_gets_413() -> None:
    chunks = [b"xx", b"xx", b"xx"]
    cases = {
        "route": Starlette(routes=[Route("/", stream_body, methods=["POST"], max_body_size=3)], middleware=GZIP),
        "mount": Starlette(
            routes=[Mount("/", routes=[Route("/", stream_body, methods=["POST"])], max_body_size=3)], middleware=GZIP
        ),
        "router": Starlette(
            routes=[Mount("/", app=Router(routes=[Route("/", stream_body, methods=["POST"])], max_body_size=3))],
            middleware=GZIP,
        ),
        "standalone": Starlette(
            routes=[Route("/", stream_body, methods=["POST"])],
            middleware=GZIP + [Middleware(RequestBodyLimitMiddleware, max_body_size=3)],
        ),
        "app": Starlette(routes=[Route("/", stream_body, methods=["POST"])], middleware=GZIP, max_body_size=3),
    }
    for name, app in cases.items():
        sent, _ = await run_asgi(app, chunks)
        assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413], name
        assert b"Content Too Large" in body(sent), name


@pytest.mark.anyio
async def test_standalone_limit_is_a_ceiling_inner_layers_cannot_raise() -> None:
    nested = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(plain_asgi, max_body_size=100), max_body_size=10)
    starlette_inside = RequestBodyLimitMiddleware(
        Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)], max_body_size=1000),
        max_body_size=10,
    )
    as_user_middleware = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    for app in (nested, starlette_inside, as_user_middleware):
        for content_length in (None, 50):
            sent, _ = await run_asgi(app, [b"x" * 25, b"x" * 25], content_length=content_length)
            assert status(sent) == 413
        sent, _ = await run_asgi(app, [b"x" * 10])
        assert status(sent) == 200
    # Inner layers can still lower it.
    lowered = RequestBodyLimitMiddleware(
        Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=3)]), max_body_size=10
    )
    sent, _ = await run_asgi(lowered, [b"x" * 4])
    assert status(sent) == 413


def closure_middleware(inner: Any) -> Any:
    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        await inner(scope, receive, send)

    return middleware


class PrivateAttrMiddleware:
    def __init__(self, application: Any) -> None:
        self._application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._application(scope, receive, send)


@pytest.mark.anyio
async def test_any_wrapper_around_a_router_defers_content_length_check() -> None:
    def router() -> Router:
        return Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])

    app = Starlette(
        routes=[
            Mount("/closure", app=closure_middleware(router())),
            Mount("/private", app=PrivateAttrMiddleware(Starlette(routes=router().routes))),
            Host("api.example.org", app=closure_middleware(router())),
        ],
        max_body_size=10,
    )
    for path, headers in (("/closure/", None), ("/private/", None), ("/", {"host": "api.example.org"})):
        sent, _ = await run_asgi(app, [b"x" * 50], path=path, content_length=50, extra_headers=headers)
        assert status(sent) == 200
        sent, _ = await run_asgi(app, [b"x" * 101], path=path, content_length=101, extra_headers=headers)
        assert status(sent) == 413


@pytest.mark.anyio
async def test_mounted_opaque_app_gets_content_length_check_on_first_use() -> None:
    async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        calls.append("read")  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=asgi_app)], max_body_size=10)
    sent, consumed = await run_asgi(app, [b"x" * 11], path="/m/", content_length=11)
    assert (status(sent), consumed, calls) == (413, 0, [])


@pytest.mark.anyio
async def test_standalone_middleware_never_sends_a_second_response_once_the_app_started_one() -> None:
    # Outside Starlette nothing tracks what reaches the server: a response start the app sent counts as started.
    app = GZipMiddleware(RequestBodyLimitMiddleware(stream_asgi, max_body_size=3), minimum_size=0)
    sent, _ = await run_asgi(app, [b"xx", b"xx", b"xx"])
    assert [m for m in sent if m["type"] == "http.response.start"] == []


@pytest.mark.anyio
async def test_router_limit_behind_gzip_sends_413() -> None:
    router = Router(routes=[Route("/", stream_body, methods=["POST"])], max_body_size=3)
    sent, _ = await run_asgi(GZipMiddleware(router), [b"xx", b"xx", b"xx"])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


@pytest.mark.anyio
async def test_limit_below_base_http_middleware_sends_413() -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=3)], middleware=[Middleware(Passthrough)]
    )
    sent, _ = await run_asgi(app, [b"xx", b"xx", b"xx"])
    assert (status(sent), body(sent)) == (413, b"Content Too Large")


@pytest.mark.anyio
async def test_limit_does_not_reach_the_error_handler() -> None:
    called: list[str] = []

    async def on_error(request: Request, exc: Exception) -> Response:
        called.append(type(exc).__name__)  # pragma: no cover
        return PlainTextResponse("err", status_code=500)  # pragma: no cover

    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], exception_handlers={500: on_error})
    sent, _ = await run_asgi(RequestBodyLimitMiddleware(inner, max_body_size=10), [b"x" * 25, b"x" * 25])
    assert status(sent) == 413
    assert called == []


async def stream_asgi(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    while True:
        await receive()


@pytest.mark.anyio
async def test_held_response_start_is_flushed_when_the_app_returns() -> None:
    async def start_only(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})

    sent, _ = await run_asgi(RequestBodyLimitMiddleware(start_only, max_body_size=3), [b"x"])
    assert [m["type"] for m in sent] == ["http.response.start"]


@pytest.mark.anyio
async def test_limit_below_base_http_middleware_below_gzip_sends_413() -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", stream_body, methods=["POST"], max_body_size=3)],
        middleware=[Middleware(GZipMiddleware, minimum_size=0), Middleware(Passthrough)],
    )
    sent, _ = await run_asgi(app, [b"xx", b"xx", b"xx"])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert b"Content Too Large" in body(sent)


@pytest.mark.anyio
async def test_held_start_is_flushed_in_starlette_when_the_app_returns() -> None:
    async def no_content(request: Request) -> Response:
        return Response(status_code=204)

    async def start_only(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})

    app = Starlette(routes=[Mount("/m", app=start_only)], max_body_size=3)
    sent, _ = await run_asgi(app, [b"x"], path="/m/")
    assert [m["type"] for m in sent] == ["http.response.start"]
    app = Starlette(routes=[Route("/", no_content, methods=["POST"])], max_body_size=3)
    sent, _ = await run_asgi(app, [b"x"])
    assert status(sent) == 204


@pytest.mark.anyio
async def test_response_start_of_a_request_without_body_is_not_held_back() -> None:
    seen: list[bool] = []
    sent: list[Message] = []

    async def events(request: Request) -> Response:
        async def gen() -> Any:
            seen.append(any(m["type"] == "http.response.start" for m in sent))
            yield b"data: hi\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    for app in (
        Starlette(routes=[Route("/", events)], max_body_size=10),
        Starlette(routes=[Route("/", events, max_body_size=10)]),
        Starlette(routes=[Mount("/", routes=[Route("/", events)], max_body_size=10)]),
    ):
        sent.clear()
        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }
        await app(scope, receive, send)
    assert seen == [True, True, True]


@pytest.mark.anyio
async def test_app_limit_below_base_http_middleware_streaming_endpoint_gets_413() -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", stream_body, methods=["POST"])], middleware=[Middleware(Passthrough)], max_body_size=3
    )
    sent, _ = await run_asgi(app, [b"xx", b"xx", b"xx"])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


@pytest.mark.anyio
async def test_started_response_is_not_replaced_and_gets_no_second_response() -> None:
    async def sloppy(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await receive()
        except BaseException:
            pass
        await send({"type": "http.response.body", "body": b"late"})

    app = Starlette(routes=[Mount("/m", app=sloppy)], max_body_size=3)
    sent, _ = await run_asgi(app, [b"xx", b"xx"], path="/m/")
    assert [m["type"] for m in sent].count("http.response.start") == 1
    assert (status(sent), body(sent)) == (200, b"late")


@pytest.mark.anyio
async def test_response_start_is_never_held_back_for_a_streamed_body() -> None:
    # An echo that starts its response, then waits for more request body, must not deadlock on the start.
    app_sent: list[Message] = []
    released = anyio.Event()

    async def echo(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await released.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    app = Starlette(routes=[Mount("/m", app=echo)], max_body_size=100)

    async def receive() -> Message:
        await anyio.sleep_forever()  # pragma: no cover
        raise AssertionError  # pragma: no cover

    async def send(message: Message) -> None:
        app_sent.append(message)
        if message["type"] == "http.response.start":
            released.set()

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/m/",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"transfer-encoding", b"chunked")],
        "scheme": "http",
        "server": ("testserver", 80),
    }
    with anyio.fail_after(2):
        await app(scope, receive, send)
    assert [m["type"] for m in app_sent] == ["http.response.start", "http.response.body"]


@pytest.mark.anyio
async def test_oversized_body_without_length_headers_behind_gzip_and_base_http_middleware_gets_413() -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    for method in ("POST", "DELETE"):
        app = Starlette(
            routes=[Route("/", stream_body, methods=[method], max_body_size=3)],
            middleware=[Middleware(GZipMiddleware, minimum_size=0), Middleware(Passthrough)],
        )
        sent, _ = await run_asgi(app, [b"xx", b"xx", b"xx"], method=method)
        assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413], method
        assert b"Content Too Large" in body(sent) or sent[-1]["type"] == "http.response.body"


@pytest.mark.anyio
async def test_app_that_sent_nothing_gets_413_from_the_root() -> None:
    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            await receive()

    app = Starlette(routes=[Mount("/m", app=silent)], middleware=[Middleware(GZipMiddleware)], max_body_size=3)
    sent, _ = await run_asgi(app, [b"xx", b"xx"], path="/m/")
    assert (status(sent), body(sent)) == (413, b"Content Too Large")


def test_multipart_counts_file_data_and_overhead(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> Response:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["f"].read())))  # type: ignore[union-attr]

    client = test_client_factory(Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=1000))
    assert client.post("/", files={"f": ("a.txt", b"x" * 100)}).text == "100"
    # The file itself is under the limit, but with the multipart overhead the body is not.
    response = client.post("/", files={"f": ("a.txt", b"x" * 990)})
    assert (response.status_code, response.text) == (413, "Content Too Large")


@pytest.mark.anyio
async def test_limit_below_gzip_below_base_http_middleware_sends_413() -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", stream_body, methods=["POST"], max_body_size=3)],
        middleware=[Middleware(Passthrough), Middleware(GZipMiddleware, minimum_size=0)],
    )
    sent, _ = await run_asgi(app, [b"xx", b"xx", b"xx"])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


@pytest.mark.anyio
async def test_multipart_closes_temp_files_when_limit_is_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original: Callable[..., Any] = tempfile.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)

    async def upload(request: Request) -> Response:
        async with request.form():
            return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=500)
    boundary = "xyz"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n'.encode()
    # No Content-Length, so the limit is only hit while the parser is streaming the file.
    chunks = [head + b"x" * 300, b"x" * 300, b"x" * 300, f"\r\n--{boundary}--\r\n".encode()]
    scope_headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    sent, _ = await run_asgi(app, chunks, extra_headers=scope_headers)
    assert (status(sent), body(sent)) == (413, b"Content Too Large")
    assert len(created) == 1
    assert created[0].closed
