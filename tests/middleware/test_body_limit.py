from __future__ import annotations

import functools
from tempfile import SpooledTemporaryFile
from typing import Any

import anyio
import pytest

from starlette import formparsers
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Host, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

called: list[str] = []


async def read_body(request: Request) -> Response:
    called.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


@pytest.fixture(autouse=True)
def reset_called() -> None:
    called.clear()


async def asgi_request(
    app: Any,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    method: str = "POST",
    path: str = "/",
    spec_version: str | None = None,
) -> tuple[list[Message], int]:
    """Drive an ASGI app directly, so that Content-Length can be absent or wrong."""
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "root_path": "",
    }
    if spec_version is not None:
        scope["asgi"] = {"version": "3.0", "spec_version": spec_version}
    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    pulled = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal pulled
        if pending:
            pulled += 1
            return pending.pop(0)
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent, pulled


def status_and_body(sent: list[Message]) -> tuple[int, bytes]:
    start = [m for m in sent if m["type"] == "http.response.start"]
    assert len(start) == 1
    return start[0]["status"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 10)
    assert (response.status_code, response.text) == (200, "10")
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert called == ["/"]


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_requests_without_body_are_unaffected(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body)], max_body_size=0))
    assert client.get("/").text == "0"


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"12345").status_code == 200
    assert client.post("/", content=b"123456").status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"123456").status_code == 413
    assert client.post("/small", content=b"12345").status_code == 200
    assert client.post("/free", content=b"x" * 5000).status_code == 200


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Route("/", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"123456").status_code == 413
    assert client.post("/m/", content=b"12345").status_code == 200
    assert client.post("/", content=b"x" * 5000).status_code == 200


def test_inner_limits_raise_and_lower_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_body, methods=["POST"], max_body_size=100),
            Route("/lower", read_body, methods=["POST"], max_body_size=2),
            Route("/inherit", read_body, methods=["POST"]),
            Mount(
                "/mount",
                max_body_size=50,
                routes=[
                    Route("/", read_body, methods=["POST"]),
                    Route("/raise", read_body, methods=["POST"], max_body_size=100),
                ],
            ),
            Mount(
                "/router",
                app=Router([Route("/", read_body, methods=["POST"])], max_body_size=60),
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raise", content=b"x" * 80).status_code == 200
    assert client.post("/raise", content=b"x" * 101).status_code == 413
    assert client.post("/lower", content=b"x" * 3).status_code == 413
    assert client.post("/lower", content=b"x" * 2).status_code == 200
    assert client.post("/inherit", content=b"x" * 11).status_code == 413
    assert client.post("/mount/", content=b"x" * 50).status_code == 200
    assert client.post("/mount/", content=b"x" * 51).status_code == 413
    assert client.post("/mount/raise", content=b"x" * 100).status_code == 200
    assert client.post("/router/", content=b"x" * 60).status_code == 200
    assert client.post("/router/", content=b"x" * 61).status_code == 413


@pytest.mark.anyio
async def test_content_length_fail_fast_uses_settled_limit() -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_body, methods=["POST"], max_body_size=100),
            Route("/lower", read_body, methods=["POST"], max_body_size=2),
            Route("/inherit", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=20),
        ],
        max_body_size=10,
    )

    async def post(path: str, declared: int, method: str = "POST") -> tuple[int, bytes, int]:
        sent, pulled = await asgi_request(
            app, [b"x"], [(b"content-length", str(declared).encode())], method=method, path=path
        )
        return (*status_and_body(sent), pulled)

    # Declared lengths between the outer and inner limit follow the inner one.
    assert (await post("/raise", 50))[0] == 200
    assert called == ["/raise"]
    called.clear()
    for path, declared in [("/raise", 101), ("/lower", 3), ("/inherit", 11), ("/m/", 21), ("/missing", 11)]:
        status, body, pulled = await post(path, declared)
        assert (status, body) == (413, b"Content Too Large"), path
        assert pulled == 0, path  # the body was never read
    assert called == []
    # A route that doesn't accept the method falls back to the application limit.
    status, body, pulled = await post("/raise", 11, method="PUT")
    assert (status, body, pulled) == (413, b"Content Too Large", 0)
    status, _, _ = await post("/raise", 5, method="PUT")
    assert status == 405
    # Redirects are decided under the application limit as well.
    assert (await post("/inherit/", 11))[0] == 413


@pytest.mark.anyio
async def test_understated_or_missing_content_length_does_not_let_body_through() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    for headers in ([], [(b"content-length", b"1")], [(b"content-length", b"bogus")]):
        sent, pulled = await asgi_request(app, [b"x" * 6, b"x" * 6, b"x" * 6], headers)
        assert status_and_body(sent) == (413, b"Content Too Large")
        assert pulled == 2  # the stream is stopped at the chunk that crossed the limit
    assert called == ["/", "/", "/"]


@pytest.mark.anyio
async def test_standalone_middleware_rejects_on_entry() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("not reached")  # pragma: no cover

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=10)
    sent, pulled = await asgi_request(wrapped, [b"x"], [(b"content-length", b"11")])
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert pulled == 0


@pytest.mark.anyio
async def test_standalone_middleware_with_plain_asgi_app() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        size = 0
        while True:
            message = await receive()
            size += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await Response(str(size))(scope, receive, send)

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=10)
    assert status_and_body((await asgi_request(wrapped, [b"x" * 5, b"x" * 5]))[0]) == (200, b"10")
    assert status_and_body((await asgi_request(wrapped, [b"x" * 5, b"x" * 6]))[0]) == (413, b"Content Too Large")


def test_standalone_middleware_in_front_of_starlette(test_client_factory: TestClientFactory) -> None:
    # The standalone limit can not be overridden by routes below it.
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert client.post("/", content=b"x" * 10).status_code == 200
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")


def test_standalone_middleware_as_starlette_middleware(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=4)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"1234").status_code == 200
    assert client.post("/", content=b"12345").status_code == 413


@pytest.mark.anyio
async def test_websocket_and_lifespan_are_unaffected() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=0)
    for scope_type in ("websocket", "lifespan"):
        await wrapped({"type": scope_type}, None, None)  # type: ignore[arg-type]
    assert seen == ["websocket", "lifespan"]


def test_websocket_on_limited_application(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app).websocket_connect("/ws") as session:
        session.send_text("hello, longer than the limit")
        assert session.receive_text() == "hello, longer than the limit"


@pytest.mark.anyio
async def test_response_already_started_gets_no_second_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive()).get("more_body"):
            pass  # pragma: no cover
        await send({"type": "http.response.body", "body": b"done"})  # pragma: no cover

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=10)
    sent, _ = await asgi_request(wrapped, [b"x" * 8, b"x" * 8])
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_stream_stays_stopped_when_application_swallows_the_error() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        failures = 0
        for _ in range(3):
            try:
                await receive()
            except Exception:
                failures += 1

    wrapped = RequestBodyLimitMiddleware(app, max_body_size=4)
    sent, _ = await asgi_request(wrapped, [b"x" * 5, b"x", b"x"])
    # The application swallowed the error and sent nothing: the request still gets a 413.
    assert status_and_body(sent) == (413, b"Content Too Large")


@pytest.mark.anyio
async def test_http_exception_handler_cannot_hide_the_limit() -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:
            pass
        return PlainTextResponse("handled")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    sent, _ = await asgi_request(app, [b"x", b"x" * 3])
    assert status_and_body(sent) == (413, b"Content Too Large")


def multipart_body(boundary: str, files: int, size: int) -> bytes:
    parts = b""
    for i in range(files):
        parts += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="f{i}"; filename="f{i}.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode()
            + b"x" * size
            + b"\r\n"
        )
    return parts + f"--{boundary}--\r\n".encode()


async def form_endpoint(request: Request) -> Response:
    async with request.form() as form:
        return PlainTextResponse(str(len(form)))


def test_multipart_counts_file_data_and_overhead(test_client_factory: TestClientFactory) -> None:
    boundary = "xyz"
    body = multipart_body(boundary, files=1, size=100)
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    ok = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=len(body))
    assert test_client_factory(ok).post("/", content=body, headers=headers).text == "1"
    # The file data alone fits, the multipart framing does not.
    tight = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=len(body) - 1)
    response = test_client_factory(tight).post("/", content=body, headers=headers)
    assert (response.status_code, response.text) == (413, "Content Too Large")


@pytest.mark.anyio
async def test_multipart_temp_files_are_closed_when_limit_is_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)
    boundary = "xyz"
    body = multipart_body(boundary, files=3, size=500)
    headers = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=len(body) - 600)
    chunks = [body[i : i + 300] for i in range(0, len(body), 300)]
    sent, _ = await asgi_request(app, chunks, headers)
    assert status_and_body(sent)[0] == 413
    assert created and all(file.closed for file in created)


@pytest.mark.anyio
async def test_multipart_parser_closes_files_on_any_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)
    boundary = "xyz"
    body = multipart_body(boundary, files=1, size=100)

    class Boom(Exception):
        pass

    async def stream() -> Any:
        yield body[:150]
        raise Boom()

    from starlette.datastructures import Headers

    parser = formparsers.MultiPartParser(
        Headers({"content-type": f"multipart/form-data; boundary={boundary}"}), stream()
    )
    with pytest.raises(Boom):
        await parser.parse()
    assert created and all(file.closed for file in created)


@pytest.mark.anyio
async def test_other_errors_and_disconnects_pass_through() -> None:
    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await asgi_request(RequestBodyLimitMiddleware(failing, max_body_size=10), [b"x"])

    messages: list[Message] = []

    async def disconnecting(scope: Scope, receive: Receive, send: Send) -> None:
        messages.append(await receive())
        await Response()(scope, receive, send)

    wrapped = RequestBodyLimitMiddleware(disconnecting, max_body_size=0)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pass

    await wrapped({"type": "http", "headers": []}, receive, send)
    assert messages == [{"type": "http.disconnect"}]


def test_response_started_while_listening_for_disconnect(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"first"
            async for _ in request.stream():
                pass  # pragma: no cover
            yield b"second"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=50)

    async def run() -> list[Message]:
        return (await asgi_request(app, [b"x" * 10] * 50))[0]

    sent = anyio.run(run)
    assert status_and_body(sent)[0] == 200
    assert b"Content Too Large" not in b"".join(m.get("body", b"") for m in sent)


class Passthrough(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        return await call_next(request)


class CatchAll(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except Exception:
            return PlainTextResponse("error", status_code=500)


class LogBody(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        await request.body()
        return await call_next(request)


async def swallowing(request: Request) -> Response:
    try:
        await request.body()
    except Exception:
        pass
    return PlainTextResponse("handled")


TOO_LARGE = (413, b"Content Too Large")


@pytest.mark.anyio
async def test_rejection_is_not_seen_by_layers_between_the_limit_and_the_route() -> None:
    # Cause: the fail-fast check answers by itself instead of raising through the layers below it.
    sub = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    app = Starlette(routes=[Mount("/sub", app=sub)], middleware=[Middleware(CatchAll)], max_body_size=10)
    cl = [(b"content-length", b"11")]
    assert status_and_body((await asgi_request(app, [b"x" * 11], cl, path="/sub/"))[0]) == TOO_LARGE
    assert status_and_body((await asgi_request(app, [b"x" * 11], cl, path="/missing"))[0]) == TOO_LARGE
    assert called == []
    # Without a Content-Length the body is cut off while the sub application reads it.
    assert status_and_body((await asgi_request(app, [b"x" * 6, b"x" * 6], path="/sub/"))[0]) == TOO_LARGE


@pytest.mark.anyio
@pytest.mark.parametrize("middleware", [[], [Middleware(Passthrough)], [Middleware(CatchAll)]])
async def test_swallowed_error_is_still_a_413_across_tasks(middleware: list[Middleware]) -> None:
    # Cause: the 413 does not depend on which task read the body and which one answers.
    app = Starlette(routes=[Route("/", swallowing, methods=["POST"])], middleware=middleware, max_body_size=3)
    assert status_and_body((await asgi_request(app, [b"x", b"x" * 3]))[0]) == TOO_LARGE
    route = Route("/", swallowing, methods=["POST"], middleware=middleware)
    app = Starlette(routes=[route], max_body_size=3)
    assert status_and_body((await asgi_request(app, [b"x", b"x" * 3]))[0]) == TOO_LARGE


@pytest.mark.anyio
async def test_error_handling_middleware_gets_a_413_when_the_endpoint_reads() -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(CatchAll)], max_body_size=10
    )
    assert status_and_body((await asgi_request(app, [b"x" * 6, b"x" * 6]))[0]) == TOO_LARGE


@pytest.mark.anyio
async def test_endpoint_answering_from_a_task_group_is_replaced() -> None:
    async def endpoint(request: Request) -> Response:
        async with anyio.create_task_group() as tg:

            async def read() -> None:
                try:
                    await request.body()
                except Exception:
                    pass

            tg.start_soon(read)
        return PlainTextResponse("handled")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    assert status_and_body((await asgi_request(app, [b"x", b"x" * 3]))[0]) == TOO_LARGE


@pytest.mark.anyio
@pytest.mark.parametrize(
    "wrap",
    [
        lambda router: router,
        lambda router: CORSMiddleware(router, allow_origins=["*"]),
        lambda router: GZipMiddleware(CORSMiddleware(router, allow_origins=["*"])),
    ],
)
async def test_inner_router_behind_middleware_can_raise_the_limit(wrap: Any) -> None:
    # Cause: a mount looks through the middleware that wraps a router to know it will settle the limit.
    inner = wrap(Router([Route("/", read_body, methods=["POST"])], max_body_size=100))
    app = Starlette(routes=[Mount("/r", app=inner)], max_body_size=10)
    for headers in ([(b"content-length", b"50")], []):
        assert status_and_body((await asgi_request(app, [b"x" * 50], headers, path="/r/"))[0]) == (200, b"50")
    assert (
        status_and_body((await asgi_request(app, [b"x" * 101], [(b"content-length", b"101")], path="/r/"))[0])
        == TOO_LARGE
    )


@pytest.mark.anyio
async def test_route_as_mounted_app_can_raise_the_limit() -> None:
    inner = Route("/", read_body, methods=["POST"], max_body_size=100)
    app = Starlette(routes=[Mount("/r", app=inner)], max_body_size=10)
    assert status_and_body((await asgi_request(app, [b"x" * 50], [(b"content-length", b"50")], path="/r/"))[0]) == (
        200,
        b"50",
    )


@pytest.mark.anyio
async def test_no_route_or_method_inside_a_raising_mount_uses_the_application_limit() -> None:
    # Cause: the application's limit is kept apart from the one a mount or router has since set.
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)], max_body_size=10
    )
    cl = [(b"content-length", b"50")]
    for method, path in [("PUT", "/m/"), ("POST", "/m/missing"), ("POST", "/m")]:
        assert status_and_body((await asgi_request(app, [b"x" * 50], cl, method=method, path=path))[0]) == TOO_LARGE
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, path="/m/"))[0]) == (200, b"50")
    # Without an application limit a no-match request is not limited, however low the mount is.
    app = Starlette(routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)])
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, method="PUT", path="/m/"))[0])[0] == 405


@pytest.mark.anyio
async def test_body_already_read_by_a_middleware_does_not_hang() -> None:
    # Cause: nothing reads from the server to settle the limit; only the declared length is looked at.
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(LogBody)], max_body_size=100
    )
    with anyio.fail_after(5):
        sent, pulled = await asgi_request(app, [b"x" * 5, b"x" * 5])
    assert status_and_body(sent) == (200, b"10")
    assert pulled == 2


@pytest.mark.anyio
async def test_get_with_a_body_reading_middleware_does_not_hang() -> None:
    app = Starlette(routes=[Route("/", read_body)], middleware=[Middleware(LogBody)], max_body_size=100)
    with anyio.fail_after(5):
        sent, _ = await asgi_request(app, [b""], method="GET")
    assert status_and_body(sent) == (200, b"0")


@pytest.mark.parametrize("bad", [-1, "10", 1.5, True])
def test_invalid_max_body_size_is_rejected_at_construction(bad: Any) -> None:
    error = ValueError if bad == -1 and bad is not True else TypeError
    for build in (
        lambda: Starlette(max_body_size=bad),
        lambda: Router(max_body_size=bad),
        lambda: Mount("/", routes=[], max_body_size=bad),
        lambda: Route("/", read_body, max_body_size=bad),
        lambda: RequestBodyLimitMiddleware(read_body, max_body_size=bad),  # type: ignore[arg-type]
    ):
        with pytest.raises(error):
            build()


@pytest.mark.anyio
async def test_middleware_without_a_limit_lets_everything_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Response("ok")(scope, receive, send)

    sent, _ = await asgi_request(RequestBodyLimitMiddleware(app, max_body_size=None), [b"x" * 50])
    assert status_and_body(sent) == (200, b"ok")


@pytest.mark.anyio
async def test_declared_length_over_limit_is_rejected_before_a_plain_app_under_mount_or_host() -> None:
    async def plain(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("not reached")  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=plain), Host("testserver", app=plain)], max_body_size=10)
    cl = [(b"content-length", b"11")]
    assert status_and_body((await asgi_request(app, [b"x"], cl, path="/m/"))[0]) == TOO_LARGE
    headers = cl + [(b"host", b"testserver")]
    assert status_and_body((await asgi_request(app, [b"x"], headers, path="/other"))[0]) == TOO_LARGE


def function_wrap(app: Any) -> Any:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)

    return wrapped


@pytest.mark.anyio
@pytest.mark.parametrize(
    "wrap",
    [function_wrap, lambda app: function_wrap(function_wrap(app)), lambda app: functools.partial(function_wrap(app))],
)
async def test_router_behind_function_wrappers_can_raise_the_limit(wrap: Any) -> None:
    inner = wrap(Router([Route("/", read_body, methods=["POST"])], max_body_size=100))
    app = Starlette(routes=[Mount("/r", app=inner)], max_body_size=10)
    cl = [(b"content-length", b"50")]
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, path="/r/"))[0]) == (200, b"50")
    assert status_and_body((await asgi_request(app, [b"x" * 50], path="/r/"))[0]) == (200, b"50")
    # A request that finds no route there still answers to the application's limit.
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, path="/r/nope"))[0]) == TOO_LARGE


@pytest.mark.anyio
async def test_unmatched_request_in_mounted_route_or_host_uses_the_application_limit() -> None:
    cl = [(b"content-length", b"50")]
    route = Route("/", read_body, methods=["POST"])
    app = Starlette(routes=[Mount("/r", app=route)], max_body_size=10)
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, path="/r/other"))[0]) == TOO_LARGE
    host = Host("other.example", app=Router([route]))
    app = Starlette(routes=[Mount("/r", app=host)], max_body_size=10)
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, path="/r/"))[0]) == TOO_LARGE
    # Without a limit on the application the same requests are simply not found.
    app = Starlette(routes=[Mount("/r", app=route)])
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, path="/r/other"))[0])[0] == 404


async def swallow_then_stream(request: Request) -> Response:
    try:
        await request.body()
    except Exception:
        pass

    async def gen() -> Any:
        yield b"handled"

    return StreamingResponse(gen())


@pytest.mark.anyio
@pytest.mark.parametrize("spec_version", [None, "2.3", "2.4"])
async def test_swallowed_error_then_streaming_response_is_a_413_whatever_the_spec(spec_version: str | None) -> None:
    # Cause: the 413 follows from the limit having been crossed before the response, not from
    # which task sends it, so it does not depend on how StreamingResponse is run.
    app = Starlette(routes=[Route("/", swallow_then_stream, methods=["POST"])], max_body_size=3)
    sent, _ = await asgi_request(app, [b"x", b"x" * 3], spec_version=spec_version)
    assert status_and_body(sent) == TOO_LARGE


@pytest.mark.anyio
async def test_router_behind_bound_method_wrapper_can_raise_the_limit() -> None:
    class Wrapper:
        def __init__(self, app: Any) -> None:
            self.target = app

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            await self.target(scope, receive, send)

    inner = Wrapper(Router([Route("/", read_body, methods=["POST"])], max_body_size=100)).handle
    app = Starlette(routes=[Mount("/r", app=inner)], max_body_size=10)
    cl = [(b"content-length", b"50")]
    assert status_and_body((await asgi_request(app, [b"x" * 50], cl, path="/r/"))[0]) == (200, b"50")


class SwallowThenSpawnedResponse:
    """An ASGI endpoint that swallows the refused read and answers from a task it spawns."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await Request(scope, receive).body()
        except Exception:
            pass
        async with anyio.create_task_group() as tg:
            tg.start_soon(Response("handled"), scope, receive, send)


@pytest.mark.anyio
@pytest.mark.parametrize("chunks", [[b"x", b"x" * 3], [b"x" * 4]])
async def test_swallowed_refusal_then_response_from_a_spawned_task_is_a_413(chunks: list[bytes]) -> None:
    # Cause: the 413 follows from the limit having been crossed before the response started,
    # not from which task sends the start.
    app = Starlette(routes=[Route("/", SwallowThenSpawnedResponse(), methods=["POST"])], max_body_size=3)
    assert status_and_body((await asgi_request(app, chunks))[0]) == TOO_LARGE


@pytest.mark.anyio
async def test_response_started_by_another_task_before_the_limit_is_crossed_stays() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async def answer() -> None:
            await Response("started")(scope, receive, send)

        async with anyio.create_task_group() as tg:
            tg.start_soon(answer)
            while (await receive()).get("more_body"):
                pass  # pragma: no cover

    sent, _ = await asgi_request(RequestBodyLimitMiddleware(app, max_body_size=3), [b"x" * 4])
    assert status_and_body(sent) == (200, b"started")
