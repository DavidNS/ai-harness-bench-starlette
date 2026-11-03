from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, Router, WebSocketRoute
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

called: list[str] = []


async def echo_len(request: Request) -> Response:
    called.append(request.url.path)
    return JSONResponse({"size": len(await request.body())})


async def no_read(request: Request) -> Response:
    called.append(request.url.path)
    return PlainTextResponse("ok")


async def form(request: Request) -> Response:
    async with request.form() as f:
        return JSONResponse({"keys": list(f.keys())})


@pytest.fixture(autouse=True)
def reset_called() -> None:
    called.clear()


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", echo_len, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_invalid_limit() -> None:
    with pytest.raises(ValueError):
        Starlette(max_body_size=-1)
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(Starlette(), max_body_size=-1)


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def gen() -> Iterator[bytes]:
        yield b"x" * 6
        yield b"x" * 6

    assert client.post("/", content=gen()).status_code == 413
    assert client.post("/", content=b"x" * 11, headers={"content-length": "1"}).status_code == 413


def test_get_without_body_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", no_read)], max_body_size=0)
    assert test_client_factory(app).get("/").status_code == 200


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", echo_len, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"12345").status_code == 200
    assert client.post("/", content=b"123456").status_code == 413


def test_route_limit_without_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"], max_body_size=5)])
    client = test_client_factory(app)
    assert client.post("/", content=b"12345").status_code == 200
    assert client.post("/", content=b"123456").status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", echo_len, methods=["POST"])], max_body_size=5),
            Route("/free", echo_len, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"12345").status_code == 200
    assert client.post("/m/", content=b"123456").status_code == 413
    assert client.post("/free", content=b"x" * 1000).status_code == 200


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", echo_len, methods=["POST"], max_body_size=100),
            Route("/lowered", echo_len, methods=["POST"], max_body_size=3),
            Route("/default", echo_len, methods=["POST"]),
            Mount("/mount", routes=[Route("/", echo_len, methods=["POST"])], max_body_size=100),
            Mount("/router", app=Router([Route("/", echo_len, methods=["POST"])], max_body_size=100)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    body = b"x" * 50
    assert client.post("/raised", content=body).status_code == 200
    assert client.post("/raised", content=b"x" * 101).status_code == 413
    assert client.post("/lowered", content=b"x" * 4).status_code == 413
    assert client.post("/lowered", content=b"x" * 3).status_code == 200
    assert client.post("/default", content=body).status_code == 413
    assert client.post("/mount/", content=body).status_code == 200
    assert client.post("/router/", content=body).status_code == 200
    assert client.post("/router/", content=b"x" * 101).status_code == 413


def test_content_length_fail_fast(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", no_read, methods=["POST"], max_body_size=100),
            Route("/default", no_read, methods=["POST"]),
            Mount("/m", routes=[Route("/", no_read, methods=["POST"], max_body_size=100)]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    big = {"content-length": "50"}
    # Rejected without reading the body and without running the endpoint.
    assert client.post("/default", headers=big).status_code == 413
    assert client.post("/nothing", headers=big).status_code == 413
    assert client.get("/default", headers=big).status_code == 413  # no method matches
    assert client.post("/default/", headers=big).status_code == 413  # redirect slash
    assert called == []
    # An inner route may raise the limit, so the declared length is judged against it.
    assert client.post("/raised", headers={"content-length": "50"}, content=b"x" * 50).status_code == 200
    assert client.post("/m/", headers={"content-length": "50"}, content=b"x" * 50).status_code == 200
    assert client.post("/raised", headers={"content-length": "500"}).status_code == 413
    assert called == ["/raised", "/m/"]


def test_standalone_middleware(test_client_factory: TestClientFactory) -> None:
    seen: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(1)
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=b"x" * 11).status_code == 413
    seen.clear()
    response = client.post("/", content=b"x", headers={"content-length": "11"})
    assert response.status_code == 413 and response.text == "Content Too Large"
    assert seen == []  # rejected on entry, app never ran


def test_standalone_middleware_over_starlette_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"], max_body_size=100)])
    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 50).status_code == 413


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    events: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi", "more_body": True})
        while True:
            message = await receive()
            events.append(message["type"])
            if message["type"] == "http.disconnect" or not message.get("more_body"):
                break
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=5))
    response = client.post("/", content=iter([b"x" * 4, b"x" * 4, b"x" * 4]))
    assert response.status_code == 200
    assert response.text == "hi"
    assert events[-1] == "http.disconnect"


@pytest.mark.anyio
async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestBodyLimitMiddleware(app, max_body_size=0)

    async def receive() -> Message:  # pragma: no cover
        raise AssertionError

    async def send(message: Message) -> None:  # pragma: no cover
        raise AssertionError

    await middleware({"type": "lifespan"}, receive, send)
    await middleware({"type": "websocket"}, receive, send)
    assert seen == ["lifespan", "websocket"]


def test_websocket_unaffected(test_client_factory: TestClientFactory) -> None:
    async def ws(websocket):  # type: ignore[no-untyped-def]
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=0)
    with test_client_factory(app).websocket_connect("/ws") as session:
        session.send_text("hello")
        assert session.receive_text() == "hello"


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", form, methods=["POST"])], max_body_size=300)
    client = test_client_factory(app)
    assert client.post("/", files={"f": ("a.txt", b"x" * 10)}).status_code == 200
    # The file data alone is under the limit; with the multipart overhead it is not.
    assert client.post("/", files={"f": ("a.txt", b"x" * 290)}).status_code == 413


def test_form_urlencoded(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", form, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", data={"a": "1"}).status_code == 200
    assert client.post("/", data={"a": "x" * 20}).status_code == 413


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = MultiPartParser.on_headers_finished

    def spy(self: MultiPartParser) -> None:
        original(self)
        created.extend(self._files_to_close_on_error)

    monkeypatch.setattr(MultiPartParser, "on_headers_finished", spy)
    app = Starlette(routes=[Route("/", form, methods=["POST"])], max_body_size=2000)
    # Several body messages and no Content-Length: the limit is crossed in the middle of the parse.
    body = b'--b\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\n' + b"x" * 5000 + b"\r\n--b--\r\n"
    messages: list[Message] = [
        {"type": "http.request", "body": body[:1000], "more_body": True},
        {"type": "http.request", "body": body[1000:3000], "more_body": True},
        {"type": "http.request", "body": body[3000:], "more_body": False},
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
        "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert created and all(f.closed for f in created)


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_any_interruption() -> None:
    from starlette.datastructures import Headers

    async def stream() -> AsyncGenerator[bytes, None]:
        yield (b'--b\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\n' + b"data")
        raise ClientDisconnect()

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(ClientDisconnect):
        await parser.parse()
    assert parser._files_to_close_on_error
    assert all(f.closed for f in parser._files_to_close_on_error)


def test_mount_router_wrapped_in_middleware_raises_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.cors import CORSMiddleware

    inner = CORSMiddleware(Router([Route("/", echo_len, methods=["POST"])], max_body_size=100), allow_origins=["*"])
    app = Starlette(routes=[Mount("/r", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/r/", content=b"x" * 50).status_code == 200
    assert client.post("/r/", content=iter([b"x" * 25, b"x" * 25])).status_code == 200
    assert client.post("/r/", content=b"x" * 101).status_code == 413


def test_no_route_or_method_in_inner_limit_uses_application_limit(test_client_factory: TestClientFactory) -> None:
    routes = [Route("/", no_read, methods=["POST"])]
    big = {"content-length": "50"}
    raised = test_client_factory(Starlette(routes=[Mount("/m", routes=routes, max_body_size=100)], max_body_size=10))
    assert raised.post("/m/nothing", headers=big).status_code == 413
    assert raised.put("/m/", headers=big).status_code == 413
    lowered = test_client_factory(Starlette(routes=[Mount("/m", routes=routes, max_body_size=5)], max_body_size=1000))
    assert lowered.post("/m/nothing", headers=big).status_code == 404
    assert lowered.put("/m/", headers=big).status_code == 405
    # Without an application limit nothing is rejected for a request that matches nothing.
    unlimited = test_client_factory(Starlette(routes=[Mount("/m", routes=routes, max_body_size=5)]))
    assert unlimited.post("/m/nothing", headers=big).status_code == 404


def test_413_goes_through_the_middleware_stack(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    errors: list[str] = []
    statuses: list[int] = []

    async def on_error(request: Request, exc: Exception) -> Response:
        errors.append(repr(exc))
        return PlainTextResponse("boom", status_code=500)

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    class Log:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            async def wrapped(message: Message) -> None:
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])
                await send(message)

            await self.app(scope, receive, wrapped)

    app = Starlette(
        routes=[
            Route("/", echo_len, methods=["POST"]),
            Route("/low", echo_len, methods=["POST"], max_body_size=3),
        ],
        middleware=[Middleware(Passthrough), Middleware(Log)],
        exception_handlers={Exception: on_error},
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=True)
    assert client.post("/", content=iter([b"x" * 6, b"x" * 6])).status_code == 413
    assert client.post("/low", content=iter([b"x" * 2, b"x" * 2])).status_code == 413
    assert client.post("/", content=b"x" * 20).status_code == 413
    assert errors == []
    assert statuses == [413, 413, 413]


async def _drive(app: Any, chunks: list[bytes], send_delay: float = 0.1) -> list[Message]:
    import anyio

    messages: list[Message] = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await anyio.sleep(send_delay)
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "asgi": {"spec_version": "2.3"},
    }
    await app(scope, receive, send)
    return sent


def _assert_complete_413(sent: list[Message]) -> None:
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"], sent
    assert sent[0]["status"] == 413 and sent[1]["body"] == b"Content Too Large"


@pytest.mark.anyio
async def test_413_survives_cancelled_receive() -> None:
    import anyio

    async def timeout_endpoint(request: Request) -> Response:
        with anyio.move_on_after(0.05):
            await request.body()
        return PlainTextResponse("Request Timeout", status_code=408)

    async def poll_endpoint(request: Request) -> Response:
        await request.is_disconnected()
        await request.is_disconnected()
        return PlainTextResponse("ok")

    async def gen() -> AsyncGenerator[bytes, None]:
        yield b"a"
        yield b"b"

    async def stream_endpoint(request: Request) -> Response:
        from starlette.responses import StreamingResponse

        return StreamingResponse(gen())

    for endpoint in (timeout_endpoint, poll_endpoint, stream_endpoint):
        app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
        _assert_complete_413(await _drive(app, [b"x" * 6, b"x" * 6]))


def test_invalid_limit_types() -> None:
    for bad in (float("nan"), 1.5, "10"):
        with pytest.raises(ValueError):
            Starlette(max_body_size=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            RequestBodyLimitMiddleware(Starlette(), max_body_size=bad)  # type: ignore[arg-type]


def test_nested_router_limit_is_not_the_application_limit(test_client_factory: TestClientFactory) -> None:
    routes = [Route("/", no_read, methods=["POST"])]
    big = {"content-length": "50"}
    via_router = test_client_factory(Starlette(routes=[Mount("/m", app=Router(routes, max_body_size=5))]))
    assert via_router.post("/m/nothing", headers=big).status_code == 404
    assert via_router.put("/m/", headers=big).status_code == 405
    assert via_router.post("/m/", headers=big).status_code == 413
    plain = test_client_factory(Router([Mount("/m", app=Router(routes, max_body_size=5))]))
    assert plain.post("/m/nothing", headers=big).status_code == 404


def test_app_limit_counts_body_read_by_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    sizes: list[int] = []

    class ReadsBody:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            body = await Request(scope, receive).body()
            sizes.append(len(body))  # pragma: no cover
            await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", echo_len, methods=["POST"])], middleware=[Middleware(ReadsBody)], max_body_size=10
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.post("/", content=iter([b"x" * 1000] * 10))
    assert response.status_code == 413 and response.text == "Content Too Large"
    assert sizes == []


def test_unrelated_error_after_limit_is_not_swallowed(test_client_factory: TestClientFactory) -> None:
    class Endpoint:
        def __init__(self, scope: Scope, receive: Receive, send: Send) -> None:
            self.args = (scope, receive, send)

        def __await__(self) -> Any:
            return self.run().__await__()

        async def run(self) -> None:
            _, receive, send = self.args
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"start", "more_body": True})
            while (await receive())["type"] == "http.request":
                pass
            raise ValueError("a real bug")

    app = Starlette(routes=[Route("/", Endpoint, methods=["POST"])], max_body_size=10)
    with pytest.raises(ValueError):
        test_client_factory(app).post("/", content=iter([b"x" * 6, b"x" * 6]))


@pytest.mark.anyio
async def test_stalled_413_send_can_be_cancelled() -> None:
    import anyio

    async def endpoint(request: Request) -> Response:
        await request.body()
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    messages: list[Message] = [{"type": "http.request", "body": b"x" * 20, "more_body": False}]

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await anyio.sleep_forever()

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []}
    with anyio.fail_after(2):
        with anyio.move_on_after(0.1):
            await app(scope, receive, send)


def test_413_body_complete_with_base_http_middleware_and_gzip(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.gzip import GZipMiddleware

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", echo_len, methods=["POST"])],
        middleware=[Middleware(Passthrough), Middleware(GZipMiddleware, minimum_size=0)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for kwargs in ({"content": b"x" * 20}, {"content": iter([b"x" * 6, b"x" * 6])}):
        response = client.post("/", **kwargs)
        assert response.status_code == 413
        assert response.text == "Content Too Large"


@pytest.mark.anyio
async def test_413_is_sent_once_when_receive_is_cancelled_mid_send() -> None:
    import anyio

    async def endpoint(request: Request) -> Response:
        with anyio.move_on_after(0.05):
            await request.body()
        return PlainTextResponse("Request Timeout", status_code=408)

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    messages: list[Message] = [
        {"type": "http.request", "body": b"x" * 6, "more_body": True},
        {"type": "http.request", "body": b"x" * 6, "more_body": True},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        await anyio.sleep_forever()  # pragma: no cover
        raise AssertionError  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)  # the server takes the message, then waits for the socket to drain
        await anyio.sleep(0.2)

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []}
    with anyio.fail_after(5):
        await app(scope, receive, send)
    _assert_complete_413(sent)
