from __future__ import annotations

from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Host, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

pytestmark = pytest.mark.anyio

called: list[str] = []


async def echo_size(request: Request) -> Response:
    called.append(request.url.path)
    return PlainTextResponse(str(len(await request.body())))


@pytest.fixture(autouse=True)
def reset_called() -> None:
    called.clear()


async def call(
    app: Any,
    path: str = "/",
    chunks: list[bytes] | None = None,
    content_length: int | None = None,
    method: str = "POST",
) -> tuple[int | None, bytes, int]:
    """Drive an ASGI app directly. Returns (status, body, number of receive() calls)."""
    chunks = [] if chunks is None else chunks
    headers = [(b"content-length", str(content_length).encode())] if content_length is not None else []
    scope: Scope = {
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
    receives = 0

    async def receive() -> Message:
        nonlocal receives
        receives += 1
        if chunks and receives <= len(chunks):
            return {"type": "http.request", "body": chunks[receives - 1], "more_body": receives < len(chunks)}
        if not chunks and receives == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, receives


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", echo_size, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_no_body_and_get_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", echo_size)], max_body_size=0)
    assert test_client_factory(app).get("/").text == "0"


def test_invalid_max_body_size() -> None:
    with pytest.raises(AssertionError):
        Starlette(max_body_size=-1)
    with pytest.raises(AssertionError):
        Route("/", echo_size, max_body_size="1")  # type: ignore[arg-type]


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router([Route("/", echo_size, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", echo_size, methods=["POST"])], max_body_size=5),
            Route("/", echo_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert client.post("/", content=b"x" * 600).status_code == 200


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", echo_size, methods=["POST"], max_body_size=5),
            Route("/free", echo_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 6).status_code == 413
    assert client.post("/free", content=b"x" * 600).status_code == 200


def test_inner_limits_raise_and_lower(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", echo_size, methods=["POST"], max_body_size=100),
            Route("/lower", echo_size, methods=["POST"], max_body_size=5),
            Route("/inherit", echo_size, methods=["POST"]),
            Mount(
                "/mount",
                routes=[
                    Route("/raise", echo_size, methods=["POST"], max_body_size=100),
                    Route("/inherit", echo_size, methods=["POST"]),
                ],
                max_body_size=50,
            ),
            Mount("/router", app=Router([Route("/", echo_size, methods=["POST"])], max_body_size=200)),
        ],
        max_body_size=20,
    )
    client = test_client_factory(app)
    assert client.post("/raise", content=b"x" * 90).status_code == 200
    assert client.post("/raise", content=b"x" * 101).status_code == 413
    assert client.post("/lower", content=b"x" * 6).status_code == 413
    assert client.post("/inherit", content=b"x" * 20).status_code == 200
    assert client.post("/inherit", content=b"x" * 21).status_code == 413
    assert client.post("/mount/inherit", content=b"x" * 50).status_code == 200
    assert client.post("/mount/inherit", content=b"x" * 51).status_code == 413
    assert client.post("/mount/raise", content=b"x" * 100).status_code == 200
    assert client.post("/mount/raise", content=b"x" * 101).status_code == 413
    assert client.post("/router/", content=b"x" * 200).status_code == 200
    assert client.post("/router/", content=b"x" * 201).status_code == 413


async def test_nested_starlette_app_limit() -> None:
    inner = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=100)
    app = Starlette(routes=[Mount("/inner", app=inner)], max_body_size=10)
    assert (await call(app, "/inner/", [b"x" * 50]))[0] == 200
    assert (await call(app, "/inner/", [b"x" * 101]))[0] == 413
    # declared length is checked by the inner application, which raised the limit
    assert (await call(app, "/inner/", [b"x" * 50], content_length=50))[0] == 200
    assert (await call(app, "/inner/", [], content_length=101))[0] == 413


async def test_limit_counts_actual_bytes_over_understated_header() -> None:
    app = Starlette(routes=[Route("/", echo_size, methods=["POST"])], max_body_size=10)
    assert (await call(app, chunks=[b"x" * 6, b"x" * 6], content_length=1))[0] == 413
    assert (await call(app, chunks=[b"x" * 6, b"x" * 6]))[0] == 413


async def test_standalone_middleware_any_asgi_app() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover
            return
        called.append("asgi")
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await PlainTextResponse(str(len(body)))(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=10)
    assert await call(app, chunks=[b"x" * 5, b"x" * 5]) == (200, b"10", 2)
    status, body, _ = await call(app, chunks=[b"x" * 5, b"x" * 6])
    assert (status, body) == (413, b"Content Too Large")

    # declared length: rejected on entry, application never runs and body never read
    called.clear()
    status, body, receives = await call(app, chunks=[b"x"], content_length=11)
    assert (status, body, receives) == (413, b"Content Too Large", 0)
    assert called == []

    # lifespan and websocket pass through
    seen: list[str] = []

    async def other(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    wrapped = RequestBodyLimitMiddleware(other, max_body_size=1)
    await wrapped({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    await wrapped({"type": "websocket"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]


async def test_content_length_fail_fast_waits_for_routing() -> None:
    app = Starlette(
        routes=[
            Route("/default", echo_size, methods=["POST"]),
            Route("/own", echo_size, methods=["POST"], max_body_size=100),
            Mount("/mount", routes=[Route("/", echo_size, methods=["POST"], max_body_size=100)]),
            Route("/get", echo_size, methods=["GET"]),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_record_middleware)],
        max_body_size=10,
    )

    # over the app limit, no own limit: rejected at routing, before the endpoint
    assert await call(app, "/default", content_length=11) == (413, b"Content Too Large", 0)
    # route without a method match, and without any route: application's limit, not 405/404
    assert (await call(app, "/get", content_length=11))[0] == 413
    assert (await call(app, "/missing", content_length=11))[0] == 413
    assert (await call(app, "/missing/", content_length=11))[0] == 413
    assert not any(c.startswith("/") for c in called)  # no endpoint ran

    # inner limit raises: a declared length between the limits is let through
    assert (await call(app, "/own", [b"x" * 50], content_length=50))[0] == 200
    assert (await call(app, "/mount/", [b"x" * 50], content_length=50))[0] == 200
    assert (await call(app, "/own", content_length=101))[0] == 413
    assert (await call(app, "/mount/", content_length=101))[0] == 413
    # no declared length / small declared length: ok
    assert (await call(app, "/default", [b"x" * 10], content_length=10))[0] == 200


async def _record_middleware(request: Request, call_next: Any) -> Response:
    called.append("mw")
    return await call_next(request)


async def test_content_length_fail_fast_host_and_plain_mount() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        called.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(
        routes=[Mount("/plain", app=asgi), Host("testserver", app=Router([Route("/h", echo_size)]))],
        max_body_size=10,
    )
    assert (await call(app, "/plain/x", content_length=11))[0] == 413
    assert called == []
    app = Starlette(routes=[Mount("/plain", app=asgi, max_body_size=5)], max_body_size=100)
    assert (await call(app, "/plain/x", content_length=6))[0] == 413
    assert (await call(app, "/plain/x", content_length=5))[0] == 200


async def test_host_with_plain_app() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        called.append("asgi")

    app = Starlette(routes=[Host("testserver", app=asgi)], max_body_size=1)
    scope_headers_app = RequestBodyLimitMiddleware(app, max_body_size=1)  # also standalone around Starlette
    assert (await call(scope_headers_app, "/x", content_length=5))[0] == 413
    assert called == []


async def test_redirect_slashes_checked() -> None:
    app = Starlette(routes=[Route("/a/", echo_size, methods=["POST"])], max_body_size=10)
    assert (await call(app, "/a", content_length=11))[0] == 413
    assert (await call(app, "/a", content_length=5))[0] == 307


async def test_response_already_started() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            await receive()  # raises once the limit is crossed

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent: list[Message] = []
    receives = 0

    async def receive() -> Message:
        nonlocal receives
        receives += 1
        return {"type": "http.request", "body": b"xxx", "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "http", "headers": []}, receive, send)
    assert receives == 2  # body stream stopped at the crossing
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]  # no second response


async def test_swallowed_exception_still_413() -> None:
    async def endpoint(request: Request) -> Response:
        try:
            await request.body()
        except Exception:
            pass
        return PlainTextResponse("handled")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=5)
    assert await call(app, chunks=[b"x" * 10]) == (413, b"Content Too Large", 1)


async def test_app_raising_other_exception_after_limit() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception as exc:
            raise ValueError("wrapped") from exc

    app = RequestBodyLimitMiddleware(asgi, max_body_size=1)
    assert await call(app, chunks=[b"xx"]) == (413, b"Content Too Large", 1)


async def test_unrelated_exception_propagates() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await call(RequestBodyLimitMiddleware(asgi, max_body_size=1))


async def test_started_response_other_exception_propagates() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await receive()
        except Exception:
            raise ValueError("boom")

    with pytest.raises(ValueError):
        await call(RequestBodyLimitMiddleware(asgi, max_body_size=1), chunks=[b"xx"])


async def test_lowering_below_already_received_bytes() -> None:
    async def read_then_route(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await Router([Route("/", echo_size, methods=["POST"], max_body_size=3)])(scope, receive, send)

    app = RequestBodyLimitMiddleware(read_then_route, max_body_size=100)
    assert (await call(app, chunks=[b"x" * 5, b"x"]))[0] == 413
    assert called == []


async def test_non_numeric_content_length_ignored() -> None:
    app = RequestBodyLimitMiddleware(Starlette(routes=[Route("/", echo_size, methods=["POST"])]), max_body_size=10)
    scope_app = app

    async def send(message: Message) -> None:
        pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x"}

    await scope_app(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"content-length", b"abc")],
        },
        receive,
        send,
    )
    assert called == ["/"]


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> Response:
        async with request.form() as form:
            upload_file: Any = form["file"]
            return PlainTextResponse(str(len(await upload_file.read())))

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=1000)
    client = test_client_factory(app)
    # file data fits, but data plus multipart framing/fields does not
    assert client.post("/", files={"file": ("a.txt", b"x" * 500)}).text == "500"
    assert client.post("/", files={"file": ("a.txt", b"x" * 980)}).status_code == 413
    assert client.post("/", files={"file": ("a.txt", b"x" * 5000)}).status_code == 413


async def test_multipart_temp_files_closed_on_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette import formparsers

    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        f = original(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    async def upload(request: Request) -> Response:
        await request.form()  # pragma: no cover
        return PlainTextResponse("no")  # pragma: no cover

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300)
    boundary = "b"
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'.encode()
    chunks = [head + b"x" * 100, b"y" * 100, b"z" * 200, f"\r\n--{boundary}--\r\n".encode()]

    sent: list[Message] = []
    n = 0

    async def receive() -> Message:
        nonlocal n
        n += 1
        return {"type": "http.request", "body": chunks[n - 1], "more_body": n < len(chunks)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert created[0].closed


async def test_multipart_temp_files_closed_on_any_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette import formparsers
    from starlette.datastructures import Headers

    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        f = original(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    class Boom(BaseException):
        pass

    async def stream() -> Any:
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\nabc'
        raise Boom()

    headers = Headers({"content-type": "multipart/form-data; boundary=b"})
    with pytest.raises(Boom):
        await formparsers.MultiPartParser(headers, stream()).parse()
    assert len(created) == 1 and created[0].closed
