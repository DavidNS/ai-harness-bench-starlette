from __future__ import annotations

from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


async def asgi_request(
    app: Any,
    chunks: list[bytes],
    *,
    path: str = "/",
    method: str = "POST",
    content_length: int | None = None,
) -> list[Message]:
    """Call `app` directly so that the `Content-Length` header can be absent or wrong."""
    headers = [(b"host", b"testserver")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
        "http_version": "1.1",
    }
    pending = list(chunks)
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def status_and_body(messages: list[Message]) -> tuple[int, bytes]:
    start = [m for m in messages if m["type"] == "http.response.start"]
    assert len(start) == 1, messages
    return start[0]["status"], b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "read 10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read_body, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "read 100000"


def test_requests_without_body_are_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body)], max_body_size=0)
    assert test_client_factory(app).get("/").text == "read 0"


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router([Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100).status_code == 200
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=5), Route("/free", read_body, methods=["POST"])]
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert client.post("/free", content=b"x" * 600).status_code == 200


def test_inner_limits_raise_and_lower_the_outer_one(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=5),
            Route("/inherited", read_body, methods=["POST"]),
            Mount(
                "/mount",
                routes=[
                    Route("/raised", read_body, methods=["POST"], max_body_size=1000),
                    Route("/inherited", read_body, methods=["POST"]),
                ],
                max_body_size=50,
            ),
            Mount("/router", app=Router([Route("/", read_body, methods=["POST"])], max_body_size=200)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)

    def post(path: str, size: int) -> int:
        return client.post(path, content=b"x" * size).status_code

    assert (post("/raised", 100), post("/raised", 101)) == (200, 413)
    assert (post("/lowered", 5), post("/lowered", 6)) == (200, 413)
    assert (post("/inherited", 10), post("/inherited", 11)) == (200, 413)
    assert (post("/mount/inherited", 50), post("/mount/inherited", 51)) == (200, 413)
    assert (post("/mount/raised", 1000), post("/mount/raised", 1001)) == (200, 413)
    assert (post("/router/", 200), post("/router/", 201)) == (200, 413)


def test_unlimited_app_with_limited_route(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=3)])
    client = test_client_factory(app)
    assert client.post("/", content=b"xxx").status_code == 200
    assert client.post("/", content=b"xxxx").status_code == 413


@pytest.mark.anyio
async def test_unlimited_body_is_not_counted_for_routes_without_limit() -> None:
    sent = await asgi_request(Route("/", read_body, methods=["POST"]), [b"x" * 1000])
    assert status_and_body(sent) == (200, b"read 1000")


@pytest.mark.anyio
async def test_understated_or_absent_content_length_does_not_let_a_body_through() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    for content_length in (None, 3):
        sent = await asgi_request(app, [b"x" * 8, b"x" * 8], content_length=content_length)
        assert status_and_body(sent) == (413, b"Content Too Large")


@pytest.mark.anyio
async def test_content_length_fails_fast_before_the_application_runs() -> None:
    calls: list[str] = []

    async def endpoint(request: Request) -> Response:
        calls.append("endpoint")  # pragma: no cover
        return Response()  # pragma: no cover

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover

    app = Starlette(
        routes=[
            Route("/", endpoint, methods=["POST"]),
            Mount("/mount", app=asgi),
            Mount("/nested", routes=[Route("/", endpoint, methods=["POST"])]),
        ],
        max_body_size=10,
    )
    for path in ("/", "/mount/x", "/nested/", "/missing"):
        sent = await asgi_request(app, [], path=path, content_length=11)
        assert status_and_body(sent) == (413, b"Content Too Large"), path
    # A method that doesn't match gets the limit of the application as well.
    sent = await asgi_request(
        Starlette(routes=[Route("/", endpoint, methods=["GET"], max_body_size=100)], max_body_size=10),
        [],
        content_length=50,
    )
    assert status_and_body(sent) == (413, b"Content Too Large")
    assert calls == []


@pytest.mark.anyio
async def test_content_length_fail_fast_uses_the_limit_that_routing_settled_on() -> None:
    app = Starlette(
        routes=[
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=5),
        ],
        max_body_size=10,
    )
    assert status_and_body(await asgi_request(app, [b"x" * 50], path="/raised", content_length=50)) == (200, b"read 50")
    assert status_and_body(await asgi_request(app, [], path="/lowered", content_length=6))[0] == 413
    assert status_and_body(await asgi_request(app, [], path="/raised", content_length=101))[0] == 413


@pytest.mark.anyio
async def test_content_length_fail_fast_when_redirecting_and_for_host() -> None:
    from starlette.routing import Host

    app = Starlette(routes=[Route("/slash/", read_body, methods=["POST"])], max_body_size=10)
    assert status_and_body(await asgi_request(app, [], path="/slash", content_length=11))[0] == 413

    host_app = Router([Host("testserver", app=Router([Route("/", read_body, methods=["POST"])]))], max_body_size=10)
    assert status_and_body(await asgi_request(host_app, [], content_length=11))[0] == 413
    assert status_and_body(await asgi_request(host_app, [b"x" * 11]))[0] == 413


@pytest.mark.anyio
async def test_invalid_content_length_is_ignored() -> None:
    app = RequestBodyLimitMiddleware(Router([Route("/", read_body, methods=["POST"])]), max_body_size=10)
    scope_headers = [(b"content-length", b"abc"), (b"content-length", b"3")]

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"abc"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(
        {"type": "http", "method": "POST", "path": "/", "headers": scope_headers, "query_string": b"", "root_path": ""},
        receive,
        send,
    )
    assert status_and_body(sent) == (200, b"read 3")


def test_streaming_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks() -> Any:
        yield b"x" * 6
        yield b"x" * 6

    assert client.post("/", content=chunks()).status_code == 413


def test_json_and_form(test_client_factory: TestClientFactory) -> None:
    async def json_endpoint(request: Request) -> Response:
        return JSONResponse(await request.json())

    async def form_endpoint(request: Request) -> Response:
        return JSONResponse(dict(await request.form()))

    app = Starlette(
        routes=[Route("/json", json_endpoint, methods=["POST"]), Route("/form", form_endpoint, methods=["POST"])],
        max_body_size=20,
    )
    client = test_client_factory(app)
    assert client.post("/json", json={"a": 1}).json() == {"a": 1}
    assert client.post("/json", json={"a": "x" * 30}).status_code == 413
    assert client.post("/form", data={"a": "1"}).json() == {"a": "1"}
    assert client.post("/form", data={"a": "x" * 30}).status_code == 413


def test_multipart_counts_file_data_and_encoding_overhead(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            upload = form["file"]
            assert isinstance(upload, UploadFile)
            return PlainTextResponse(str(len(await upload.read())))

    files = {"file": ("a.txt", b"x" * 100)}
    request = test_client_factory(Starlette()).build_request("POST", "/", files=files)
    total = len(request.read())
    assert total > 100  # boundaries and headers count as well

    def client_for(limit: int) -> Any:
        return test_client_factory(Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=limit))

    assert client_for(total + 200).post("/", files=files).text == "100"
    # The file alone would fit, but not with the multipart overhead.
    assert client_for(100).post("/", files=files).status_code == 413
    assert client_for(total - 1).post("/", files=files).status_code == 413


@pytest.mark.anyio
async def test_multipart_closes_temporary_files_when_the_limit_is_crossed() -> None:
    closed: list[bool] = []
    boundary = b"b"
    head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n'

    async def endpoint(request: Request) -> Response:
        parser = MultiPartParser(request.headers, request.stream())
        try:
            await parser.parse()
        finally:
            closed.extend(f.closed for f in parser._files_to_close_on_error)
        return Response()  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=len(head) + 10)
    scope_headers = {"content-type": "multipart/form-data; boundary=" + boundary.decode()}
    assert scope_headers
    sent = await asgi_request_with_headers(app, [head + b"x" * 5, b"x" * 20], scope_headers)
    assert status_and_body(sent)[0] == 413
    assert closed == [True]


async def asgi_request_with_headers(app: Any, chunks: list[bytes], headers: dict[str, str]) -> list[Message]:
    sent: list[Message] = []
    pending = list(chunks)

    async def receive() -> Message:
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    await app(scope, receive, send)
    return sent


@pytest.mark.anyio
async def test_multipart_closes_temporary_files_on_any_interruption() -> None:
    head = b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n'
    parsers: list[MultiPartParser] = []

    async def stream() -> Any:
        yield head + b"data"
        raise RuntimeError("interrupted")

    parser = MultiPartParser(Headers({"content-type": "multipart/form-data; boundary=b"}), stream())
    parsers.append(parser)
    with pytest.raises(RuntimeError):
        await parser.parse()
    assert len(parser._files_to_close_on_error) == 1
    assert all(f.closed for f in parser._files_to_close_on_error)


@pytest.mark.anyio
async def test_standalone_middleware_with_a_plain_asgi_app() -> None:
    called = False

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            total += len(message["body"])
            if not message.get("more_body"):
                break
        await PlainTextResponse(str(total))(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=10)
    assert status_and_body(await asgi_request(app, [b"x" * 4, b"x" * 6])) == (200, b"10")
    assert status_and_body(await asgi_request(app, [b"x" * 4, b"x" * 7])) == (413, b"Content Too Large")

    # It rejects on entry, the application is never called.
    called = False
    assert status_and_body(await asgi_request(app, [], content_length=11)) == (413, b"Content Too Large")
    assert not called


@pytest.mark.anyio
async def test_standalone_middleware_ignores_other_scopes() -> None:
    seen: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    app = RequestBodyLimitMiddleware(asgi, max_body_size=0)
    await app({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    await app({"type": "websocket"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]


def test_standalone_middleware_wrapping_starlette(test_client_factory: TestClientFactory) -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])]), max_body_size=4)
    client = test_client_factory(app)
    assert client.post("/", content=b"xxxx").status_code == 200
    assert client.post("/", content=b"xxxxx").status_code == 413


@pytest.mark.anyio
async def test_response_already_started_gets_no_second_response() -> None:
    reads: list[Message] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"early", "more_body": True})
        while True:
            message = await receive()
            reads.append(message)
            if message["type"] != "http.request":
                break
        # The application carries on and finishes its response.
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent = await asgi_request(app, [b"x" * 3, b"x" * 3, b"x" * 3])
    assert [m["type"] for m in sent].count("http.response.start") == 1
    assert sent[0]["status"] == 200
    assert reads[-1] == {"type": "http.disconnect"}
    assert len(reads) == 2  # the stream stops at the chunk that crossed the limit


@pytest.mark.anyio
async def test_started_response_with_starlette_stream(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async def body() -> Any:
            yield b"first"
            async for _ in request.stream():  # pragma: no branch
                pass  # pragma: no cover

        return StreamingResponse(body())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    sent = await asgi_request(app, [b"xx", b"xx"])
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1  # never a second response


@pytest.mark.anyio
async def test_application_errors_unrelated_to_the_limit_still_propagate() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await asgi_request(RequestBodyLimitMiddleware(asgi, max_body_size=5), [b"x"])


def test_server_errors_are_not_swallowed_by_the_limit(test_client_factory: TestClientFactory) -> None:
    async def broken(request: Request) -> Response:
        raise RuntimeError("boom")

    app = Starlette(routes=[Route("/", broken, methods=["POST"])], max_body_size=5)
    with pytest.raises(RuntimeError):
        test_client_factory(app).post("/", content=b"x")


# Regression tests for the causes found in review.


def _post_status(app: Any, chunks: list[bytes], **kwargs: Any) -> int:
    import anyio

    return status_and_body(anyio.run(lambda: asgi_request(app, chunks, **kwargs)))[0]


def test_host_defers_content_length_to_the_router_below() -> None:
    from starlette.routing import Host

    app = Starlette(
        routes=[Host("testserver", app=Router([Route("/", read_body, methods=["POST"], max_body_size=100)]))],
        max_body_size=10,
    )
    assert _post_status(app, [b"x" * 50], content_length=50) == 200
    assert _post_status(app, [b"x" * 101], content_length=101) == 413


def test_mount_defers_content_length_to_a_router_wrapped_in_middleware() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = GZipMiddleware(Router([Route("/", read_body, methods=["POST"], max_body_size=100)]))
    app = Starlette(routes=[Mount("/m", app=inner)], max_body_size=10)
    assert _post_status(app, [b"x" * 50], path="/m/", content_length=50) == 200
    assert _post_status(app, [], path="/m/", content_length=101) == 413


def test_mount_of_a_starlette_app_uses_its_limit_for_content_length() -> None:
    sub = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/m", app=sub)], max_body_size=10)
    assert _post_status(app, [b"x" * 50], path="/m/", content_length=50) == 200
    assert _post_status(app, [], path="/m/", content_length=101) == 413


def test_standalone_middleware_limit_cannot_be_raised_from_below() -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)], max_body_size=1000)
    app = RequestBodyLimitMiddleware(inner, max_body_size=10)
    for headers in (None, 50):
        assert _post_status(app, [b"x" * 25, b"x" * 25], content_length=headers) == 413
    nested = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(inner, max_body_size=500), max_body_size=10)
    assert _post_status(nested, [b"x" * 25, b"x" * 25]) == 413


def test_standalone_middleware_in_the_middleware_list_is_a_cap() -> None:
    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
        max_body_size=1000,
    )
    assert _post_status(app, [b"x" * 25, b"x" * 25]) == 413
    assert _post_status(app, [b"x" * 10]) == 200


class ReadFirst:
    total = 0

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            ReadFirst.total = 0
            async for chunk in Request(scope, receive).stream():
                ReadFirst.total += len(chunk)
        await self.app(scope, receive, send)


def test_application_limit_covers_user_middleware() -> None:
    from starlette.middleware import Middleware

    ReadFirst.total = 0
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(ReadFirst)], max_body_size=10
    )
    assert _post_status(app, [b"x" * 6] * 10) == 413
    assert ReadFirst.total <= 12


def test_application_content_length_fails_fast_before_user_middleware() -> None:
    from starlette.middleware import Middleware

    calls: list[str] = []

    class Rec:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append(scope["type"])  # pragma: no cover
            await self.app(scope, receive, send)  # pragma: no cover

    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], middleware=[Middleware(Rec)], max_body_size=10)
    assert _post_status(app, [], content_length=11) == 413
    assert calls == []
    # An inner route that raises the limit is still honoured, and then the middleware does run.
    raised = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(Rec)],
        max_body_size=10,
    )
    assert _post_status(raised, [b"x" * 50], content_length=50) == 200
    assert calls == ["http"]


@pytest.mark.anyio
async def test_413_is_delivered_when_the_application_receives_in_a_cancelled_scope() -> None:
    import anyio

    async def endpoint(request: Request) -> Response:
        if await request.is_disconnected():
            return PlainTextResponse("gone")  # pragma: no cover
        return PlainTextResponse("read " + str(len(await request.body())))  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5)
    sent: list[Message] = []
    chunks = [b"x" * 10]

    async def receive() -> Message:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": False}
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        await anyio.sleep(0.01)  # a send that yields, as a real server's does
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "root_path": "", "headers": []}
    await app(scope, receive, send)
    assert status_and_body(sent) == (413, b"Content Too Large")


@pytest.mark.anyio
async def test_413_is_delivered_when_the_body_read_has_a_deadline() -> None:
    import anyio

    async def endpoint(request: Request) -> Response:
        with anyio.move_on_after(0.05):
            await request.body()
        return PlainTextResponse("late")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5)
    sent: list[Message] = []
    chunks = [b"x" * 10]

    async def receive() -> Message:
        return {"type": "http.request", "body": chunks.pop(0), "more_body": False}

    async def send(message: Message) -> None:
        await anyio.sleep(0.2)  # slower than the deadline
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "root_path": "", "headers": []}
    await app(scope, receive, send)
    assert status_and_body(sent) == (413, b"Content Too Large")


def test_invalid_limits_are_rejected_at_configuration() -> None:
    for bad, error in ((-1, ValueError), ("10", TypeError), (1.5, TypeError), (True, TypeError)):
        with pytest.raises(error):
            Starlette(max_body_size=bad)  # type: ignore[arg-type]
        with pytest.raises(error):
            Route("/", read_body, max_body_size=bad)  # type: ignore[arg-type]
        with pytest.raises(error):
            RequestBodyLimitMiddleware(Router(), max_body_size=bad)  # type: ignore[arg-type]
    assert Starlette(max_body_size=0).max_body_size == 0


def test_content_length_uses_the_route_routing_picks_after_a_partial_match() -> None:
    routes = [
        Route("/", read_body, methods=["GET"]),
        Route("/", read_body, methods=["POST"], max_body_size=100),
    ]
    for app in (Starlette(routes=routes, max_body_size=10), Router(routes, max_body_size=10)):
        assert _post_status(app, [b"x" * 50], content_length=50) == 200
        assert _post_status(app, [b"x" * 50]) == 200
        assert _post_status(app, [], content_length=101) == 413


def test_predicted_limit_for_content_length_is_not_used_for_counting() -> None:
    from starlette.middleware import Middleware

    class MethodOverride:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope["method"] = "PUT"
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/items", read_body, methods=["POST"], max_body_size=10_000),
            Route("/items", read_body, methods=["PUT"]),
        ],
        middleware=[Middleware(MethodOverride)],
        max_body_size=10,
    )
    assert _post_status(app, [b"x" * 50, b"x" * 50], path="/items") == 413
    # The declared length is predicted against the POST route, the PUT route that handles it gets 10.
    assert _post_status(app, [b"x" * 50, b"x" * 50], path="/items", content_length=100) == 413


@pytest.mark.anyio
@pytest.mark.parametrize("standalone", [False, True])
async def test_server_can_cancel_the_request_while_the_413_send_is_stuck(standalone: bool) -> None:
    import time

    import anyio

    if standalone:
        app: Any = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", read_body, methods=["POST"])]), 5)
    else:
        app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    chunks = [b"x" * 10]

    async def receive() -> Message:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": False}
        await anyio.sleep(3)  # pragma: no cover
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: Message) -> None:
        await anyio.sleep(3)  # the client is not reading

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "root_path": "", "headers": []}
    start = time.monotonic()
    with anyio.move_on_after(0.2):
        await app(scope, receive, send)
    assert time.monotonic() - start < 1
