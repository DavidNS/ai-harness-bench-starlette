from __future__ import annotations

import gc
from collections.abc import Callable
from typing import Any

import pytest

from starlette import formparsers
from starlette.applications import Starlette
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory


async def read_size(request: Request) -> Response:
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def ignore_body(request: Request) -> Response:
    return PlainTextResponse("ignored")


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/", content=b"x" * 11))


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_applies_before_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(max_body_size=5)
    assert_too_large(test_client_factory(app).post("/missing", content=b"x" * 6))


def test_no_body_and_get(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_size)], max_body_size=0)
    assert test_client_factory(app).get("/").json() == {"size": 0}


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_size, methods=["POST"])], max_body_size=4)
    client = test_client_factory(router)
    assert client.post("/", content=b"1234").status_code == 200
    assert_too_large(client.post("/", content=b"12345"))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/limited", routes=[Route("/", read_size, methods=["POST"])], max_body_size=4),
            Route("/free", read_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/limited/", content=b"1234").status_code == 200
    assert_too_large(client.post("/limited/", content=b"12345"))
    assert client.post("/free", content=b"x" * 1000).status_code == 200


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/a", read_size, methods=["POST"], max_body_size=4),
            Route("/b", read_size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/a", content=b"1234").status_code == 200
    assert_too_large(client.post("/a", content=b"12345"))
    assert client.post("/b", content=b"x" * 1000).status_code == 200


def test_inner_limit_raises_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/big", read_size, methods=["POST"], max_body_size=100),
            Mount("/m", routes=[Route("/", read_size, methods=["POST"])], max_body_size=100),
            Route("/small", read_size, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    # Content-Length above the application limit is fine when the route raises it.
    assert client.post("/big", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/m/", content=b"x" * 50).json() == {"size": 50}
    assert_too_large(client.post("/big", content=b"x" * 101))
    assert_too_large(client.post("/small", content=b"x" * 11))


def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_size, methods=["POST"], max_body_size=5),
            Route("/default", read_size, methods=["POST"]),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"x" * 5).status_code == 200
    assert_too_large(client.post("/small", content=b"x" * 6))
    assert client.post("/default", content=b"x" * 100).status_code == 200


def test_content_length_fail_fast_without_reading(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "ignored"
    assert_too_large(client.post("/", content=b"x" * 11))


async def collect(app: Any, headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def receive() -> dict[str, Any]:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    try:
        await app(scope, receive, send)
    finally:
        pass
    return sent


@pytest.mark.anyio
async def test_middleware_standalone_streaming() -> None:
    seen: list[int] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        more = True
        while more:
            message = await receive()
            seen.append(len(message["body"]))
            more = message["more_body"]
        await PlainTextResponse("ok")(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=6)
    sent = await collect(app, [], [b"abc", b"def"])
    assert sent[0]["status"] == 200

    seen.clear()
    sent = await collect(app, [], [b"abc", b"defg", b"h"])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert sent[1]["body"] == b"Content Too Large"
    assert seen == [3]  # the oversized chunk is never delivered


@pytest.mark.anyio
async def test_middleware_understated_content_length() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive())["more_body"]:
            pass  # pragma: no cover
        await PlainTextResponse("ok")(scope, receive, send)  # pragma: no cover

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    for header in (b"1", b"abc", b"1_0", b"-1"):
        sent = await collect(app, [(b"content-length", header)], [b"x" * 20])
        assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_middleware_declared_length_not_read() -> None:
    called = False

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        await PlainTextResponse("ok")(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent = await collect(app, [(b"content-length", b"6")], [b""])
    assert called
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


@pytest.mark.anyio
async def test_middleware_response_already_started() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while (await receive())["more_body"]:
            pass  # pragma: no cover

    app = RequestBodyLimitMiddleware(asgi, max_body_size=3)
    sent = await collect(app, [], [b"abcd", b"e"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200


@pytest.mark.anyio
async def test_middleware_app_swallows_error() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception:
            pass
        await PlainTextResponse("ok")(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=1)
    sent = await collect(app, [], [b"abc"])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


@pytest.mark.anyio
async def test_middleware_ignores_other_scopes_and_none() -> None:
    calls: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope["type"])

    for app in (RequestBodyLimitMiddleware(asgi, max_body_size=1), RequestBodyLimitMiddleware(asgi)):
        for type_ in ("lifespan", "websocket", "http"):
            await app({"type": type_, "headers": [(b"content-length", b"100")]}, None, None)  # type: ignore[arg-type]
    assert calls == ["lifespan", "websocket", "http"] * 2


def test_middleware_with_starlette_app(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware import Middleware

    app = Starlette(
        routes=[Route("/", read_size, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=3)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"abc").status_code == 200
    assert_too_large(client.post("/", content=b"abcd"))


def test_multipart_counts_all_bytes(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> Response:
        async with request.form() as form:
            return JSONResponse({"keys": sorted(form.keys())})

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=500)
    client = test_client_factory(app)
    assert client.post("/", files={"f": ("a.txt", b"x" * 100)}).status_code == 200
    # The file data is small, but with the multipart encoding the body exceeds the limit.
    response = client.post("/", files={"f": ("a.txt", b"x" * 480)})
    assert_too_large(response)


@pytest.mark.anyio
async def test_multipart_temp_files_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        f = original(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    async def upload(request: Request) -> Response:
        async with request.form():
            pass  # pragma: no cover
        return Response()  # pragma: no cover

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=3000)
    body = (
        b'--b\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\n'
        + b"x" * 4000
        + b"\r\n--b--\r\n"
    )
    # Streamed without a Content-Length, so the parse starts and is interrupted midway.
    chunks = [body[i : i + 1000] for i in range(0, len(body), 1000)]
    headers = [(b"content-type", b"multipart/form-data; boundary=b")]
    sent = await collect(app, headers, chunks)
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert created
    assert all(f.closed for f in created)


@pytest.mark.anyio
async def test_multipart_parse_closes_files_on_any_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.datastructures import Headers

    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        f = original(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)
    body = (
        b'--b\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\n'
        b"data\r\n"
    )

    async def stream() -> Any:
        yield body
        raise RuntimeError("interrupted")

    parser = formparsers.MultiPartParser(Headers({"Content-Type": "multipart/form-data; boundary=b"}), stream())
    with pytest.raises(RuntimeError):
        await parser.parse()
    assert created and all(f.closed for f in created)
    gc.collect()
