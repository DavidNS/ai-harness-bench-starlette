from __future__ import annotations

from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Host, Mount, Route, Router
from starlette.testclient import TestClient
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
    headers: list[tuple[bytes, bytes]] | None = None,
    path: str = "/",
    method: str = "POST",
) -> tuple[list[Message], int]:
    """Drive an app by hand: chunks are sent as-is, with only the given headers. Returns (sent, chunks consumed)."""
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }
    queue = list(chunks)
    consumed = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal consumed
        if not queue:
            await anyio.sleep_forever()  # pragma: no cover
        consumed += 1
        chunk = queue.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(queue)}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent, consumed


def status_and_body(sent: list[Message]) -> tuple[int, bytes]:
    start = [m for m in sent if m["type"] == "http.response.start"]
    assert len(start) == 1
    return start[0]["status"], b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit_applies_to_unrouted_requests(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/missing", content=b"x" * 11).status_code == 413
    assert client.post("/missing", content=b"x").status_code == 404
    assert client.put("/", content=b"x" * 11).status_code == 413
    assert client.put("/", content=b"x").status_code == 405


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).status_code == 200
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert client.post("/nope", content=b"x" * 11).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/small", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small/", content=b"x" * 10).status_code == 200
    assert client.post("/small/", content=b"x" * 11).status_code == 413
    assert client.post("/free", content=b"x" * 1000).status_code == 200


def test_mount_limit_on_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    app = Mount("/m", app=PlainTextResponse("ok"), max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/m/", content=b"xxxxx").status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    route = Route("/", read_body, methods=["POST"], max_body_size=10)
    client = test_client_factory(route)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).text == "Content Too Large"


def test_inner_limits_raise_and_lower_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/default", read_body, methods=["POST"]),
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=3),
            Mount("/mraised", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount(
                "/nested",
                routes=[Mount("/deep", routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)],
                max_body_size=5,
            ),
            Mount(
                "/router",
                app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)

    def post(path: str, size: int) -> int:
        return client.post(path, content=b"x" * size).status_code

    assert (post("/default", 10), post("/default", 11)) == (200, 413)
    assert (post("/raised", 50), post("/raised", 101)) == (200, 413)
    assert (post("/lowered", 3), post("/lowered", 4)) == (200, 413)
    assert (post("/mraised/", 50), post("/mraised/", 101)) == (200, 413)
    assert (post("/nested/deep/", 50), post("/nested/deep/", 51)) == (200, 413)
    assert (post("/router/", 100), post("/router/", 101)) == (200, 413)


def test_content_length_fails_fast_after_routing_settled() -> None:
    app = Starlette(
        routes=[
            Route("/default", read_body, methods=["POST"]),
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
        ],
        max_body_size=10,
    )
    headers = [(b"content-length", b"50")]

    # Above the application limit but below the route's: not rejected up front.
    sent, _ = anyio.run(lambda: run_asgi(app, [b"x" * 50], headers=headers, path="/raised"))
    assert status_and_body(sent) == (200, b"read 50")

    # Rejected without reading anything, and before the endpoint runs.
    calls.clear()
    sent, consumed = anyio.run(lambda: run_asgi(app, [b"x" * 50], headers=headers, path="/default"))
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert consumed == 0 and calls == []

    # Unrouted, and wrong method: the application's limit.
    for path, method in [("/missing", "POST"), ("/default", "PUT")]:
        sent, consumed = anyio.run(lambda: run_asgi(app, [b"x" * 50], headers=headers, path=path, method=method))
        assert status_and_body(sent) == (413, b"Content Too Large")
        assert consumed == 0


def test_content_length_fail_fast_with_inner_lowering() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=3)], max_body_size=100)
    sent, consumed = anyio.run(lambda: run_asgi(app, [b"xxxx"], headers=[(b"content-length", b"4")]))
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert consumed == 0 and calls == []


def test_missing_or_understated_content_length_does_not_let_a_body_through() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    for headers in ([], [(b"content-length", b"2")], [(b"content-length", b"bogus")]):
        calls.clear()
        sent, consumed = anyio.run(lambda: run_asgi(app, [b"x" * 6, b"x" * 6, b"x" * 6], headers=headers))
        assert status_and_body(sent) == (413, b"Content Too Large")
        assert consumed == 2  # stopped at the chunk that crossed the limit


def test_response_already_started_gets_no_second_response() -> None:
    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("early")(scope, receive, send)
        while True:
            message = await receive()
            if not message.get("more_body"):  # pragma: no cover
                break

    app = Starlette(routes=[Mount("/", app=endpoint)], max_body_size=10)
    sent, consumed = anyio.run(lambda: run_asgi(app, [b"x" * 6, b"x" * 6, b"x" * 6]))
    assert status_and_body(sent) == (200, b"early")
    assert consumed == 2


def test_swallowed_limit_error_still_gives_413() -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:
            pass
        return PlainTextResponse("ignored")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    sent, _ = anyio.run(lambda: run_asgi(app, [b"x" * 11]))
    assert status_and_body(sent) == (413, b"Content Too Large")


def test_limit_through_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)  # type: ignore[no-any-return]

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413


def test_unaffected_requests(test_client_factory: TestClientFactory) -> None:
    async def ws(websocket: Any) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    started: list[bool] = []

    async def lifespan(app: Any) -> Any:
        started.append(True)
        yield

    from contextlib import asynccontextmanager

    app = Starlette(
        routes=[Route("/", read_body, methods=["GET"]), WebSocketRoute("/ws", ws)],
        lifespan=asynccontextmanager(lifespan),
        max_body_size=1,
    )
    with test_client_factory(app) as client:
        assert client.get("/").text == "read 0"
        with client.websocket_connect("/ws") as session:
            session.send_text("long message")
            assert session.receive_text() == "long message"
    assert started == [True]


async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    calls.append("echo")
    body = b""
    more = True
    while more:
        message = await receive()
        body += message.get("body", b"")
        more = message.get("more_body", False)
    await PlainTextResponse(f"got {len(body)}")(scope, receive, send)


def test_standalone_middleware(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(echo_app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "got 10"
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")


def test_standalone_middleware_rejects_declared_length_on_entry() -> None:
    app = RequestBodyLimitMiddleware(echo_app, max_body_size=10)
    sent, consumed = anyio.run(lambda: run_asgi(app, [b"x" * 50], headers=[(b"content-length", b"50")]))
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert consumed == 0 and calls == []


def test_standalone_middleware_counts_streamed_chunks() -> None:
    app = RequestBodyLimitMiddleware(echo_app, max_body_size=10)
    sent, consumed = anyio.run(lambda: run_asgi(app, [b"x" * 4] * 5, headers=[(b"content-length", b"1")]))
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert consumed == 3


def test_standalone_middleware_is_not_overridden_from_below() -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)])
    client = TestClient(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=b"x" * 10).status_code == 200


def test_standalone_middleware_started_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("early")(scope, receive, send)
        while (await receive()).get("more_body"):
            pass  # pragma: no cover

    sent, _ = anyio.run(lambda: run_asgi(RequestBodyLimitMiddleware(app, max_body_size=3), [b"xx", b"xx", b"xx"]))
    assert status_and_body(sent) == (200, b"early")


def test_standalone_middleware_passes_other_scopes() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestBodyLimitMiddleware(app, max_body_size=1)
    anyio.run(lambda: middleware({"type": "lifespan"}, None, None))  # type: ignore[arg-type]
    anyio.run(lambda: middleware({"type": "websocket"}, None, None))  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]


multipart_body = (
    b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n' + b"x" * 200 + b"\r\n--b--\r\n"
)


async def form_endpoint(request: Request) -> Response:
    async with request.form() as form:
        return PlainTextResponse(str(len(await form["f"].read())))  # type: ignore[union-attr]


def test_multipart_counts_encoding_overhead_and_file_data(test_client_factory: TestClientFactory) -> None:
    content_type = {"content-type": "multipart/form-data; boundary=b"}
    total = len(multipart_body)
    assert total > 200
    ok = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=total)
    assert test_client_factory(ok).post("/", content=multipart_body, headers=content_type).text == "200"
    tight = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=total - 1)
    assert test_client_factory(tight).post("/", content=multipart_body, headers=content_type).status_code == 413


def test_multipart_temp_files_closed_when_limit_crossed() -> None:
    from starlette.formparsers import MultiPartParser

    opened: list[Any] = []
    original = MultiPartParser.on_headers_finished

    def spy(self: MultiPartParser) -> None:
        original(self)
        opened.extend(self._files_to_close_on_error)

    MultiPartParser.on_headers_finished = spy  # type: ignore[method-assign]
    try:
        app = Starlette(routes=[Route("/", form_endpoint, methods=["POST"])], max_body_size=150)
        headers = [(b"content-type", b"multipart/form-data; boundary=b")]
        chunks = [multipart_body[i : i + 50] for i in range(0, len(multipart_body), 50)]
        sent, _ = anyio.run(lambda: run_asgi(app, chunks, headers=headers))
    finally:
        MultiPartParser.on_headers_finished = original  # type: ignore[method-assign]
    assert status_and_body(sent)[0] == 413
    assert opened and all(file.closed for file in opened)


def test_multipart_temp_files_closed_on_any_interruption() -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    async def stream() -> Any:
        yield multipart_body[:120]
        raise RuntimeError("boom")

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RuntimeError):
        anyio.run(parser.parse)
    assert parser._files_to_close_on_error and all(f.closed for f in parser._files_to_close_on_error)


def test_host_with_plain_app_settles_limit_at_the_host(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Host("testserver", app=echo_app)], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).text == "got 5"
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert calls == ["echo"]


def test_unrelated_error_after_limit_crossed_propagates() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        with pytest.raises(Exception):
            await receive()
            await receive()
        raise ValueError("unrelated")

    with pytest.raises(ValueError, match="unrelated"):
        anyio.run(lambda: run_asgi(RequestBodyLimitMiddleware(app, max_body_size=3), [b"xx", b"xx", b"xx"]))


def test_wrapped_limit_error_still_gives_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while True:
                await receive()
        except Exception as exc:
            raise RuntimeError("wrapped") from exc

    sent, _ = anyio.run(lambda: run_asgi(RequestBodyLimitMiddleware(app, max_body_size=3), [b"xx", b"xx"]))
    assert status_and_body(sent) == (413, b"Content Too Large")


def test_disconnect_messages_pass_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.disconnect"
        await PlainTextResponse("bye")(scope, receive, send)

    async def run() -> list[Message]:
        async def receive() -> Message:
            return {"type": "http.disconnect"}

        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {"type": "http", "method": "GET", "headers": []}
        await RequestBodyLimitMiddleware(app, max_body_size=1)(scope, receive, send)
        return sent

    assert status_and_body(anyio.run(run)) == (200, b"bye")


def test_wrong_method_uses_application_limit_for_content_length() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=10000)], max_body_size=10)
    sent, consumed = anyio.run(lambda: run_asgi(app, [b""], headers=[(b"content-length", b"500")], method="PUT"))
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert consumed == 0 and calls == []


def test_streaming_response_started_by_another_task_is_not_replaced() -> None:
    from starlette.responses import StreamingResponse

    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"first"
            await request.body()
            yield b"second"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=100)
    sent: list[Message] = []

    async def run() -> None:
        queue = [{"type": "http.request", "body": b"x" * 5000, "more_body": False}]

        async def receive() -> Message:
            if queue:
                return queue.pop(0)
            raise AssertionError  # pragma: no cover

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []}
        try:
            await app(scope, receive, send)
        except BaseException:  # pragma: no cover
            pass

    anyio.run(run)
    assert [m["type"] for m in sent].count("http.response.start") == 1
    assert sent[0]["status"] == 200
    assert b"Content Too Large" not in b"".join(m.get("body", b"") for m in sent)


def _status(app: Any, size: int, *, path: str = "/", method: str = "POST", declared: bool = True) -> int:
    headers = [(b"content-length", str(size).encode())] if declared else []
    sent, _ = anyio.run(lambda: run_asgi(app, [b"x" * size], headers=headers, path=path, method=method))
    return status_and_body(sent)[0]


def _post_only(**kwargs: Any) -> Route:
    return Route("/", read_body, methods=["POST"], **kwargs)


def test_unmatched_and_wrong_method_inside_mount_use_the_application_limit() -> None:
    raising = Starlette(routes=[Mount("/m", routes=[_post_only()], max_body_size=1000)], max_body_size=10)
    assert _status(raising, 500, path="/m/missing") == 413
    assert _status(raising, 500, path="/m/", method="PUT") == 413
    assert _status(raising, 500, path="/m/") == 200

    lowering = Starlette(routes=[Mount("/m", routes=[_post_only()], max_body_size=3)])
    assert _status(lowering, 10, path="/m/missing") == 404

    app = Starlette(routes=[Mount("/m", routes=[_post_only()], max_body_size=5)], max_body_size=10)
    assert _status(app, 8, path="/m/", method="PUT") == 405
    assert _status(app, 8, path="/m/") == 413


class Wrap:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def test_inner_limit_raises_through_wrapping_middleware_with_content_length() -> None:
    from starlette.middleware.cors import CORSMiddleware
    from starlette.middleware.gzip import GZipMiddleware

    router = Router(routes=[_post_only()], max_body_size=100)
    sub = Starlette(routes=[_post_only()], max_body_size=100)
    for inner in (Wrap(router), GZipMiddleware(router), CORSMiddleware(sub, allow_origins=["*"]), Wrap(Wrap(sub))):
        app = Starlette(routes=[Mount("/x", app=inner)], max_body_size=10)
        assert _status(app, 50, path="/x/") == 200
        assert _status(app, 101, path="/x/") == 413
    host_app = Starlette(routes=[Host("testserver", app=Wrap(router))], max_body_size=10)
    headers = [(b"host", b"testserver"), (b"content-length", b"50")]
    sent, _ = anyio.run(lambda: run_asgi(host_app, [b"x" * 50], headers=headers))
    assert status_and_body(sent)[0] == 200


def test_huge_content_length_is_rejected() -> None:
    headers = [(b"content-length", b"9" * 5000)]
    for app in (
        RequestBodyLimitMiddleware(echo_app, max_body_size=10),
        Starlette(routes=[_post_only()], max_body_size=10),
    ):
        sent, _ = anyio.run(lambda: run_asgi(app, [b"x"], headers=headers))
        assert status_and_body(sent)[0] == 413
    zeros = [(b"content-length", b"0" * 5000 + b"3")]
    sent, _ = anyio.run(lambda: run_asgi(Starlette(routes=[_post_only()], max_body_size=10), [b"xxx"], headers=zeros))
    assert status_and_body(sent) == (200, b"read 3")


def test_max_body_size_is_validated() -> None:
    for make in (
        lambda v: Starlette(max_body_size=v),
        lambda v: Router(max_body_size=v),
        lambda v: Mount("/", routes=[], max_body_size=v),
        lambda v: Route("/", read_body, max_body_size=v),
        lambda v: RequestBodyLimitMiddleware(echo_app, max_body_size=v),
    ):
        with pytest.raises(ValueError):
            make(-1)
        with pytest.raises(ValueError):
            make(True)
        make(0)
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    assert _status(app, 0, method="GET") == 200


def test_unrelated_chained_errors_after_limit() -> None:
    def deep(n: int) -> None:
        if n == 0:
            raise ValueError("bottom")
        try:
            deep(n - 1)
        except ValueError as exc:
            raise ValueError(n) from exc

    async def chained(scope: Scope, receive: Receive, send: Send) -> None:
        with pytest.raises(Exception):
            while True:
                await receive()
        deep(40)

    async def cyclic(scope: Scope, receive: Receive, send: Send) -> None:
        with pytest.raises(Exception):
            while True:
                await receive()
        exc = ValueError("x")
        raise exc from exc

    for app in (chained, cyclic):
        with pytest.raises(ValueError):
            anyio.run(lambda: run_asgi(RequestBodyLimitMiddleware(app, max_body_size=3), [b"xx", b"xx"]))


class Passthrough(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        return await call_next(request)  # type: ignore[no-any-return]


def test_swallowed_limit_error_from_any_task_still_gives_413() -> None:
    async def json_endpoint(request: Request) -> Response:
        try:
            await request.json()
        except Exception:
            return PlainTextResponse("bad", status_code=400)
        return PlainTextResponse("ok")  # pragma: no cover

    async def child_task_endpoint(request: Request) -> Response:
        failed = False

        async def read() -> None:
            nonlocal failed
            try:
                await request.body()
            except Exception:
                failed = True

        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
        return PlainTextResponse("bad", status_code=400 if failed else 200)

    chunks = [b"x" * 6, b"x" * 6]
    routes = [Route("/", json_endpoint, methods=["POST"])]
    child = [Route("/", child_task_endpoint, methods=["POST"])]
    apps = [
        Starlette(routes=routes, middleware=[Middleware(Passthrough)], max_body_size=10),
        Starlette(routes=child, max_body_size=10),
        RequestBodyLimitMiddleware(Starlette(routes=routes, middleware=[Middleware(Passthrough)]), max_body_size=10),
    ]
    for app in apps:
        sent, _ = anyio.run(lambda: run_asgi(app, chunks))
        assert status_and_body(sent) == (413, b"Content Too Large")


def test_stream_stops_when_started_response_app_swallows_the_error() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        try:
            while (await receive()).get("more_body"):
                pass  # pragma: no cover
        except Exception:
            pass
        await send({"type": "http.response.body", "body": b"b", "more_body": False})

    for wrapped in (
        RequestBodyLimitMiddleware(app, max_body_size=10),
        Starlette(routes=[Mount("/", app=app)], max_body_size=10),
    ):
        sent, _ = anyio.run(lambda: run_asgi(wrapped, [b"x" * 6] * 3))
        assert [m.get("body") for m in sent if m["type"] == "http.response.body"] == [b"a"]


def test_mounted_sub_application_keeps_the_outer_application_limit() -> None:
    inner = Starlette(routes=[_post_only(max_body_size=100)])
    app = Starlette(routes=[Mount("/x", app=Wrap(inner))], max_body_size=10)
    assert _status(app, 50, path="/x/") == 200
    assert _status(app, 50, path="/x/missing") == 413
    assert _status(app, 50, path="/x/", method="PUT") == 413
    assert _status(app, 5, path="/x/missing") == 404


def test_standalone_middleware_around_starlette_with_inner_limits() -> None:
    inner = Starlette(routes=[_post_only(max_body_size=3), Route("/free", read_body, methods=["POST"])])
    app = RequestBodyLimitMiddleware(inner, max_body_size=10)
    assert _status(app, 4) == 413  # lowered by the route
    assert _status(app, 3) == 200
    assert _status(app, 10, path="/free") == 200
    assert _status(app, 11, path="/free") == 413  # the middleware's limit is not raised from below


def test_standalone_middleware_around_starlette_gives_413_not_500() -> None:
    inner = Starlette(routes=[_post_only(max_body_size=100)])
    app = RequestBodyLimitMiddleware(inner, max_body_size=10)
    for headers in ([], [(b"content-length", b"50")]):
        sent, _ = anyio.run(lambda: run_asgi(app, [b"x" * 25, b"x" * 25], headers=headers))
        assert status_and_body(sent) == (413, b"Content Too Large")


def test_host_that_does_not_match_uses_the_application_limit() -> None:
    router = Router(routes=[_post_only()], max_body_size=100)
    app = Starlette(routes=[Host("testserver", app=Wrap(router))], max_body_size=10)
    other = [(b"host", b"other.example")]

    def status(size: int, headers: list[tuple[bytes, bytes]]) -> int:
        declared = [(b"content-length", str(size).encode())]
        sent, _ = anyio.run(lambda: run_asgi(app, [b"x" * size], headers=headers + declared))
        return status_and_body(sent)[0]

    assert status(50, other) == 413
    assert status(5, other) == 404
    assert status(50, [(b"host", b"testserver")]) == 200
