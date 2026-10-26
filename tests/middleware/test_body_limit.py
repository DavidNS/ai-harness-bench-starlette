from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Host, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

pytestmark = pytest.mark.anyio


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


class Driver:
    """Drives an ASGI app by hand, so the body can be chunked and `Content-Length` chosen freely."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.sent: list[Message] = []
        self.receive_calls = 0
        self.app_ran = False

    async def request(
        self,
        chunks: Sequence[bytes],
        *,
        path: str = "/",
        method: str = "POST",
        content_length: int | None = None,
        headers: Sequence[tuple[bytes, bytes]] = (),
        body_after_response_start: bool = False,
    ) -> None:
        raw_headers = list(headers)
        if content_length is not None:
            raw_headers.append((b"content-length", str(content_length).encode()))
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
            "headers": raw_headers,
            "server": ("testserver", 80),
            "client": ("testclient", 5000),
        }
        pending = list(chunks)
        started = anyio.Event()
        if not body_after_response_start:
            started.set()

        async def receive() -> Message:
            await started.wait()
            self.receive_calls += 1
            if not pending:
                await (
                    anyio.sleep_forever()
                )  # pragma: no cover  # a real server only disconnects when the client goes away
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}

        async def send(message: Message) -> None:
            self.sent.append(message)
            started.set()

        await self.app(scope, receive, send)

    @property
    def status(self) -> int:
        status: int = next(m["status"] for m in self.sent if m["type"] == "http.response.start")
        return status

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


async def run(app: Any, chunks: Sequence[bytes], **kwargs: Any) -> Driver:
    driver = Driver(app)
    await driver.request(chunks, **kwargs)
    return driver


async def assert_too_large(app: Any, chunks: Sequence[bytes], **kwargs: Any) -> Driver:
    driver = await run(app, chunks, **kwargs)
    assert driver.status == 413
    assert driver.body == b"Content Too Large"
    return driver


async def assert_ok(app: Any, chunks: Sequence[bytes], **kwargs: Any) -> Driver:
    driver = await run(app, chunks, **kwargs)
    assert driver.status == 200
    assert driver.body == b"read %d" % sum(map(len, chunks))
    return driver


# -- Standalone middleware ---------------------------------------------------------------


async def raw_app(scope: Scope, receive: Receive, send: Send) -> None:
    request = Request(scope, receive, send)
    response = await read_body(request)
    await response(scope, receive, send)


async def test_middleware_allows_body_up_to_limit() -> None:
    app = RequestBodyLimitMiddleware(raw_app, max_body_size=10)
    await assert_ok(app, [b"12345", b"12345"])


async def test_middleware_rejects_streamed_body_without_content_length() -> None:
    app = RequestBodyLimitMiddleware(raw_app, max_body_size=10)
    await assert_too_large(app, [b"12345", b"123456"])


async def test_middleware_rejects_understated_content_length() -> None:
    app = RequestBodyLimitMiddleware(raw_app, max_body_size=10)
    await assert_too_large(app, [b"x" * 6, b"x" * 6], content_length=3)


async def test_middleware_overstated_content_length_within_limit_is_not_rejected() -> None:
    app = RequestBodyLimitMiddleware(raw_app, max_body_size=10)
    await assert_ok(app, [b"abc"], content_length=5)


async def test_middleware_content_length_fails_fast_on_entry() -> None:
    called = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True  # pragma: no cover

    app = RequestBodyLimitMiddleware(inner, max_body_size=10)
    driver = await assert_too_large(app, [b"x" * 11], content_length=11)
    assert not called
    assert driver.receive_calls == 0


async def test_middleware_ignores_malformed_content_length() -> None:
    app = RequestBodyLimitMiddleware(raw_app, max_body_size=10)
    await assert_ok(app, [b"abc"], headers=[(b"content-length", b"nonsense")])
    await assert_too_large(app, [b"x" * 11], headers=[(b"content-length", b"nonsense")])


async def test_middleware_zero_limit() -> None:
    app = RequestBodyLimitMiddleware(raw_app, max_body_size=0)
    await assert_ok(app, [b""])
    await assert_too_large(app, [b"x"])


async def test_middleware_no_body_request() -> None:
    app = RequestBodyLimitMiddleware(raw_app, max_body_size=0)
    await assert_ok(app, [b""], method="GET")


async def test_middleware_stops_body_stream_after_limit() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while True:
                await receive()
        except BaseException as exc:
            seen.append("first")
            with pytest.raises(type(exc)):
                await receive()
            seen.append("stopped")
            raise

    app = RequestBodyLimitMiddleware(inner, max_body_size=3)
    driver = await assert_too_large(app, [b"ab", b"cd", b"ef"])
    assert seen == ["first", "stopped"]
    assert driver.receive_calls == 2  # the third chunk is never pulled


async def test_middleware_no_second_response_when_response_started() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            await receive()

    app = RequestBodyLimitMiddleware(inner, max_body_size=3)
    driver = await run(app, [b"abcd"])
    assert [m["type"] for m in driver.sent] == ["http.response.start", "http.response.body"]
    assert driver.status == 200


async def test_middleware_ignores_websocket_and_lifespan() -> None:
    types_seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        types_seen.append(scope["type"])

    app = RequestBodyLimitMiddleware(inner, max_body_size=0)
    for kind in ("websocket", "lifespan"):
        await app({"type": kind, "headers": [(b"content-length", b"100")]}, None, None)  # type: ignore[arg-type]
    assert types_seen == ["websocket", "lifespan"]


def test_negative_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(raw_app, max_body_size=-1)
    with pytest.raises(ValueError):
        Starlette(max_body_size=-1)
    with pytest.raises(ValueError):
        Route("/", read_body, max_body_size=-1)
    with pytest.raises(ValueError):
        Router(max_body_size=-1)
    with pytest.raises(ValueError):
        Mount("/", routes=[], max_body_size=-1)


async def test_middleware_inside_starlette_is_a_hard_cap() -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    await assert_ok(app, [b"x" * 10])
    await assert_too_large(app, [b"x" * 11])
    await assert_too_large(app, [b"x"], content_length=11)


async def test_middleware_inside_limited_starlette_app_keeps_both_limits() -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=100)],
        max_body_size=5,
    )
    await assert_ok(app, [b"x" * 5])
    await assert_too_large(app, [b"x" * 8])
    await assert_too_large(app, [b"x"], content_length=8)


# -- Starlette application ---------------------------------------------------------------


async def test_app_limit() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    await assert_ok(app, [b"x" * 10])
    await assert_too_large(app, [b"x" * 5, b"x" * 6])


async def test_app_default_is_unlimited() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    await assert_ok(app, [b"x" * 100_000])


def test_app_limit_with_test_client(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_app_limit_streamed_upload(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def gen() -> Any:
        for _ in range(5):
            yield b"abc"

    assert client.post("/", content=gen()).status_code == 413


async def test_app_limit_counts_body_read_by_middleware() -> None:
    class ReadingMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            await request.body()
            return await call_next(request)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(ReadingMiddleware)],
        max_body_size=10,
    )
    await assert_too_large(app, [b"x" * 11])


async def test_app_limit_through_base_http_middleware() -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=10,
    )
    await assert_ok(app, [b"x" * 10])
    await assert_too_large(app, [b"x" * 11])


async def test_app_limit_unaffected_for_requests_without_body() -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    driver = await run(app, [b""], method="GET")
    assert driver.status == 200


async def test_app_limit_started_response_gets_no_second_response() -> None:
    async def stream(request: Request) -> Response:
        async def gen() -> Any:
            yield b"first"
            await request.body()
            yield b"never"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", stream, methods=["POST"])], max_body_size=3)
    driver = await run(app, [b"abcd", b"ef"], body_after_response_start=True)
    assert driver.status == 200
    assert driver.body == b"first"


# -- Content-Length fail-fast inside a Starlette application ----------------------------------


class Probe:
    """Records whether application code (middleware / endpoint) ran."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def endpoint(self, request: Request) -> Response:
        self.ran.append("endpoint")
        return PlainTextResponse("ok")

    def middleware(self, app: Any) -> Any:
        async def mw(scope: Scope, receive: Receive, send: Send) -> None:
            self.ran.append("middleware")
            await app(scope, receive, send)

        return mw


async def test_content_length_waits_for_routing_so_inner_route_can_raise_limit() -> None:
    probe = Probe()
    app = Starlette(
        routes=[Route("/big", probe.endpoint, methods=["POST"], max_body_size=100)],
        max_body_size=10,
    )
    driver = await run(app, [b"x" * 50], path="/big", content_length=50)
    assert driver.status == 200
    assert probe.ran == ["endpoint"]


async def test_content_length_rejected_before_endpoint_runs() -> None:
    probe = Probe()
    app = Starlette(routes=[Route("/", probe.endpoint, methods=["POST"])], max_body_size=10)
    driver = await assert_too_large(app, [b"x" * 11], content_length=11)
    assert probe.ran == []
    assert driver.receive_calls == 0


async def test_content_length_rejected_before_route_middleware_runs() -> None:
    probe = Probe()
    app = Starlette(
        routes=[Route("/", probe.endpoint, methods=["POST"], middleware=[Middleware(probe.middleware)])],
        max_body_size=10,
    )
    await assert_too_large(app, [b"x"], content_length=11)
    assert probe.ran == []


async def test_content_length_over_route_limit_rejected_even_if_app_limit_is_larger() -> None:
    probe = Probe()
    app = Starlette(
        routes=[Route("/small", probe.endpoint, methods=["POST"], max_body_size=5)],
        max_body_size=100,
    )
    await assert_too_large(app, [b"x"], path="/small", content_length=6)
    assert probe.ran == []


async def test_content_length_not_found_uses_app_limit() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    await assert_too_large(app, [b"x"], path="/missing", content_length=11)
    driver = await run(app, [b"x"], path="/missing", content_length=5)
    assert driver.status == 404


async def test_content_length_method_not_allowed_uses_app_limit() -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)],
        max_body_size=10,
    )
    await assert_too_large(app, [b"x"], method="PUT", content_length=11)
    driver = await run(app, [b"x"], method="PUT", content_length=5)
    assert driver.status == 405


async def test_content_length_redirect_uses_app_limit() -> None:
    app = Starlette(routes=[Route("/path/", read_body, methods=["POST"])], max_body_size=10)
    await assert_too_large(app, [b"x"], path="/path", content_length=11)
    driver = await run(app, [b"x"], path="/path", content_length=5)
    assert driver.status == 307


async def test_content_length_user_middleware_runs_before_routing_decides() -> None:
    probe = Probe()
    app = Starlette(
        routes=[Route("/", probe.endpoint, methods=["POST"])],
        middleware=[Middleware(probe.middleware)],
        max_body_size=10,
    )
    await assert_too_large(app, [b"x"], content_length=11)
    assert probe.ran == ["middleware"]


# -- Router ------------------------------------------------------------------------------


async def test_router_limit() -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=10)
    await assert_ok(router, [b"x" * 10])
    await assert_too_large(router, [b"x" * 11])
    await assert_too_large(router, [b"x"], content_length=11)


async def test_router_limit_without_matching_route() -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=10)
    await assert_too_large(router, [b"x"], path="/missing", content_length=11)


async def test_router_limit_overrides_app_limit_in_both_directions() -> None:
    inner_high = Router([Route("/", read_body, methods=["POST"])], max_body_size=100)
    inner_low = Router([Route("/", read_body, methods=["POST"])], max_body_size=5)
    app = Starlette(
        routes=[Mount("/high", app=inner_high), Mount("/low", app=inner_low), Route("/", read_body, methods=["POST"])],
        max_body_size=10,
    )
    await assert_ok(app, [b"x" * 50], path="/high/", content_length=50)
    await assert_too_large(app, [b"x" * 11], path="/low/")
    await assert_too_large(app, [b"x"], path="/low/", content_length=6)
    await assert_ok(app, [b"x" * 5], path="/low/")
    await assert_too_large(app, [b"x" * 11])


async def test_router_without_app_limit_applies_to_whole_router_only() -> None:
    router = Router(
        [
            Mount("/inner", routes=[Route("/", read_body, methods=["POST"])]),
        ],
        max_body_size=10,
    )
    await assert_too_large(router, [b"x" * 11], path="/inner/")


# -- Mount -------------------------------------------------------------------------------


async def test_mount_limit_routes() -> None:
    app = Starlette(routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)])
    await assert_ok(app, [b"x" * 10], path="/m/")
    await assert_too_large(app, [b"x" * 11], path="/m/")
    await assert_too_large(app, [b"x"], path="/m/", content_length=11)


async def test_mount_limit_asgi_app() -> None:
    app = Starlette(routes=[Mount("/m", app=raw_app, max_body_size=10)])
    await assert_ok(app, [b"x" * 10], path="/m/")
    await assert_too_large(app, [b"x" * 11], path="/m/")
    await assert_too_large(app, [b"x"], path="/m/", content_length=11)


async def test_mount_limit_content_length_fails_fast_before_mounted_app() -> None:
    probe = Probe()

    async def mounted(scope: Scope, receive: Receive, send: Send) -> None:
        probe.ran.append("app")  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=mounted, max_body_size=10)])
    await assert_too_large(app, [b"x"], path="/m/", content_length=11)
    assert probe.ran == []


async def test_mount_without_limit_uses_app_limit() -> None:
    app = Starlette(routes=[Mount("/m", app=raw_app)], max_body_size=10)
    await assert_too_large(app, [b"x"], path="/m/", content_length=11)
    await assert_too_large(app, [b"x" * 11], path="/m/")


async def test_mount_raises_and_lowers_app_limit() -> None:
    app = Starlette(
        routes=[
            Mount("/high", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount("/low", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ],
        max_body_size=10,
    )
    await assert_ok(app, [b"x" * 50], path="/high/", content_length=50)
    await assert_too_large(app, [b"x" * 6], path="/low/")
    await assert_too_large(app, [b"x"], path="/low/", content_length=6)


async def test_mount_limit_with_unlimited_app() -> None:
    app = Starlette(routes=[Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=3)])
    await assert_too_large(app, [b"x" * 4], path="/m/")


async def test_nested_starlette_app_limit_overrides_outer() -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10)
    await assert_ok(app, [b"x" * 50], path="/inner/", content_length=50)


# -- Route -------------------------------------------------------------------------------


async def test_route_limit_without_app_limit() -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_body, methods=["POST"], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    await assert_ok(app, [b"x" * 5], path="/limited")
    await assert_too_large(app, [b"x" * 6], path="/limited")
    await assert_too_large(app, [b"x"], path="/limited", content_length=6)
    await assert_ok(app, [b"x" * 1000], path="/free")


async def test_route_limit_raises_app_limit() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=10)
    await assert_ok(app, [b"x" * 100])
    await assert_too_large(app, [b"x" * 101])


async def test_route_limit_lowers_app_limit() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=5)], max_body_size=100)
    await assert_ok(app, [b"x" * 5])
    await assert_too_large(app, [b"x" * 6])


async def test_route_limit_overrides_mount_limit() -> None:
    app = Starlette(
        routes=[
            Mount(
                "/m",
                routes=[
                    Route("/up", read_body, methods=["POST"], max_body_size=100),
                    Route("/down", read_body, methods=["POST"], max_body_size=2),
                    Route("/same", read_body, methods=["POST"]),
                ],
                max_body_size=10,
            )
        ],
    )
    await assert_ok(app, [b"x" * 50], path="/m/up", content_length=50)
    await assert_too_large(app, [b"x" * 3], path="/m/down")
    await assert_too_large(app, [b"x" * 11], path="/m/same")
    await assert_ok(app, [b"x" * 10], path="/m/same")


async def test_route_limit_lower_than_bytes_already_read() -> None:
    async def prefetch(scope: Scope, receive: Receive, send: Send) -> None:
        # something outside the limited route already consumed part of the body
        await receive()
        await Route("/", read_body, methods=["POST"], max_body_size=3)(scope, receive, send)

    app = Starlette(routes=[Mount("/", app=prefetch)], max_body_size=100)
    await assert_too_large(app, [b"x" * 10, b""])


async def test_route_standalone_limit() -> None:
    route = Route("/", read_body, methods=["POST"], max_body_size=5)
    await assert_ok(route, [b"x" * 5])
    await assert_too_large(route, [b"x" * 6])
    await assert_too_large(route, [b"x"], content_length=6)


async def test_route_class_endpoint_limit() -> None:
    class Endpoint:
        def __init__(self, scope: Scope, receive: Receive, send: Send) -> None:
            self.args = (scope, receive, send)

        def __await__(self) -> Any:
            return raw_app(*self.args).__await__()

    app = Starlette(routes=[Route("/", Endpoint, methods=["POST"], max_body_size=5)])
    await assert_too_large(app, [b"x" * 6])


# -- Multipart ---------------------------------------------------------------------------


def multipart_body(file_size: int) -> tuple[bytes, str]:
    boundary = "xxBOUNDARYxx"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="a.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + b"a" * file_size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


async def read_form(request: Request) -> Response:
    async with request.form() as form:
        upload = form["file"]
        return PlainTextResponse(f"file {len(await upload.read())}")  # type: ignore[union-attr]


def test_multipart_limit_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    body, content_type = multipart_body(100)
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=len(body))
    client = test_client_factory(app)
    response = client.post("/", content=body, headers={"content-type": content_type})
    assert response.text == "file 100"

    # The file data alone fits in the limit, but file data plus multipart overhead does not.
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=len(body) - 1)
    client = test_client_factory(app)
    response = client.post("/", content=body, headers={"content-type": content_type})
    assert response.status_code == 413
    assert response.text == "Content Too Large"


async def test_multipart_limit_without_content_length() -> None:
    body, content_type = multipart_body(100)
    chunks = [body[i : i + 40] for i in range(0, len(body), 40)]
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=len(body) - 1)
    await assert_too_large(app, chunks, headers=[(b"content-type", content_type.encode())])


async def test_multipart_temp_files_closed_when_body_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    from tempfile import SpooledTemporaryFile

    created: list[SpooledTemporaryFile[bytes]] = []

    class TrackedFile(SpooledTemporaryFile):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", TrackedFile)

    body, content_type = multipart_body(100)
    chunks = [body[i : i + 40] for i in range(0, len(body), 40)]
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=len(body) - 10)
    await assert_too_large(app, chunks, headers=[(b"content-type", content_type.encode())])
    assert len(created) == 1
    assert created[0].closed


async def test_multipart_temp_files_closed_on_any_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    from tempfile import SpooledTemporaryFile

    from starlette.formparsers import MultiPartParser

    created: list[SpooledTemporaryFile[bytes]] = []

    class TrackedFile(SpooledTemporaryFile):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", TrackedFile)

    class Interrupted(BaseException):
        pass

    body, content_type = multipart_body(10)
    split = body.index(b"aaaa") + 4

    async def stream() -> Any:
        yield body[:split]
        raise Interrupted()

    from starlette.datastructures import Headers

    parser = MultiPartParser(Headers({"content-type": content_type}), stream())
    with pytest.raises(Interrupted):
        await parser.parse()
    assert len(created) == 1
    assert created[0].closed


# -- Layering with standalone middleware, hosts and mounted applications --------------------


def post_route(**kwargs: Any) -> Route:
    return Route("/", read_body, methods=["POST"], **kwargs)


async def test_route_lowers_limit_of_user_middleware() -> None:
    app = Starlette(
        routes=[post_route(max_body_size=5)], middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=100)]
    )
    await assert_ok(app, [b"x" * 5])
    await assert_too_large(app, [b"x" * 50])
    await assert_too_large(app, [b"x"], content_length=50)


async def test_app_lowers_limit_of_wrapping_middleware() -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[post_route()], max_body_size=5), max_body_size=100)
    await assert_ok(app, [b"x" * 5])
    await assert_too_large(app, [b"x" * 50])
    await assert_too_large(app, [b"x" * 50], content_length=50)


async def test_mount_lowers_limit_of_wrapping_middleware() -> None:
    inner = Starlette(routes=[Mount("/m", routes=[post_route()], max_body_size=5)])
    app = RequestBodyLimitMiddleware(inner, max_body_size=100)
    await assert_too_large(app, [b"x" * 50], path="/m/")


async def test_wrapping_middleware_is_a_ceiling_for_inner_limits() -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[post_route(max_body_size=500)]), max_body_size=100)
    await assert_ok(app, [b"x" * 100])
    await assert_too_large(app, [b"x" * 101])


async def test_route_raises_app_limit_under_user_middleware() -> None:
    app = Starlette(
        routes=[post_route(max_body_size=50)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=100)],
        max_body_size=5,
    )
    await assert_ok(app, [b"x" * 20], content_length=20)


async def test_route_raises_limit_under_host() -> None:
    app = Starlette(routes=[Host("testserver", app=Router([post_route(max_body_size=100)]))], max_body_size=10)
    headers = [(b"host", b"testserver")]
    await assert_ok(app, [b"x" * 50], content_length=50, headers=headers)
    await assert_too_large(app, [b"x" * 150], content_length=150, headers=headers)


async def test_mount_raises_limit_under_host() -> None:
    inner = Router([Mount("/m", routes=[post_route()], max_body_size=100)])
    app = Starlette(routes=[Host("testserver", app=inner)], max_body_size=10)
    await assert_ok(app, [b"x" * 50], path="/m/", content_length=50, headers=[(b"host", b"testserver")])


async def test_host_with_asgi_app_fails_fast_on_app_limit() -> None:
    ran = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append(1)  # pragma: no cover

    app = Starlette(routes=[Host("testserver", app=asgi)], max_body_size=10)
    await assert_too_large(app, [b"x"], content_length=50, headers=[(b"host", b"testserver")])
    assert ran == []


async def test_route_raises_limit_in_mounted_application_without_own_limit() -> None:
    inner = Starlette(routes=[post_route(max_body_size=100)])
    app = Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10)
    await assert_ok(app, [b"x" * 50], path="/inner/", content_length=50)


async def test_route_raises_mount_limit_in_mounted_application() -> None:
    inner = Starlette(routes=[post_route(max_body_size=100)])
    app = Starlette(routes=[Mount("/inner", app=inner, max_body_size=10)])
    await assert_ok(app, [b"x" * 50], path="/inner/", content_length=50)


async def test_mounted_application_rejection_is_a_413_not_a_500() -> None:
    inner = Starlette(routes=[post_route()])
    app = Starlette(routes=[Mount("/sub", app=inner)], max_body_size=10)
    await assert_too_large(app, [b"x" * 6, b"x" * 6], path="/sub/")
    await assert_too_large(app, [b"x"], path="/sub/", content_length=50)


async def test_mounted_application_raising_limit_rejection_is_a_413() -> None:
    inner = Starlette(routes=[post_route()], max_body_size=100)
    app = Starlette(routes=[Mount("/sub", app=inner)], max_body_size=10)
    await assert_too_large(app, [b"x" * 60, b"x" * 60], path="/sub/")
    await assert_too_large(app, [b"x"], path="/sub/", content_length=150)


async def test_wrapping_middleware_around_starlette_gives_413() -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[post_route()]), max_body_size=10)
    await assert_too_large(app, [b"x" * 6, b"x" * 6])


async def test_single_read_app_never_gets_a_truncated_body() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        await PlainTextResponse(f"read {len(message.get('body', b''))}")(scope, receive, send)

    app = RequestBodyLimitMiddleware(inner, max_body_size=10)
    await assert_too_large(app, [b"x" * 11], content_length=3)


async def test_partial_reader_never_gets_a_truncated_body() -> None:
    async def sniff(request: Request) -> Response:
        async for chunk in request.stream():
            return PlainTextResponse(f"first {len(chunk)}")
        raise AssertionError  # pragma: no cover

    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", sniff, methods=["POST"])]), max_body_size=10)
    await assert_too_large(app, [b"x" * 20])


async def test_streaming_response_not_reading_body_is_not_cut_by_the_limit() -> None:
    async def stream(request: Request) -> Response:
        async def gen() -> Any:
            for _ in range(5):
                yield b"chunk"
                await anyio.sleep(0.01)

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", stream, methods=["POST"])], max_body_size=3)
    driver = Driver(app)
    scope_headers: list[tuple[bytes, bytes]] = []
    started = anyio.Event()
    pending = [b"ab", b"cd", b"ef"]

    async def receive() -> Message:
        await started.wait()
        if not pending:
            await anyio.sleep_forever()  # pragma: no cover
        return {"type": "http.request", "body": pending.pop(0), "more_body": bool(pending)}

    async def send(message: Message) -> None:
        driver.sent.append(message)
        started.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": scope_headers,
        "server": ("t", 80),
        "client": ("c", 1),
    }
    with anyio.fail_after(5):
        await app(scope, receive, send)
    assert driver.status == 200
    assert driver.body == b"chunk" * 5


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.5, True, "10"])
def test_non_integer_limit_is_rejected(value: Any) -> None:
    with pytest.raises(TypeError):
        Starlette(max_body_size=value)
    with pytest.raises(TypeError):
        RequestBodyLimitMiddleware(raw_app, max_body_size=value)


def test_multipart_temp_files_closed_when_one_message_crosses_limit(
    test_client_factory: TestClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    created: list[Any] = []
    orig = tempfile.SpooledTemporaryFile.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "__init__", init)

    async def endpoint(request: Request) -> Response:
        await request.form()  # never closed by the endpoint
        return PlainTextResponse("ok")  # pragma: no cover

    head = b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n'
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3000)

    def body() -> Any:  # streamed, so no Content-Length is declared
        yield head + b"x" * 5000 + b"\r\n--B--\r\n"

    response = test_client_factory(app).post(
        "/", content=body(), headers={"content-type": "multipart/form-data; boundary=B"}
    )
    assert response.status_code == 413
    assert created
    assert all(f.closed for f in created)


# -- Wrapped inner routers, huge Content-Length, disconnects, truncation ---------------------


async def test_route_raises_limit_in_mount_of_middleware_wrapped_router() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = GZipMiddleware(Router([post_route(max_body_size=100)]))
    app = Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10)
    await assert_ok(app, [b"x" * 50], path="/inner/", content_length=50)


async def test_route_raises_limit_in_host_of_middleware_wrapped_starlette() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = GZipMiddleware(Starlette(routes=[post_route(max_body_size=100)]))
    app = Starlette(routes=[Host("testserver", app=inner)], max_body_size=10)
    await assert_ok(app, [b"x" * 50], content_length=50, headers=[(b"host", b"testserver")])


@pytest.mark.parametrize("wrap", [False, True])
async def test_huge_content_length_is_rejected(wrap: bool) -> None:
    app: Any = Starlette(routes=[post_route()], max_body_size=10)
    if wrap:
        app = RequestBodyLimitMiddleware(app, max_body_size=10)
    await assert_too_large(app, [b"x"], headers=[(b"content-length", b"9" * 5000)])


async def test_stream_never_yields_bytes_beyond_limit() -> None:
    stored: list[bytes] = []

    async def upload(request: Request) -> Response:
        async for chunk in request.stream():
            stored.append(chunk)
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=10)
    await assert_too_large(app, [b"x" * 100_000])
    assert sum(map(len, stored)) <= 10


@pytest.mark.parametrize("wrap", [False, True])
async def test_endless_stream_ends_when_client_disconnects_after_oversized_body(wrap: bool) -> None:
    async def events(request: Request) -> Response:
        async def gen() -> Any:
            while True:  # pragma: no branch
                yield b"tick"
                await anyio.sleep(0.01)

        return StreamingResponse(gen())

    app: Any = Starlette(routes=[Route("/", events, methods=["POST"])], max_body_size=None if wrap else 3)
    if wrap:
        app = RequestBodyLimitMiddleware(app, max_body_size=3)
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"abcdef", "more_body": False}
        await anyio.sleep(0.1)
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "server": ("t", 80),
        "client": ("c", 1),
    }
    with anyio.fail_after(3):
        await app(scope, receive, send)
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


async def test_base_http_middleware_still_wraps_receive_errors() -> None:
    class Boom(Exception): ...

    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            return await call_next(request)

    seen: list[str] = []

    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except BaseException as exc:
            seen.append(type(exc).__name__)
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], middleware=[Middleware(Passthrough)])

    async def receive() -> Message:
        raise Boom

    async def send(message: Message) -> None: ...

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("t", 80),
        "client": ("c", 1),
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
    }
    await app(scope, receive, send)
    assert seen == ["ExceptionGroup"]
