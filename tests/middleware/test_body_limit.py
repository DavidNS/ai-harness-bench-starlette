from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from typing import Any
from unittest import mock

import httpx
import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def read_body(request: Request) -> JSONResponse:
    calls.append(request.url.path)
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def ignore_body(request: Request) -> PlainTextResponse:
    calls.append(request.url.path)
    return PlainTextResponse("ok")


async def read_form(request: Request) -> JSONResponse:
    calls.append(request.url.path)
    async with request.form() as form:
        return JSONResponse({"keys": list(form.keys())})


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    calls.clear()


def encode_multipart(files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    request = httpx.Request("POST", "http://testserver/", files=files)
    return request.read(), request.headers["content-type"]


def chunks(*sizes: int) -> Iterator[bytes]:
    for size in sizes:
        yield b"x" * size


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/", content=b"x" * 11))
    # No Content-Length: the received bytes are counted.
    assert client.post("/", content=chunks(5, 5)).json() == {"size": 10}
    assert_too_large(client.post("/", content=chunks(5, 3, 3)))
    assert client.get("/", headers={}).status_code == 405


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        # The server claims 2 bytes but delivers 12.
        scope["headers"] = [(b"content-length", b"2")]
        await Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)(scope, receive, send)

    assert_too_large(test_client_factory(app).post("/", content=b"x" * 12))


def test_content_length_fail_fast(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=10)
    assert_too_large(test_client_factory(app).post("/", content=b"x" * 11))
    assert calls == []


def test_content_length_waits_for_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", ignore_body, methods=["POST"], max_body_size=100),
            Route("/lower", ignore_body, methods=["POST"], max_body_size=5),
            Route("/inherit", ignore_body, methods=["POST"]),
            Mount("/m", routes=[Route("/x", ignore_body, methods=["POST"])], max_body_size=100),
            Mount(
                "/outer",
                routes=[Mount("/inner", routes=[Route("/x", ignore_body, methods=["POST"], max_body_size=100)])],
            ),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    body = b"x" * 50
    assert client.post("/raise", content=body).status_code == 200
    assert client.post("/m/x", content=body).status_code == 200
    assert client.post("/outer/inner/x", content=body).status_code == 200
    assert calls == ["/raise", "/m/x", "/outer/inner/x"]
    calls.clear()

    assert_too_large(client.post("/lower", content=b"x" * 6))
    assert_too_large(client.post("/inherit", content=b"x" * 11))
    assert calls == []
    # No route, and no method: the application's limit applies.
    assert_too_large(client.post("/missing", content=b"x" * 11))
    assert client.post("/missing", content=b"x" * 10).status_code == 404
    assert_too_large(client.request("GET", "/lower", content=b"x" * 11))
    assert client.request("GET", "/lower", content=b"x" * 10).status_code == 405
    assert calls == []


def test_inner_limits_count_streamed_bytes(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_body, methods=["POST"], max_body_size=100),
            Route("/lower", read_body, methods=["POST"], max_body_size=5),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raise", content=chunks(30, 30)).json() == {"size": 60}
    assert_too_large(client.post("/raise", content=chunks(60, 60)))
    assert_too_large(client.post("/lower", content=chunks(3, 3)))
    assert client.post("/lower", content=chunks(3, 2)).json() == {"size": 5}


def test_route_limit_without_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_body, methods=["POST"], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/limited", content=b"x" * 6))
    assert calls == []
    assert_too_large(client.post("/limited", content=chunks(3, 3)))
    assert client.post("/free", content=b"x" * 1000).json() == {"size": 1000}


def test_mount_and_router_limits(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/mount", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
            Mount(
                "/router",
                app=Router([Route("/", read_body, methods=["POST"])], max_body_size=100),
            ),
            Mount("/asgi", app=PlainTextResponse("ok"), max_body_size=5),
            Route("/", read_body, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/mount/", content=b"x" * 6))
    assert_too_large(client.post("/mount/", content=chunks(3, 3)))
    assert client.post("/router/", content=b"x" * 50).json() == {"size": 50}
    assert_too_large(client.post("/router/", content=b"x" * 101))
    assert_too_large(client.post("/asgi/", content=b"x" * 6))
    assert_too_large(client.post("/", content=b"x" * 11))
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}


def test_router_and_route_without_starlette() -> None:
    router = Router(
        [Route("/", read_body, methods=["POST"]), Route("/r", read_body, methods=["POST"], max_body_size=3)],
        max_body_size=10,
    )
    client = TestClient(router)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/", content=chunks(6, 6)))
    assert_too_large(client.post("/r", content=b"x" * 4))
    assert client.post("/r", content=b"x" * 3).json() == {"size": 3}
    assert calls == ["/", "/", "/r"]


def test_standalone_middleware_on_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    ran = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal ran
        ran = True
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.get("/").text == "0"
    assert_too_large(client.post("/", content=chunks(6, 6)))
    ran = False
    assert_too_large(client.post("/", content=b"x" * 11))  # rejected on entry
    assert ran is False


def test_standalone_middleware_in_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"], max_body_size=100)],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    # The route cannot raise the middleware's limit.
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == ["/"]


def test_non_http_scopes_unaffected() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] == "lifespan"
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})

    with TestClient(RequestBodyLimitMiddleware(app, max_body_size=0)):
        pass

    async def ws_endpoint(websocket: Any) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    from starlette.routing import WebSocketRoute

    starlette = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)], max_body_size=0)
    with TestClient(starlette) as client, client.websocket_connect("/ws") as session:
        session.send_text("hello" * 10)
        assert session.receive_text() == "hello" * 10


def test_no_body_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", ignore_body, methods=["GET", "POST"])], max_body_size=0)
    client = test_client_factory(app)
    assert client.get("/").status_code == 200
    assert client.post("/").status_code == 200
    assert_too_large(client.post("/", content=b"x"))


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    events: list[str] = []

    class Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await PlainTextResponse("first")(scope, receive, send)
            try:
                await Request(scope, receive).body()
            except Exception as exc:
                events.append(type(exc).__name__)

    app = Starlette(routes=[Route("/", Endpoint(), methods=["POST"])], max_body_size=10)
    response = test_client_factory(app).post("/", content=chunks(6, 6))
    assert response.status_code == 200
    assert response.text == "first"
    assert events == ["ClientDisconnect"]


def test_response_started_raw_asgi() -> None:
    sent: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive())["type"] == "http.request":
            pass
        await send({"type": "http.response.body", "body": b"done"})

    async def run() -> None:
        messages = [{"type": "http.request", "body": b"x" * 6, "more_body": True}] * 2

        async def receive() -> Any:
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message: Any) -> None:
            sent.append(message["type"] + str(message.get("status", "")))

        scope = {"type": "http", "headers": [], "method": "POST", "path": "/"}
        await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)

    import anyio

    anyio.run(run)
    # No second response: only the application's own start and body.
    assert sent == ["http.response.start200", "http.response.body"]


def test_swallowed_error_still_gives_413(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        try:
            await request.body()
        except Exception:
            pass
        return PlainTextResponse("fine")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    assert_too_large(test_client_factory(app).post("/", content=chunks(6, 6)))


def test_multipart_counts_encoding_overhead(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])], max_body_size=400)
    client = test_client_factory(app)
    # 100 bytes of file data, but the encoded body is larger than the limit.
    files = {"file": ("a.txt", b"x" * 250)}
    assert_too_large(client.post("/", files=files))
    response = client.post("/", files={"file": ("a.txt", b"x" * 10)})
    assert response.json() == {"keys": ["file"]}


def test_multipart_files_closed_when_interrupted() -> None:
    import anyio

    boundary = "b"
    body = b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n' + b"x" * 100

    class Interrupted(Exception): ...

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body
        raise Interrupted

    async def run() -> list[Any]:
        parser = MultiPartParser(Headers({"content-type": f"multipart/form-data; boundary={boundary}"}), stream())
        with pytest.raises(Interrupted):
            await parser.parse()
        return parser._files_to_close_on_error

    files = anyio.run(run)
    assert len(files) == 1
    assert files[0].closed


def test_multipart_files_closed_on_body_limit() -> None:
    import anyio

    seen: list[Any] = []

    class Spy(MultiPartParser):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen.append(self)

    body, content_type = encode_multipart({"file": ("a.txt", b"x" * 1000)})
    messages = [
        {"type": "http.request", "body": body[i : i + 100], "more_body": i + 100 < len(body)}
        for i in range(0, len(body), 100)
    ]
    sent: list[Any] = []

    async def receive() -> Any:
        return messages.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    async def run() -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-type", content_type.encode())],
        }
        await RequestBodyLimitMiddleware(Router([Route("/", read_form, methods=["POST"])]), max_body_size=400)(
            scope, receive, send
        )

    with mock.patch("starlette.requests.MultiPartParser", Spy):
        anyio.run(run)
    assert sent[0]["status"] == 413
    assert len(seen) == 1
    assert seen[0]._files_to_close_on_error
    assert all(f.closed for f in seen[0]._files_to_close_on_error)


def test_receive_after_limit_keeps_failing(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> PlainTextResponse:
        for _ in range(2):
            with pytest.raises(Exception, match="exceeds"):
                await request.body()
            request._stream_consumed = False
        return PlainTextResponse("fine")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=10)
    assert_too_large(test_client_factory(app).post("/", content=chunks(6, 6)))


def test_outer_middleware_limit_with_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=chunks(6, 3)).json() == {"size": 9}
    assert_too_large(client.post("/", content=chunks(6, 6)))


def test_no_match_uses_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount(
                "/n",
                app=Router([Route("/x", ignore_body, methods=["POST"])], max_body_size=100),
            ),
            Mount("/low", routes=[Route("/x", ignore_body, methods=["POST"])], max_body_size=5),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/n/missing", content=b"x" * 50).status_code == 413
    assert client.post("/n/missing", content=b"x" * 10).status_code == 404
    assert client.request("GET", "/n/x", content=b"x" * 50).status_code == 413
    assert client.request("GET", "/low/x", content=b"x" * 8).status_code == 405
    assert client.post("/low/missing", content=b"x" * 8).status_code == 404


def test_multipart_truncated_body_closes_files() -> None:
    import anyio

    body, content_type = encode_multipart({"f": ("a.txt", b"x" * 500)})
    files: list[Any] = []

    class Spy(MultiPartParser):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            files.append(self)

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body[:300]

    async def run() -> None:
        parser = Spy(Headers({"content-type": content_type, "content-length": str(len(body))}), stream())
        with pytest.raises(MultiPartException):
            await parser.parse()

    anyio.run(run)
    assert files[0]._files_to_close_on_error
    assert all(f.closed for f in files[0]._files_to_close_on_error)


def test_base_http_middleware_streamed_413(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"]), Route("/r", read_body, methods=["POST"], max_body_size=5)],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=lambda request, call_next: call_next(request))],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=chunks(6, 6)))
    assert_too_large(client.post("/r", content=chunks(3, 3)))
    assert client.post("/", content=chunks(5, 5)).json() == {"size": 10}
    standalone = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=8))
    assert_too_large(standalone.post("/", content=chunks(5, 5)))


def test_huge_content_length_header(test_client_factory: TestClientFactory) -> None:
    huge = "9" * 5000
    unlimited = test_client_factory(Starlette(routes=[Route("/", ignore_body, methods=["POST"])]))
    assert unlimited.post("/", headers={"content-length": huge}, content=b"").status_code == 200
    limited = test_client_factory(Starlette(routes=[Route("/", ignore_body, methods=["POST"])], max_body_size=10))
    assert_too_large(limited.post("/", headers={"content-length": huge}, content=b""))


def test_inner_router_behind_middleware_raises_limit(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.gzip import GZipMiddleware

    inner = GZipMiddleware(Router([Route("/", read_body, methods=["POST"])], max_body_size=100))
    app = Starlette(routes=[Mount("/m", app=inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/m/", content=chunks(25, 25)).json() == {"size": 50}
    assert_too_large(client.post("/m/", content=b"x" * 101))


def test_no_match_streamed_uses_application_limit() -> None:
    seen: list[int] = []

    async def default(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(len(await Request(scope, receive).body()))
        await PlainTextResponse("nope", status_code=404)(scope, receive, send)

    inner = Router([Route("/x", ignore_body, methods=["POST"])], max_body_size=100, default=default)
    app = Starlette(routes=[Mount("/m", app=inner)], max_body_size=10)
    client = TestClient(app)
    assert_too_large(client.post("/m/missing", content=chunks(25, 25)))
    assert seen == []
    assert client.post("/m/missing", content=b"x" * 10).status_code == 404
    assert seen == [10]


def test_multipart_unrelated_behaviour_unchanged(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])])
    client = test_client_factory(app)
    headers = {"content-type": "multipart/form-data; boundary=abc"}
    assert client.post("/", content=b"", headers=headers).json() == {"keys": []}
    unclosed = b'--abc\r\nContent-Disposition: form-data; name="a"\r\n\r\nvalue\r\n'
    assert client.post("/", content=unclosed, headers=headers).status_code == 200


def test_non_digit_content_length_ignored() -> None:
    import anyio

    sent: list[Any] = []

    async def receive() -> Any:
        return {"type": "http.request", "body": b"x"}  # pragma: no cover

    async def send(message: Any) -> None:
        sent.append(message)

    async def run() -> None:
        scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"abc")]}
        await RequestBodyLimitMiddleware(PlainTextResponse("ok"), max_body_size=10)(scope, receive, send)

    anyio.run(run)
    assert sent[0]["status"] == 200


def test_multipart_declared_length_edge_cases(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_form, methods=["POST"])])
    client = test_client_factory(app)
    body, content_type = encode_multipart({"f": ("a.txt", b"x" * 100)})
    # A well-formed body whose Content-Length is off is still accepted.
    headers = {"content-type": content_type, "content-length": str(len(body) + 50)}
    assert client.post("/", content=body, headers=headers).json() == {"keys": ["f"]}
    # A non-ASCII "digit" in the header must not crash the parser.
    import anyio

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body

    async def run() -> int:
        raw = [(b"content-type", content_type.encode()), (b"content-length", b"\xb2")]
        form = await MultiPartParser(Headers(raw=raw), stream()).parse()
        size = len(form)
        await form.close()
        return size

    assert anyio.run(run) == 1
