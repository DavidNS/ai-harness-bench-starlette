from __future__ import annotations

from collections.abc import Iterable, Iterator
from tempfile import SpooledTemporaryFile
from typing import Any
from unittest import mock

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

TOO_LARGE = "Content Too Large"


def chunks(*sizes: int) -> Iterator[bytes]:
    for size in sizes:
        yield b"x" * size


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def calls_recorder() -> tuple[list[str], Any]:
    calls: list[str] = []

    async def endpoint(request: Request) -> Response:
        calls.append(request.url.path)
        return await read_body(request)

    return calls, endpoint


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=chunks(5, 6))  # no Content-Length
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert client.post("/", content=chunks(5, 5)).text == "10"


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_no_body_is_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    assert test_client_factory(app).get("/").text == "0"


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"Content-Length": "5"})
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=chunks(11)).status_code == 413
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100).text == "100"
    assert client.post("/m/", content=b"x" * 10).text == "10"
    assert client.post("/m/", content=chunks(11)).status_code == 413


def test_mount_limit_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    calls: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("called")
        await Response("ok")(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Mount("/m", asgi, max_body_size=10)]))
    # An opaque app may route, so the declared length is checked when it first receives or sends.
    response = client.post("/m/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert client.post("/m/", content=b"x" * 10).text == "ok"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/small", read_body, methods=["POST"], max_body_size=10),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100).text == "100"
    assert client.post("/small", content=b"x" * 10).text == "10"
    assert client.post("/small", content=chunks(11)).status_code == 413


def test_route_limit_without_outer_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=3)])
    assert test_client_factory(router).post("/", content=chunks(2, 2)).status_code == 413


def test_inner_limits_raise_the_outer_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Mount(
                "/m",
                routes=[
                    Route("/", read_body, methods=["POST"]),
                    Route("/big", read_body, methods=["POST"], max_body_size=100),
                ],
                max_body_size=50,
            ),
            Mount("/r", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=70)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=chunks(11)).status_code == 413
    assert client.post("/big", content=chunks(60, 40)).text == "100"
    assert client.post("/big", content=b"x" * 100).text == "100"
    assert client.post("/big", content=chunks(101)).status_code == 413
    assert client.post("/m/", content=chunks(50)).text == "50"
    assert client.post("/m/", content=chunks(51)).status_code == 413
    assert client.post("/m/big", content=chunks(100)).text == "100"  # route over mount over app
    assert client.post("/r/", content=chunks(70)).text == "70"
    assert client.post("/r/", content=chunks(71)).status_code == 413


def test_inner_limits_lower_the_outer_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"], max_body_size=3)], max_body_size=20),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/small", content=chunks(5)).text == "5"
    assert client.post("/small", content=chunks(2, 4)).status_code == 413
    assert client.post("/m/", content=chunks(4)).status_code == 413
    assert client.post("/m/", content=b"x" * 4).status_code == 413


def test_content_length_fail_fast_uses_the_route_limit(test_client_factory: TestClientFactory) -> None:
    calls, endpoint = calls_recorder()
    seen: list[str] = []

    class Spy(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            seen.append("middleware")  # user middleware runs before routing
            response: Response = await call_next(request)
            return response

    app = Starlette(
        routes=[
            Route("/", endpoint, methods=["POST"]),
            Route("/big", endpoint, methods=["POST"], max_body_size=100),
            Route("/small", endpoint, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/", endpoint, methods=["POST"])], max_body_size=50),
        ],
        middleware=[Middleware(Spy)],
        max_body_size=10,
    )
    client = test_client_factory(app)

    # The inner limit settles it: a declared length over the app limit may still be allowed.
    assert client.post("/big", content=b"x" * 50).text == "50"
    assert client.post("/m/", content=b"x" * 50).text == "50"
    assert calls == ["/big", "/m/"]

    # Over the settled limit: rejected before the endpoint runs.
    calls.clear()
    for path, size in [("/", 11), ("/big", 101), ("/small", 6), ("/m/", 51)]:
        response = client.post(path, content=b"x" * size)
        assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert calls == []


def test_content_length_fail_fast_for_no_route_and_no_method(test_client_factory: TestClientFactory) -> None:
    calls, endpoint = calls_recorder()
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"], max_body_size=1000)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/missing", content=b"x" * 11).status_code == 413
    assert client.post("/missing", content=b"x" * 5).status_code == 404
    # No method: the limit outside of the route applies.
    assert client.put("/", content=b"x" * 11).status_code == 413
    assert client.put("/", content=b"x" * 5).status_code == 405
    assert calls == []


def test_content_length_fail_fast_with_redirect_slashes(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/x/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/x", content=b"x" * 11, follow_redirects=False).status_code == 413


def test_content_length_with_invalid_value_is_ignored(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"xy", headers={"Content-Length": "abc"}).text == "2"


def test_nested_router_limits_without_application(test_client_factory: TestClientFactory) -> None:
    calls, endpoint = calls_recorder()
    router = Router(
        routes=[Mount("/m", routes=[Route("/", endpoint, methods=["POST"], max_body_size=100)], max_body_size=5)],
        max_body_size=10,
    )
    client = test_client_factory(router)
    assert client.post("/m/", content=b"x" * 50).text == "50"
    assert client.post("/m/", content=b"x" * 101).status_code == 413
    assert calls == ["/m/"]


def test_limit_lowered_after_body_was_read(test_client_factory: TestClientFactory) -> None:
    class ReadFirst(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            response: Response = await call_next(request)  # pragma: no cover
            return response  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=5)],
        middleware=[Middleware(ReadFirst)],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 50).status_code == 413


def test_body_read_by_base_http_middleware_is_limited(test_client_factory: TestClientFactory) -> None:
    class ReadFirst(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await request.body()
            response: Response = await call_next(request)  # pragma: no cover
            return response  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(ReadFirst)], max_body_size=10
    )
    assert test_client_factory(app).post("/", content=chunks(6, 6)).status_code == 413


def test_body_read_downstream_of_base_http_middleware_is_limited(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            response: Response = await call_next(request)
            return response  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(Passthrough)], max_body_size=10
    )
    response = test_client_factory(app).post("/", content=chunks(6, 6))
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_limit_is_not_swallowed_by_exception_handlers(test_client_factory: TestClientFactory) -> None:
    async def handler(request: Request, exc: Exception) -> Response:
        return PlainTextResponse("handled", status_code=500)  # pragma: no cover

    async def catching(request: Request) -> Response:
        try:
            await request.body()
        except Exception:  # pragma: no cover
            return PlainTextResponse("swallowed")
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(
        routes=[Route("/", catching, methods=["POST"])],
        exception_handlers={Exception: handler},
        max_body_size=5,
    )
    response = test_client_factory(app, raise_server_exceptions=False).post("/", content=chunks(3, 3))
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    received: list[int] = []
    after_limit: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        try:
            while True:
                message = await receive()
                received.append(len(message.get("body", b"")))  # pragma: no cover
                if not message.get("more_body"):  # pragma: no cover
                    break
        finally:
            try:
                await receive()
            except BaseException:
                after_limit.append("stopped")
                raise

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    response = client.post("/", content=chunks(6, 6, 6))
    assert response.status_code == 200
    assert received == []  # the test client sends the body as one message
    assert after_limit == ["stopped"]


def test_response_already_started_in_starlette(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"first"
            async for _ in request.stream():
                pass  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    response = test_client_factory(app).post("/", content=chunks(6, 6))
    assert response.status_code == 200


def test_standalone_middleware_with_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    called = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called += 1
        message = await receive()
        size = len(message.get("body", b""))
        await Response(str(size))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert called == 1
    response = client.post("/", content=chunks(5, 6))
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    assert called == 2
    # Fail-fast on entry: the application is never called.
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert called == 2
    assert client.post("/", content=b"xx", headers={"Content-Length": "1"}).text == "2"


def test_standalone_middleware_not_overridden_by_inner_limits(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)])
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=chunks(6, 6)).status_code == 413
    assert client.post("/", content=b"x" * 10).text == "10"


def test_standalone_middleware_in_starlette_middleware(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=chunks(6, 6)).text == TOO_LARGE
    assert client.post("/", content=b"x" * 10).text == "10"


def test_websocket_and_lifespan_are_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    started = False

    async def lifespan(app: Any) -> Any:
        nonlocal started
        started = True
        yield

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from contextlib import asynccontextmanager

    app = Starlette(
        routes=[WebSocketRoute("/ws", ws)],
        lifespan=asynccontextmanager(lifespan),
        max_body_size=1,
    )
    with test_client_factory(app) as client:
        assert started
        with client.websocket_connect("/ws") as session:
            session.send_text("hello world")
            assert session.receive_text() == "hello world"

    standalone = RequestBodyLimitMiddleware(app, max_body_size=1)
    with test_client_factory(standalone) as client:
        with client.websocket_connect("/ws") as session:
            session.send_text("hello world")
            assert session.receive_text() == "hello world"


# multipart


def multipart_body(boundary: str, file_size: int) -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + b"x" * file_size
        + f"\r\n--{boundary}--\r\n".encode()
    )


async def upload(request: Request) -> Response:
    async with request.form() as form:
        file = form["file"]
        assert not isinstance(file, str)
        return PlainTextResponse(str(file.size))


def test_multipart_limit_counts_the_whole_body(test_client_factory: TestClientFactory) -> None:
    boundary = "a7f7ac8d4e2e437c877bb7b8d7cc549c"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    body = multipart_body(boundary, file_size=100)
    overhead = len(body) - 100

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=100 + overhead)
    client = test_client_factory(app)
    assert client.post("/", content=body, headers=headers).text == "100"

    # The file itself fits, but the encoding overhead does not.
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=100 + overhead - 1)
    client = test_client_factory(app)
    response = client.post("/", content=body, headers=headers)
    assert (response.status_code, response.text) == (413, TOO_LARGE)
    response = client.post("/", content=chunks_of(body, 7), headers=headers)
    assert (response.status_code, response.text) == (413, TOO_LARGE)


def chunks_of(data: bytes, size: int) -> Iterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def run_asgi(
    app: Any, body_chunks: Iterable[bytes], headers: list[tuple[bytes, bytes]] | None = None, path: str = "/"
) -> list[Message]:
    """Call an ASGI app with the body arriving in separate messages, without a Content-Length."""
    pending = list(body_chunks)
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": bool(pending)}
        await anyio.sleep_forever()  # pragma: no cover
        raise AssertionError  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": headers or [], "query_string": b""}
    await app(scope, receive, send)
    return sent


def test_chunked_body_is_counted_across_messages() -> None:
    received: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            received.append(len(message["body"]))
            if not message["more_body"]:  # pragma: no cover
                break
        await Response()(scope, receive, send)  # pragma: no cover

    sent = anyio.run(run_asgi, RequestBodyLimitMiddleware(app, max_body_size=10), [b"x" * 6, b"x" * 6, b"x" * 6])
    assert received == [6]
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert sent[-1]["body"] == b"Content Too Large"


def test_multipart_temp_files_are_closed_when_limit_is_crossed() -> None:
    closed: list[bool] = []

    class TrackingSpooledTemporaryFile(SpooledTemporaryFile):  # type: ignore[type-arg]
        def close(self) -> None:
            closed.append(True)
            super().close()

    boundary = "a7f7ac8d4e2e437c877bb7b8d7cc549c"
    headers = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=200)
    body = multipart_body(boundary, 1000)
    with mock.patch("starlette.formparsers.SpooledTemporaryFile", TrackingSpooledTemporaryFile):
        sent = anyio.run(run_asgi, app, list(chunks_of(body, 50)), headers)
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert closed == [True]


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, RuntimeError])
def test_multipart_temp_files_are_closed_on_any_interruption(exc_type: type[BaseException]) -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    closed: list[bool] = []

    class TrackingSpooledTemporaryFile(SpooledTemporaryFile):  # type: ignore[type-arg]
        def close(self) -> None:
            closed.append(True)
            super().close()

    boundary = "a7f7ac8d4e2e437c877bb7b8d7cc549c"
    body = multipart_body(boundary, 10)

    async def stream() -> Any:
        yield body[:-20]
        raise exc_type()

    async def main() -> None:
        parser = MultiPartParser(Headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}), stream())
        with pytest.raises(exc_type):
            await parser.parse()

    with mock.patch("starlette.formparsers.SpooledTemporaryFile", TrackingSpooledTemporaryFile):
        anyio.run(main)
    assert closed == [True]


def test_disconnect_is_passed_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.disconnect"
        await Response()(scope, receive, send)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=1), scope, receive, send)
    assert sent[0]["status"] == 200


class Passthrough:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def test_inner_limit_raises_outer_limit_behind_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.cors import CORSMiddleware

    def inner() -> Any:
        return CORSMiddleware(
            Passthrough(Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])),
            allow_origins=["*"],
        )

    for app in (
        Starlette(routes=[Mount("/m", app=inner())], max_body_size=10),
        Starlette(routes=[Mount("/m", app=inner(), max_body_size=10)]),
        Starlette(
            routes=[
                Mount(
                    "/m",
                    app=Passthrough(Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])),
                )
            ],
            max_body_size=10,
        ),
    ):
        client = test_client_factory(app)
        assert client.post("/m/", content=chunks(50)).text == "50"
        assert client.post("/m/", content=b"x" * 50).text == "50"
        assert client.post("/m/", content=b"x" * 101).status_code == 413

    app = Starlette(
        routes=[
            Route(
                "/",
                Passthrough(Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])),
                max_body_size=10,
            )
        ]
    )
    assert test_client_factory(app).post("/", content=b"x" * 50).text == "50"


def test_huge_and_padded_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=10)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x", headers={"Content-Length": "9" * 5000}).status_code == 413
    padded = "0" * 5000 + "5"
    assert client.post("/", content=b"x" * 5, headers={"Content-Length": padded}).text == "5"
    standalone = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert standalone.post("/", content=b"x", headers={"Content-Length": "9" * 5000}).status_code == 413


@pytest.mark.parametrize("value", [-1, "10", 1.5, True])
def test_max_body_size_is_validated(value: Any) -> None:
    exc = ValueError if value == -1 else TypeError
    for factory in (
        lambda: Starlette(max_body_size=value),
        lambda: Router(max_body_size=value),
        lambda: Mount("/m", routes=[], max_body_size=value),
        lambda: Route("/", read_body, max_body_size=value),
        lambda: RequestBodyLimitMiddleware(Router(), max_body_size=value),
    ):
        with pytest.raises(exc):
            factory()


def test_real_error_next_to_limit_is_not_swallowed(test_client_factory: TestClientFactory) -> None:
    async def both(request: Request) -> Response:
        async def read() -> None:
            await request.body()

        async def fail() -> None:
            raise RuntimeError("real error")

        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            tg.start_soon(fail)
        return Response()  # pragma: no cover

    app = Starlette(routes=[Route("/", both, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app, raise_server_exceptions=False)
    assert client.post("/", content=chunks(6, 6)).status_code == 500


def test_other_error_after_limit_is_raised() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except BaseException:
            raise RuntimeError("other") from None

    with pytest.raises(RuntimeError, match="other"):
        anyio.run(run_asgi, RequestBodyLimitMiddleware(app, max_body_size=1), [b"xx"])


def closure_wrap(inner: Any) -> Any:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        await inner(scope, receive, send)

    return wrapped


class OtherName:
    def __init__(self, application: Any) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.application(scope, receive, send)


@pytest.mark.parametrize("wrap", [closure_wrap, OtherName, lambda app: __import__("functools").partial(app)])
def test_wrapped_inner_router_raises_limit_with_declared_length(
    test_client_factory: TestClientFactory, wrap: Any
) -> None:
    def inner(path: str = "/") -> Any:
        return wrap(Router(routes=[Route(path, read_body, methods=["POST"], max_body_size=100)]))

    for app in (
        Starlette(routes=[Mount("/m", app=inner())], max_body_size=10),
        Starlette(routes=[Mount("/m", app=inner(), max_body_size=10)]),
        Starlette(routes=[Route("/m", OtherName(inner("/m")), max_body_size=10)]),
    ):
        client = test_client_factory(app)
        path = "/m/" if len(app.routes) and isinstance(app.routes[0], Mount) else "/m"
        assert client.post(path, content=chunks(50)).text == "50"
        assert client.post(path, content=b"x" * 50).text == "50"
        assert client.post(path, content=b"x" * 101).status_code == 413


def test_opaque_wrapper_without_router_checks_declared_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Mount("/m", app=closure_wrap(Response("ok")), max_body_size=10)])
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 11).status_code == 413
    assert client.post("/m/", content=b"x" * 10).text == "ok"


def test_route_limit_without_outer_limit_declared_length(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"], max_body_size=3)])
    client = test_client_factory(router)
    assert client.post("/", content=b"xxxx").status_code == 413
    assert client.post("/", content=b"xxx").text == "3"


def test_http_endpoint_is_rejected_before_it_runs(test_client_factory: TestClientFactory) -> None:
    from starlette.endpoints import HTTPEndpoint

    calls: list[str] = []

    class Endpoint(HTTPEndpoint):
        async def post(self, request: Request) -> Response:
            calls.append("post")
            return PlainTextResponse("ok")

    for app in (
        Starlette(routes=[Route("/", Endpoint, max_body_size=10)]),
        Starlette(routes=[Route("/", Endpoint)], max_body_size=10),
    ):
        client = test_client_factory(app)
        assert client.post("/", content=b"x" * 11).status_code == 413
        assert calls == []
        assert client.post("/", content=b"x" * 10).text == "ok"
        calls.clear()


def test_concurrent_tasks_of_opaque_app_stop_after_declared_length_rejected() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def reader() -> None:
            await receive()

        async def writer() -> None:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0.01)
                await Response("app ran")(scope, receive, send)  # pragma: no cover

        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(writer)

    starlette_app = Starlette(routes=[Mount("/m", app=app)], max_body_size=10)

    async def main() -> list[Message]:
        return await run_asgi(starlette_app, [b"x" * 5], [(b"content-length", b"50")], path="/m/")

    sent = anyio.run(main)
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
