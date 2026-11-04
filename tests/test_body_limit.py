from __future__ import annotations

from typing import Any

import pytest

from starlette._body_limit import BodyTooLarge
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Host, Mount, Route, Router, WebSocketRoute
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocket
from tests.types import TestClientFactory

calls: list[str] = []


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def read_body(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


async def ignore_body(request: Request) -> Response:
    calls.append(request.url.path)
    return PlainTextResponse("ok")


def chunks(*sizes: int) -> Any:
    for size in sizes:
        yield b"x" * size


async def drive(
    app: Any,
    body: list[bytes],
    *,
    content_length: int | None = None,
    method: str = "POST",
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[list[Message], int]:
    """Run `app` with a hand-made request. Returns the sent messages and how many receive() calls were served."""
    raw_headers = list(headers or [])
    if content_length is not None:
        raw_headers.append((b"content-length", str(content_length).encode()))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw_headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(body) - 1} for i, chunk in enumerate(body)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]
    served = 0

    async def receive() -> Message:
        nonlocal served
        if served < len(messages):
            served += 1
            return messages[served - 1]
        return {"type": "http.disconnect"}  # pragma: no cover

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent, served


def status_of(sent: list[Message]) -> int:
    return int(sent[0]["status"])


def assert_413(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_413(client.post("/", content=b"x" * 11))
    assert client.post("/").text == "0"


def test_application_limit_json(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        return JSONResponse(await request.json())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=20)
    client = test_client_factory(app)
    assert client.post("/", json={"a": 1}).json() == {"a": 1}
    assert_413(client.post("/", json={"a": "b" * 50}))


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=chunks(4, 4)).text == "8"
    response = client.post("/", content=chunks(4, 4, 4))
    assert_413(response)


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"12345").text == "5"
    assert_413(client.post("/", content=b"123456"))
    assert calls == ["/"]


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=3),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/small", content=b"123").text == "3"
    assert_413(client.post("/small", content=b"1234"))
    assert client.post("/free", content=b"x" * 1000).text == "1000"


def test_standalone_route_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Route("/", read_body, methods=["POST"], max_body_size=3))
    assert client.post("/", content=b"123").text == "3"
    assert_413(client.post("/", content=b"1234"))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(
        routes=[
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=4),
            Route("/other", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"1234").text == "4"
    assert_413(client.post("/m/", content=b"12345"))
    assert client.post("/other", content=b"12345").text == "5"


def test_mount_of_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await Request(scope, receive).body()
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Mount("/m", asgi, max_body_size=4)])
    client = test_client_factory(app)
    assert client.post("/m/", content=b"1234").text == "ok"
    # The mounted app is opaque, the declared length is checked when it first receives or sends.
    assert_413(client.post("/m/", content=b"12345"))
    assert calls == ["asgi"]


def test_standalone_mount_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=2))
    assert client.post("/m/", content=b"12").text == "2"
    assert_413(client.post("/m/", content=b"123"))


def test_inner_limit_raises_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount("/r", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)),
            Route("/default", read_body, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path in ("/big", "/m/", "/r/"):
        assert client.post(path, content=b"x" * 50).text == "50"
        assert client.post(path, content=b"x" * 50, headers={"content-length": "50"}).text == "50"
        assert_413(client.post(path, content=b"x" * 101))
    assert_413(client.post("/default", content=b"x" * 50))


def test_inner_limit_lowers_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/small", read_body, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Route("/default", read_body, methods=["POST"]),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    assert_413(client.post("/small", content=b"x" * 6))
    assert_413(client.post("/m/", content=b"x" * 6))
    assert client.post("/small", content=b"x" * 5).text == "5"
    assert client.post("/default", content=b"x" * 50).text == "50"


def test_nested_overrides(test_client_factory: TestClientFactory) -> None:
    inner = Mount(
        "/in",
        routes=[Route("/", read_body, methods=["POST"]), Route("/own", read_body, methods=["POST"], max_body_size=50)],
        max_body_size=20,
    )
    app = Starlette(routes=[Mount("/out", routes=[inner], max_body_size=10)], max_body_size=1000)
    client = test_client_factory(app)
    assert client.post("/out/in/", content=b"x" * 20).text == "20"
    assert_413(client.post("/out/in/", content=b"x" * 21))
    assert client.post("/out/in/own", content=b"x" * 50).text == "50"
    assert_413(client.post("/out/in/own", content=b"x" * 51))


def test_mounted_starlette_inherits_and_overrides(test_client_factory: TestClientFactory) -> None:
    inherit = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    own = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)
    app = Starlette(routes=[Mount("/a", app=inherit), Mount("/b", app=own)], max_body_size=10)
    client = test_client_factory(app)
    assert_413(client.post("/a/", content=b"x" * 11))
    assert client.post("/b/", content=b"x" * 50).text == "50"
    assert_413(client.post("/b/", content=b"x" * 51))


@pytest.mark.anyio
async def test_content_length_fail_fast_on_route() -> None:
    app = Starlette(routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=10)
    sent, served = await drive(app, [b"x" * 5], content_length=11)
    assert status_of(sent) == 413
    assert sent[1]["body"] == b"Content Too Large"
    assert calls == []
    assert served == 0


@pytest.mark.anyio
async def test_content_length_is_not_trusted_when_understated() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent, _ = await drive(app, [b"x" * 6, b"x" * 6], content_length=3)
    assert status_of(sent) == 413
    assert len(sent) == 2

    sent, _ = await drive(app, [b"x" * 6, b"x" * 6])
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_invalid_content_length_is_ignored() -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    sent, _ = await drive(app, [b"x" * 5], headers=[(b"content-length", b"abc")])
    assert status_of(sent) == 200


@pytest.mark.anyio
async def test_content_length_fail_fast_waits_for_routing() -> None:
    # The declared length is over the application limit but within the matched route's own limit.
    app = Starlette(
        routes=[
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Mount("/m", routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=100),
            Route("/default", ignore_body, methods=["POST"]),
        ],
        max_body_size=10,
    )
    sent, _ = await drive(app, [b"x" * 50], content_length=50, path="/big")
    assert status_of(sent) == 200
    sent, _ = await drive(app, [], content_length=50, path="/m/")
    assert status_of(sent) == 200
    calls.clear()
    sent, _ = await drive(app, [], content_length=50, path="/default")
    assert status_of(sent) == 413
    assert calls == []


@pytest.mark.anyio
async def test_content_length_fail_fast_no_route_and_no_method() -> None:
    app = Starlette(
        routes=[Route("/get", ignore_body, methods=["GET"], max_body_size=100)],
        max_body_size=10,
    )
    sent, served = await drive(app, [b"x" * 50], content_length=50, path="/nowhere")
    assert status_of(sent) == 413
    assert served == 0
    # Method mismatch: the matched route's own limit does not apply, the outer one does.
    sent, served = await drive(app, [b"x" * 50], content_length=50, path="/get")
    assert status_of(sent) == 413
    assert served == 0
    sent, _ = await drive(app, [b"x" * 5], content_length=5, path="/get")
    assert status_of(sent) == 405
    sent, _ = await drive(app, [b"x" * 5], content_length=5, path="/nowhere")
    assert status_of(sent) == 404


@pytest.mark.anyio
async def test_content_length_fail_fast_redirect_slashes_case() -> None:
    app = Starlette(routes=[Route("/a/", ignore_body, methods=["POST"])], max_body_size=10)
    sent, _ = await drive(app, [], content_length=50, path="/a")
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_content_length_fail_fast_plain_router_and_host() -> None:
    router = Router(routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=10)
    sent, served = await drive(router, [b"x"], content_length=11)
    assert status_of(sent) == 413
    assert served == 0

    hosted = Starlette(routes=[Host("testserver", app=Router(routes=[Route("/", ignore_body, methods=["POST"])]))])
    sent, _ = await drive(hosted, [b"x" * 5], content_length=5, headers=[(b"host", b"testserver")])
    assert status_of(sent) == 200

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover

    calls.clear()
    hosted = Starlette(routes=[Host("testserver", app=asgi)], max_body_size=10)
    sent, _ = await drive(hosted, [], content_length=50, headers=[(b"host", b"testserver")])
    assert status_of(sent) == 413
    assert calls == []


@pytest.mark.anyio
async def test_custom_route_is_checked_before_handle() -> None:
    from starlette.routing import BaseRoute, Match

    class Custom(BaseRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            return Match.FULL, {}

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("custom")  # pragma: no cover

    app = Starlette(routes=[Custom()], max_body_size=10)
    sent, _ = await drive(app, [], content_length=50)
    assert status_of(sent) == 413
    assert calls == []


@pytest.mark.anyio
async def test_inner_limit_below_bytes_already_received() -> None:
    async def endpoint(request: Request) -> Response:
        return PlainTextResponse("ok")  # pragma: no cover

    class ReadAll:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            while (await receive()).get("more_body"):
                pass
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[Route("/", endpoint, methods=["POST"], max_body_size=5)],
        middleware=[Middleware(ReadAll)],
    )
    sent, _ = await drive(app, [b"x" * 4, b"x" * 4])
    assert status_of(sent) == 413


# Standalone middleware.


def make_asgi() -> Any:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("app")
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(total).encode()})

    return app


@pytest.mark.anyio
async def test_middleware_on_plain_asgi_app() -> None:
    app = RequestBodyLimitMiddleware(make_asgi(), max_body_size=10)
    sent, _ = await drive(app, [b"x" * 5, b"x" * 5])
    assert status_of(sent) == 200
    assert sent[1]["body"] == b"10"

    sent, _ = await drive(app, [b"x" * 5, b"x" * 6])
    assert status_of(sent) == 413
    assert sent[1]["body"] == b"Content Too Large"
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


@pytest.mark.anyio
async def test_middleware_rejects_on_entry() -> None:
    app = RequestBodyLimitMiddleware(make_asgi(), max_body_size=10)
    sent, served = await drive(app, [b"x" * 5], content_length=11)
    assert status_of(sent) == 413
    assert served == 0
    assert calls == []


@pytest.mark.anyio
async def test_middleware_with_understated_content_length() -> None:
    app = RequestBodyLimitMiddleware(make_asgi(), max_body_size=10)
    sent, _ = await drive(app, [b"x" * 20], content_length=1)
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_middleware_swallowed_error_still_gets_413() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception:
            pass

    sent, _ = await drive(RequestBodyLimitMiddleware(app, max_body_size=1), [b"xx"])
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_middleware_unrelated_errors_propagate() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception as exc:
            raise ValueError("boom") from exc

    with pytest.raises(ValueError):
        await drive(RequestBodyLimitMiddleware(app, max_body_size=1), [b"xx"])

    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await drive(RequestBodyLimitMiddleware(failing, max_body_size=1), [b"x"])


@pytest.mark.anyio
async def test_middleware_no_second_response_when_already_started() -> None:
    receives = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal receives
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        while True:
            receives += 1
            message = await receive()
            if not message.get("more_body"):  # pragma: no cover
                break

    sent, _ = await drive(RequestBodyLimitMiddleware(app, max_body_size=3), [b"xx", b"xx", b"xx"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    assert receives == 2  # the stream stopped at the second chunk


@pytest.mark.anyio
async def test_middleware_stream_stays_stopped() -> None:
    results: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            while True:
                await receive()
        except Exception:
            results.append("first")
        try:
            await receive()
        except Exception:
            results.append("second")

    sent, _ = await drive(RequestBodyLimitMiddleware(app, max_body_size=1), [b"xx", b"x"])
    assert results == ["first", "second"]
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_middleware_does_not_touch_other_scopes() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestBodyLimitMiddleware(app, max_body_size=1)

    async def receive() -> Message:  # pragma: no cover
        raise AssertionError

    async def send(message: Message) -> None:  # pragma: no cover
        raise AssertionError

    await middleware({"type": "lifespan"}, receive, send)
    await middleware({"type": "websocket"}, receive, send)
    assert seen == ["lifespan", "websocket"]


def test_middleware_rejects_negative_limit() -> None:
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(make_asgi(), max_body_size=-1)


def test_middleware_inside_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=4)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"1234").text == "4"
    assert_413(client.post("/", content=b"12345"))


def test_middleware_with_none_is_passthrough(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(make_asgi(), max_body_size=None))
    assert client.post("/", content=b"x" * 1000).text == "1000"


def test_no_body_requests_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", ignore_body)], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").text == "ok"


def test_zero_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=0)
    client = test_client_factory(app)
    assert client.post("/").text == "0"
    assert_413(client.post("/", content=b"x"))


def test_websocket_unaffected(test_client_factory: TestClientFactory) -> None:
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        text = await websocket.receive_text()
        await websocket.send_text(text)
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app).websocket_connect("/ws") as session:
        session.send_text("a long message")
        assert session.receive_text() == "a long message"


def test_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    started: list[bool] = []

    async def lifespan(app: Starlette) -> Any:
        started.append(True)
        yield

    from contextlib import asynccontextmanager

    app = Starlette(lifespan=asynccontextmanager(lifespan), max_body_size=1)
    with test_client_factory(app):
        assert started == [True]


def test_with_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    class Passthrough(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            response: Response = await call_next(request)
            return response

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(Passthrough)],
        max_body_size=5,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"12345").text == "5"
    assert_413(client.post("/", content=b"123456"))


def test_exception_handlers_do_not_see_the_limit(test_client_factory: TestClientFactory) -> None:
    def handler(request: Request, exc: Exception) -> Response:  # pragma: no cover
        return PlainTextResponse("handled", status_code=500)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        exception_handlers={Exception: handler, ValueError: handler},
        max_body_size=1,
    )
    assert_413(test_client_factory(app).post("/", content=b"xx"))


# Multipart.


def multipart_body(file_size: int) -> tuple[bytes, str]:
    boundary = "xxBOUNDARYxx"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="field"\r\n\r\nvalue\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + b"f" * file_size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["file"].read())))  # type: ignore[union-attr]

    body, content_type = multipart_body(100)
    limit = len(body)
    ok = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=limit)
    assert test_client_factory(ok).post("/", content=body, headers={"content-type": content_type}).text == "100"
    # The file data alone fits, the framing overhead pushes the body over.
    tight = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=limit - 1)
    assert_413(test_client_factory(tight).post("/", content=body, headers={"content-type": content_type}))
    small = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=100)
    assert_413(test_client_factory(small).post("/", content=body, headers={"content-type": content_type}))


def test_multipart_via_test_client_files(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            return PlainTextResponse(str(len(form)))

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=300)
    client = test_client_factory(app)
    assert client.post("/", files={"a": ("a.txt", b"x" * 10)}).text == "1"
    assert_413(client.post("/", files={"a": ("a.txt", b"x" * 1000)}))


def test_urlencoded_form_limit(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            return PlainTextResponse(str(len(form)))

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", data={"a": "b"}).text == "1"
    assert_413(client.post("/", data={"a": "b" * 20}))


async def parse_interrupted(error: BaseException, chunks_before: int = 1) -> list[Any]:
    """Feed a multipart parser a body that is interrupted by `error`, return the temp files it opened."""
    body, content_type = multipart_body(5000)
    opened: list[Any] = []

    class Parser(MultiPartParser):
        def on_headers_finished(self) -> None:
            super().on_headers_finished()
            opened.extend(self._files_to_close_on_error)

    async def stream() -> Any:
        yield body[:300]
        raise error

    gen = stream()
    try:
        with pytest.raises(type(error)):
            await Parser(Headers({"content-type": content_type}), gen).parse()
    finally:
        await gen.aclose()
    return opened


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_limit_crossed() -> None:
    opened = await parse_interrupted(BodyTooLarge())
    assert opened
    assert all(f.closed for f in opened)


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_any_interruption() -> None:
    opened = await parse_interrupted(KeyboardInterrupt())
    assert opened
    assert all(f.closed for f in opened)


@pytest.mark.anyio
async def test_multipart_temp_files_closed_through_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Any] = []

    class Parser(MultiPartParser):
        def on_headers_finished(self) -> None:
            super().on_headers_finished()
            opened.extend(self._files_to_close_on_error)

    monkeypatch.setattr("starlette.requests.MultiPartParser", Parser)

    async def endpoint(request: Request) -> Response:
        async with request.form():
            pass  # pragma: no cover
        return PlainTextResponse("ok")  # pragma: no cover

    body, content_type = multipart_body(5000)
    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=len(body) - 1)
    sent, _ = await drive(
        app, [body[:300], body[300:2000], body[2000:]], headers=[(b"content-type", content_type.encode())]
    )
    assert status_of(sent) == 413
    assert opened
    assert all(f.closed for f in opened)


# Nested layers.


def test_streamed_body_in_mounted_starlette(test_client_factory: TestClientFactory) -> None:
    handled: list[Exception] = []

    def error_handler(request: Request, exc: Exception) -> Response:  # pragma: no cover
        handled.append(exc)
        return PlainTextResponse("handled", status_code=500)

    def inner(**kwargs: Any) -> Starlette:
        return Starlette(routes=[Route("/", read_body, methods=["POST"], **kwargs.pop("route", {}))], **kwargs)

    cases = [
        Starlette(routes=[Mount("/a", app=inner(exception_handlers={500: error_handler}))], max_body_size=10),
        Starlette(routes=[Mount("/a", app=inner(max_body_size=10, exception_handlers={500: error_handler}))]),
        Starlette(
            routes=[Mount("/a", app=inner(route={"max_body_size": 10}, exception_handlers={500: error_handler}))]
        ),
    ]
    for app in cases:
        client = test_client_factory(app)
        assert_413(client.post("/a/", content=chunks(3, 8)))
    assert handled == []


def test_streamed_body_under_middleware_wrapping_starlette(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert_413(client.post("/", content=chunks(3, 8)))
    assert client.post("/", content=chunks(3, 4)).text == "7"


def test_middleware_limit_is_a_cap(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)])
    for app in (
        RequestBodyLimitMiddleware(inner, max_body_size=10),
        Starlette(
            routes=[Route("/", read_body, methods=["POST"], max_body_size=1000)],
            middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
        ),
    ):
        client = test_client_factory(app)
        assert_413(client.post("/", content=b"x" * 50))
        assert_413(client.post("/", content=chunks(30, 20)))
        assert client.post("/", content=chunks(5, 5)).text == "10"


def test_mount_of_wrapped_starlette_defers_to_inner_route(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    app = Starlette(routes=[Mount("/a", app=GZipMiddleware(inner))], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/a/", content=b"x" * 50).text == "50"
    assert_413(client.post("/a/", content=b"x" * 101))


@pytest.mark.parametrize("factory", [Route, Mount, Router, Starlette])
def test_invalid_max_body_size(factory: Any) -> None:
    args: tuple[Any, ...] = ("/", ignore_body) if factory is Route else ("/m", Router()) if factory is Mount else ()
    with pytest.raises(ValueError):
        factory(*args, max_body_size=-1)
    with pytest.raises(TypeError):
        factory(*args, max_body_size="10")


def test_starlette_docstring_kept() -> None:
    assert Starlette.__doc__ == "Creates an Starlette application."


def test_middleware_without_limit_inside_limited_app(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=None)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert_413(client.post("/", content=b"x" * 11))


class AttributeWrapper:
    def __init__(self, application: Any) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.application(scope, receive, send)


def closure_wrap(app: Any) -> Any:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)

    return asgi


def test_mount_of_wrapped_starlette_with_app_attribute(test_client_factory: TestClientFactory) -> None:
    class Wrapper(AttributeWrapper):
        def __init__(self, app: Any) -> None:
            super().__init__(app)
            self.app = app

    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    app = Starlette(routes=[Mount("/a", app=Wrapper(inner))], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/a/", content=b"x" * 50).text == "50"
    assert_413(client.post("/a/", content=b"x" * 101))


@pytest.mark.parametrize("wrap", [closure_wrap, AttributeWrapper])
def test_streamed_body_through_opaque_wrapper_uses_inner_limit(
    test_client_factory: TestClientFactory, wrap: Any
) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    app = Starlette(routes=[Mount("/a", app=wrap(inner))], max_body_size=10)
    assert test_client_factory(app).post("/a/", content=chunks(10, 40)).text == "50"


def test_mounted_plain_app_not_run_for_declared_length_over_limit(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover

    for app in (
        Starlette(routes=[Mount("/m", app=asgi)], max_body_size=10),
        Starlette(routes=[Mount("/m", app=asgi, max_body_size=10)]),
    ):
        assert_413(test_client_factory(app).post("/m/", content=b"x" * 50))
    assert calls == []


@pytest.mark.anyio
async def test_mounted_app_that_handles_errors_gets_single_413() -> None:
    from starlette.middleware.errors import ServerErrorMiddleware

    async def reader(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()  # pragma: no cover

    app = Starlette(routes=[Mount("/m", ServerErrorMiddleware(reader))], max_body_size=10)
    sent, _ = await drive(app, [b"x" * 50], content_length=50, path="/m/")
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


def test_opaque_mounted_app_that_sends_first(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Mount("/m", asgi)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 10).text == "ok"
    assert_413(client.post("/m/", content=b"x" * 11))
