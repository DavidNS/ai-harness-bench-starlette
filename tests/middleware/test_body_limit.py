from __future__ import annotations

import io
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from ..types import TestClientFactory

called: list[str] = []


async def read_body(request: Request) -> Response:
    called.append(request.url.path)
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def read_form(request: Request) -> Response:
    called.append(request.url.path)
    async with request.form() as form:
        return JSONResponse({"keys": sorted(form.keys())})


@pytest.fixture(autouse=True)
def reset_called() -> None:
    called.clear()


def stream(*chunks: bytes) -> Callable[[], Any]:
    def gen() -> Any:
        yield from chunks

    return gen


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=stream(b"x" * 6, b"x" * 6)()))
    assert client.get("/nowhere").status_code == 404
    assert client.post("/", content=b"").json() == {"size": 0}


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_invalid_limit() -> None:
    with pytest.raises(AssertionError):
        Starlette(max_body_size=-1)
    with pytest.raises(AssertionError):
        Router(max_body_size=-1)
    with pytest.raises(AssertionError):
        Route("/", read_body, max_body_size=-1)
    with pytest.raises(AssertionError):
        Mount("/", routes=[], max_body_size=-1)
    with pytest.raises(AssertionError):
        RequestBodyLimitMiddleware(Router(), max_body_size=-1)


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).json() == {"size": 5}
    assert_too_large(client.post("/", content=b"x" * 6))
    assert_too_large(client.post("/", content=stream(b"xxx", b"xxx")()))


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 5).json() == {"size": 5}
    assert_too_large(client.post("/small", content=b"x" * 6))
    assert client.post("/other", content=b"x" * 600).json() == {"size": 600}


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 5).json() == {"size": 5}
    assert_too_large(client.post("/m/", content=b"x" * 6))
    assert_too_large(client.post("/m/", content=stream(b"xxxx", b"xxxx")()))
    assert client.post("/other", content=b"x" * 60).json() == {"size": 60}


def test_mounted_asgi_app_fail_fast(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        called.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Mount("/m", asgi, max_body_size=5)])
    assert_too_large(test_client_factory(app).post("/m/x", content=b"x" * 6))
    assert called == []


def test_inner_route_raises_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Route("/default", read_body, methods=["POST"]),
        ],
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert client.post("/big", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/big", content=stream(b"x" * 30, b"x" * 30)()).json() == {"size": 60}
    assert_too_large(client.post("/big", content=b"x" * 101))
    assert_too_large(client.post("/default", content=b"x" * 6))


def test_inner_route_lowers_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=5)], max_body_size=100)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).json() == {"size": 5}
    assert_too_large(client.post("/", content=b"x" * 6))
    assert_too_large(client.post("/", content=stream(b"xxx", b"xxx")()))


def test_inner_mount_and_router_override(test_client_factory: TestClientFactory) -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)
    app = Starlette(
        routes=[
            Mount("/raise", app=inner, max_body_size=None),
            Mount("/lower", routes=[Route("/", read_body, methods=["POST"], max_body_size=3)], max_body_size=20),
            Mount("/lower2", routes=[Route("/", read_body, methods=["POST"])], max_body_size=3),
            Mount("/raise2", routes=[Route("/", read_body, methods=["POST"])], max_body_size=50),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raise/", content=b"x" * 40).json() == {"size": 40}
    assert_too_large(client.post("/raise/", content=b"x" * 51))
    assert client.post("/raise2/", content=b"x" * 40).json() == {"size": 40}
    assert_too_large(client.post("/lower/", content=b"x" * 4))
    assert_too_large(client.post("/lower2/", content=b"x" * 4))
    assert called == ["/raise/", "/raise2/"]


def test_content_length_fail_fast_in_application(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Route("/get", read_body, methods=["GET"], max_body_size=100),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount("/r", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/nope", content=b"x" * 11))  # no route
    assert_too_large(client.post("/nope/", content=b"x" * 11))  # no route, would redirect
    assert_too_large(client.post("/get", content=b"x" * 11))  # no method: the application limit applies
    assert called == []
    # An inner limit that raises the outer one is not applied too early.
    assert client.post("/big", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/m/", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/r/", content=b"x" * 50).json() == {"size": 50}
    assert_too_large(client.post("/m/", content=b"x" * 101))
    assert called == ["/big", "/m/", "/r/"]


def test_content_length_not_trusted(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    # Understated header.
    assert_too_large(client.post("/", content=b"x" * 50, headers={"content-length": "5"}))
    # Absent header (chunked).
    assert_too_large(client.post("/", content=stream(b"x" * 6, b"x" * 6)()))


def test_standalone_middleware(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        called.append("asgi")
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":  # pragma: no cover
                return
            size += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await PlainTextResponse(str(size))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/", content=b"x" * 11))
    assert called == ["asgi"]  # the declared length is rejected on entry
    assert_too_large(client.post("/", content=stream(b"x" * 6, b"x" * 6)()))
    assert_too_large(client.post("/", content=b"x" * 50, headers={"content-length": "5"}))

    unlimited = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=None))
    assert unlimited.post("/", content=b"x" * 1000).text == "1000"


def test_standalone_middleware_with_starlette(test_client_factory: TestClientFactory) -> None:
    starlette = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    client = test_client_factory(RequestBodyLimitMiddleware(starlette, max_body_size=10))
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/", content=b"x" * 11))
    assert called == ["/"]


def test_standalone_middleware_as_starlette_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=stream(b"x" * 6, b"x" * 6)()))


def test_body_bytes_counted_with_overhead(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=200)
    client = test_client_factory(app)
    assert client.post("/", data={"a": "1"}).json() == {"keys": ["a"]}
    # The file is smaller than the limit, the multipart framing is what crosses it.
    assert_too_large(client.post("/", files={"f": ("f.txt", io.BytesIO(b"x" * 150))}))
    assert client.post("/", files={"f": ("f.txt", io.BytesIO(b"x" * 10))}).json() == {"keys": ["f"]}


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    sent: list[Message] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        messages = []
        while True:
            message = await receive()
            messages.append(message["type"])
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await send({"type": "http.response.body", "body": ",".join(messages).encode()})

    async def capture(scope: Scope, receive: Receive, send: Send) -> None:
        async def send_wrapper(message: Message) -> None:
            sent.append(message)
            await send(message)

        await RequestBodyLimitMiddleware(asgi, max_body_size=10)(scope, receive, send_wrapper)

    response = test_client_factory(capture).post("/", content=stream(b"x" * 6, b"x" * 6)())
    assert response.status_code == 200
    assert response.text == "http.disconnect"
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


def test_body_stream_stopped_after_limit() -> None:
    async def run() -> list[str]:
        seen: list[str] = []
        messages = iter([{"type": "http.request", "body": b"x" * 6, "more_body": True}] * 3)
        sent: list[Message] = []

        async def receive() -> Message:
            return next(messages)

        async def send(message: Message) -> None:
            sent.append(message)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            for _ in range(3):
                seen.append((await receive())["type"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"late"})

        await RequestBodyLimitMiddleware(app, max_body_size=10)({"type": "http", "headers": []}, receive, send)
        assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
        assert sent[0]["status"] == 413
        return seen

    import anyio

    assert anyio.run(run) == ["http.request", "http.disconnect", "http.disconnect"]


def test_non_http_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    events: list[str] = []

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    async def lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        events.append("startup")
        yield
        events.append("shutdown")

    from contextlib import asynccontextmanager

    from starlette.routing import WebSocketRoute

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1, lifespan=asynccontextmanager(lifespan))
    wrapped = RequestBodyLimitMiddleware(app, max_body_size=1)
    with TestClient(wrapped) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("hello world")
            assert session.receive_text() == "hello world"
    assert events == ["startup", "shutdown"]


def test_multipart_temp_files_closed_when_limit_crossed() -> None:
    import anyio

    files: list[Any] = []
    original = MultiPartParser.on_headers_finished

    def spy(self: MultiPartParser) -> None:
        original(self)
        files.extend(self._files_to_close_on_error)

    async def handler(request: Request) -> Response:
        try:
            async with request.form():
                pass  # pragma: no cover
        except ClientDisconnect:
            return PlainTextResponse("disconnected")
        return PlainTextResponse("done")  # pragma: no cover

    head = b"--b\r\nContent-Disposition: form-data; name=f; filename=f.txt\r\n\r\n"
    chunks = [head + b"x" * 600, b"y" * 600, b"\r\n--b--\r\n"]
    messages = [{"type": "http.request", "body": c, "more_body": True} for c in chunks]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"multipart/form-data; boundary=b")],
    }
    app = Starlette(routes=[Route("/", handler, methods=["POST"])], max_body_size=1000)
    MultiPartParser.on_headers_finished = spy  # type: ignore[method-assign]
    try:
        anyio.run(app, scope, receive, send)
    finally:
        MultiPartParser.on_headers_finished = original  # type: ignore[method-assign]
    assert sent[0]["status"] == 413
    assert len(files) == 1
    assert files[0].closed


def test_multipart_temp_files_closed_when_stream_fails() -> None:
    import anyio

    async def run() -> list[Any]:
        files: list[Any] = []

        async def failing() -> AsyncGenerator[bytes, None]:
            yield b"--b\r\nContent-Disposition: form-data; name=f; filename=f.txt\r\n\r\nabc"
            raise RuntimeError("interrupted")

        from starlette.datastructures import Headers

        parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), failing())
        with pytest.raises(RuntimeError):
            try:
                await parser.parse()
            finally:
                files.extend(parser._files_to_close_on_error)
        return files

    files = anyio.run(run)
    assert len(files) == 1
    assert files[0].closed


def test_unmatched_path_inside_limited_mount_and_router(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/ok", read_body, methods=["GET"])], max_body_size=5),
            Mount("/r", app=Router(routes=[Route("/ok", read_body, methods=["GET"])], max_body_size=5)),
            Mount("/big", routes=[Route("/ok", read_body, methods=["GET"])], max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for prefix in ("/m", "/r"):
        assert_too_large(client.post(f"{prefix}/nope", content=b"x" * 6))  # no route
        assert_too_large(client.post(f"{prefix}/ok", content=b"x" * 6))  # no method
        assert client.post(f"{prefix}/nope", content=b"x" * 5).status_code == 404
    assert client.post("/big/nope", content=b"x" * 50).status_code == 404
    assert client.post("/big/ok", content=b"x" * 50).status_code == 405
    assert called == []


def _wrapped(app: Any) -> Any:
    from starlette.middleware.cors import CORSMiddleware

    return CORSMiddleware(app, allow_origins=["*"])


@pytest.mark.parametrize("wrap", [_wrapped])
def test_wrapped_inner_app_can_raise_limit_with_content_length(
    test_client_factory: TestClientFactory, wrap: Callable[[Any], Any]
) -> None:
    from starlette.routing import Host

    inner_router = Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    inner_app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(
        routes=[
            Mount("/r", app=wrap(inner_router)),
            Mount("/s", app=wrap(inner_app)),
            Host("example.org", app=wrap(inner_router)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for url, host in (("/r/", "testserver"), ("/s/", "testserver"), ("/", "example.org")):
        headers = {"host": host}
        assert client.post(url, content=stream(b"x" * 25, b"x" * 25)(), headers=headers).json() == {"size": 50}
        assert client.post(url, content=b"x" * 50, headers=headers).json() == {"size": 50}
        assert_too_large(client.post(url, content=b"x" * 101, headers=headers))


def test_huge_content_length_digits(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x", headers={"content-length": "1" + "0" * 5000}))
    assert client.post("/", content=b"x" * 5, headers={"content-length": "0" * 5000 + "5"}).json() == {"size": 5}
    standalone = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert_too_large(standalone.post("/", content=b"x", headers={"content-length": "1" + "0" * 5000}))


def test_class_endpoint_without_method_uses_outer_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.endpoints import HTTPEndpoint

    class Ep(HTTPEndpoint):
        async def get(self, request: Request) -> Response:
            return PlainTextResponse("ok")  # pragma: no cover

        async def put(self, request: Request) -> Response:
            return JSONResponse({"size": len(await request.body())})

    app = Starlette(routes=[Route("/", Ep, max_body_size=100)], max_body_size=10)
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 50))
    assert client.post("/", content=b"x" * 5).status_code == 405
    assert client.put("/", content=b"x" * 50).json() == {"size": 50}


def test_413_messages_not_shared_between_requests(test_client_factory: TestClientFactory) -> None:
    from starlette.datastructures import MutableHeaders

    async def tag(scope: Scope, receive: Receive, send: Send) -> None:
        async def wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("x-user", dict(scope["headers"])[b"x-user"].decode())
            await send(message)

        await RequestBodyLimitMiddleware(PlainTextResponse("ok"), max_body_size=10)(scope, receive, wrapper)

    client = test_client_factory(tag)
    client.post("/", content=b"x" * 12, headers={"x-user": "alice"})
    response = client.post("/", content=b"x" * 12, headers={"x-user": "bob"})
    assert response.headers.get_list("x-user") == ["bob"]


def test_413_completed_when_receive_is_cancelled() -> None:
    import anyio

    async def run() -> list[Message]:
        sent: list[Message] = []
        messages = iter([{"type": "http.request", "body": b"x" * 20, "more_body": True}])

        async def receive() -> Message:
            return next(messages)

        async def send(message: Message) -> None:
            await anyio.sleep(0.01)  # a slow client
            sent.append(message)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            async with anyio.create_task_group() as tg:

                async def listen() -> None:
                    await receive()

                tg.start_soon(listen)
                await anyio.sleep(0)
                await anyio.sleep(0.001)
                tg.cancel_scope.cancel()

        await RequestBodyLimitMiddleware(app, max_body_size=10)({"type": "http", "headers": []}, receive, send)
        return sent

    sent = anyio.run(run)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"] == b"Content Too Large"


def test_mounted_app_merely_referencing_a_router_fails_fast(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import Host

    class Admin:
        def __init__(self, main: Any) -> None:
            self.app = main  # kept for url building, the request is not forwarded to it

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            called.append("admin")
            await PlainTextResponse("admin")(scope, receive, send)

    def make_closure() -> Any:
        router = Router(routes=[])

        async def plain(scope: Scope, receive: Receive, send: Send) -> None:
            called.append(f"closure {len(router.routes)}")
            await PlainTextResponse("plain")(scope, receive, send)

        return plain

    app = Starlette(
        routes=[
            Mount("/admin", app=Admin(Starlette()), max_body_size=5),
            Mount("/p", app=make_closure()),
            Host("example.org", app=make_closure()),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/admin/x", content=b"x" * 6))
    assert_too_large(client.post("/p/", content=b"x" * 50))
    assert_too_large(client.post("/", content=b"x" * 50, headers={"host": "example.org"}))
    assert called == []
    assert client.post("/p/", content=b"x" * 5).text == "plain"
