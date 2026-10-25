from __future__ import annotations

from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.formparsers import MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

called: list[str] = []


async def read_body(request: Request) -> JSONResponse:
    called.append(request.url.path)
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def no_read(request: Request) -> PlainTextResponse:
    called.append(request.url.path)
    return PlainTextResponse("ok")


@pytest.fixture(autouse=True)
def reset_called() -> None:
    called.clear()


def too_large(response: Any) -> bool:
    return bool(response.status_code == 413 and response.text == "Content Too Large")


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert too_large(client.post("/", content=b"x" * 11))
    assert client.get("/", headers={}).status_code == 405
    assert called == ["/"]


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).json() == {"size": 100_000}


def test_chunked_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)

    def chunks() -> Any:
        for _ in range(5):
            yield b"abcd"

    assert too_large(client.post("/", content=chunks()))


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=b"x" * 50, headers={"content-length": "5"})
    assert too_large(response)


def test_content_length_fail_fast_does_not_run_endpoint(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", no_read, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert too_large(client.post("/", content=b"x" * 11))
    assert called == []
    assert client.post("/", content=b"x" * 10).text == "ok"


def test_content_length_fail_fast_waits_for_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/big", no_read, methods=["POST"], max_body_size=100),
            Route("/small", no_read, methods=["POST"], max_body_size=5),
            Route("/inherit", no_read, methods=["POST"]),
            Mount("/m", routes=[Route("/x", no_read, methods=["POST"])], max_body_size=50),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/big", content=b"x" * 50).text == "ok"
    assert too_large(client.post("/big", content=b"x" * 101))
    assert too_large(client.post("/small", content=b"x" * 6))
    assert too_large(client.post("/inherit", content=b"x" * 11))
    assert client.post("/m/x", content=b"x" * 40).text == "ok"
    # No route / no method: the application's limit applies, not the route's.
    assert too_large(client.post("/nothing", content=b"x" * 11))
    assert client.post("/nothing", content=b"x" * 10).status_code == 404
    assert too_large(client.put("/big", content=b"x" * 11))
    assert client.put("/big", content=b"x" * 10).status_code == 405
    assert called == ["/big", "/m/x"]


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}
    assert too_large(client.post("/", content=b"x" * 11))
    assert too_large(client.post("/missing", content=b"x" * 11))
    assert client.post("/missing", content=b"x").status_code == 404


def test_mount_limit_raises_and_lowers(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/raise", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
            Mount("/lower", routes=[Route("/", read_body, methods=["POST"])], max_body_size=3),
            Mount("/inherit", routes=[Route("/", read_body, methods=["POST"])]),
            Mount("/router", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raise/", content=b"x" * 100).json() == {"size": 100}
    assert too_large(client.post("/raise/", content=b"x" * 101))
    assert client.post("/lower/", content=b"x" * 3).json() == {"size": 3}
    assert too_large(client.post("/lower/", content=b"x" * 4))
    assert too_large(client.post("/inherit/", content=b"x" * 11))
    assert client.post("/router/", content=b"x" * 50).json() == {"size": 50}
    assert too_large(client.post("/router/", content=b"x" * 51))


def test_mount_with_opaque_app(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        called.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    app = Starlette(routes=[Mount("/a", app=asgi), Mount("/b", app=asgi, max_body_size=20)], max_body_size=10)
    client = test_client_factory(app)
    assert too_large(client.post("/a/", content=b"x" * 11))
    assert client.post("/b/", content=b"x" * 20).text == "ok"
    assert too_large(client.post("/b/", content=b"x" * 21))
    assert called == ["asgi"]


def test_route_limit_without_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_body, methods=["POST"], max_body_size=4),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert too_large(client.post("/limited", content=b"x" * 5))
    assert client.post("/free", content=b"x" * 1000).json() == {"size": 1000}


def test_nested_application_overrides(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=100)
    plain_inner = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    app = Starlette(routes=[Mount("/in", app=inner), Mount("/plain", app=plain_inner)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/in/", content=b"x" * 100).json() == {"size": 100}
    assert too_large(client.post("/plain/", content=b"x" * 11))


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        async for chunk in Request(scope, receive).stream():
            pass
        await send({"type": "http.response.body", "body": b"done"})  # pragma: no cover

    sent: list[Any] = []

    async def run() -> None:
        messages = [{"type": "http.request", "body": b"x" * 8, "more_body": True}] * 2

        async def receive() -> Any:
            return messages.pop(0)

        async def send(message: Any) -> None:
            sent.append(message)

        await RequestBodyLimitMiddleware(app, max_body_size=10)({"type": "http", "headers": []}, receive, send)

    import anyio

    anyio.run(run)
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_standalone_middleware(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        called.append("asgi")
        body = await Request(scope, receive).body()
        await PlainTextResponse(str(len(body)))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert too_large(client.post("/", content=b"x" * 11))
    assert called == ["asgi"]  # Content-Length rejects on entry.
    assert too_large(client.post("/", content=b"x" * 50, headers={"content-length": "1"}))
    assert client.get("/").text == "0"


def test_standalone_middleware_ignores_inner_limits(test_client_factory: TestClientFactory) -> None:
    inner = Starlette(routes=[Route("/", read_body, methods=["POST"], max_body_size=100)])
    client = test_client_factory(RequestBodyLimitMiddleware(inner, max_body_size=10))
    assert too_large(client.post("/", content=b"x" * 11))


def test_standalone_middleware_websocket_and_lifespan(test_client_factory: TestClientFactory) -> None:
    from starlette.websockets import WebSocket

    async def ws_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            ws = WebSocket(scope, receive, send)
            await ws.accept()
            await ws.send_text("hi")
            await ws.close()
        else:
            while True:  # lifespan
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return

    with test_client_factory(RequestBodyLimitMiddleware(ws_app, max_body_size=1)) as client:
        with client.websocket_connect("/") as ws:
            assert ws.receive_text() == "hi"


def test_user_middleware_reading_body_is_limited(test_client_factory: TestClientFactory) -> None:
    class ReadsBody:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":  # pragma: no branch
                await Request(scope, receive).body()
            await self.app(scope, receive, send)  # pragma: no cover

    app = Starlette(
        routes=[Route("/", no_read, methods=["POST"])], middleware=[Middleware(ReadsBody)], max_body_size=10
    )
    client = test_client_factory(app)
    assert too_large(client.post("/", content=b"x" * 50, headers={"content-length": "1"}))


def multipart_body(size: int) -> tuple[bytes, dict[str, str]]:
    boundary = "bnd"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n'.encode()
        + b"x" * size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


def test_multipart_limit_counts_overhead(test_client_factory: TestClientFactory) -> None:
    async def upload(request: Request) -> JSONResponse:
        async with request.form() as form:
            return JSONResponse({"size": form["f"].size})  # type: ignore[union-attr]

    body, headers = multipart_body(100)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body))
    client = test_client_factory(app)
    assert client.post("/", content=body, headers=headers).json() == {"size": 100}

    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body) - 1)
    # Understate the length so that only the received bytes can reject the request.
    response = test_client_factory(app).post("/", content=body, headers={**headers, "content-length": "1"})
    assert too_large(response)


@pytest.mark.anyio
async def test_multipart_closes_files_on_any_interruption() -> None:
    body, headers = multipart_body(100)

    async def stream() -> Any:
        yield body[:80]
        raise RuntimeError("interrupted")

    from starlette.datastructures import Headers

    parser = MultiPartParser(Headers(headers), stream())
    with pytest.raises(RuntimeError):
        await parser.parse()
    assert parser._files_to_close_on_error
    assert all(f.closed for f in parser._files_to_close_on_error)


def test_with_base_http_middleware(test_client_factory: TestClientFactory) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    async def passthrough(request: Request, call_next: Any) -> Any:
        return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=passthrough)],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert too_large(client.post("/", content=b"x" * 50, headers={"content-length": "1"}))
    assert client.post("/", content=b"x" * 10).json() == {"size": 10}


def test_other_exceptions_and_caught_failures(test_client_factory: TestClientFactory) -> None:
    async def swallows(request: Request) -> PlainTextResponse:
        try:
            await request.body()
        except Exception:
            pass
        with pytest.raises(Exception):
            await request.body()  # The stream stays stopped.
        raise RuntimeError("something else")

    async def boom(request: Request) -> PlainTextResponse:
        raise RuntimeError("boom")

    app = Starlette(
        routes=[Route("/swallows", swallows, methods=["POST"]), Route("/boom", boom, methods=["POST"])],
        max_body_size=10,
    )
    client = test_client_factory(app, raise_server_exceptions=False)
    assert too_large(client.post("/swallows", content=b"x" * 11, headers={"content-length": "1"}))
    assert client.post("/boom", content=b"x").status_code == 500
    with pytest.raises(RuntimeError, match="boom"):
        test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10)).post("/boom", content=b"x")


@pytest.mark.anyio
async def test_disconnect_is_passed_through() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["type"] == "http.disconnect"
        await PlainTextResponse("ok")(scope, receive, send)

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        pass

    await RequestBodyLimitMiddleware(app, max_body_size=1)({"type": "http", "headers": []}, receive, send)


@pytest.mark.anyio
async def test_scope_reused_between_requests() -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    scope: Scope = {"type": "http", "method": "POST", "path": "/", "root_path": "", "headers": []}
    statuses: list[int] = []

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    for size in (10, 11):
        messages = [{"type": "http.request", "body": b"x" * size}]

        async def receive() -> Any:
            return messages.pop(0)

        await router(scope, receive, send)
    assert statuses == [200, 413]


def test_content_length_header_case_and_whitespace(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        called.append("asgi")  # pragma: no cover

    app = RequestBodyLimitMiddleware(asgi, max_body_size=10)
    for raw in ((b"Content-Length", b"51"), (b"content-length", b" 51 ")):
        sent: list[Any] = []

        async def send(message: Any) -> None:
            sent.append(message)

        async def receive() -> Any:
            raise AssertionError  # pragma: no cover

        import anyio

        anyio.run(app, {"type": "http", "headers": [raw]}, receive, send)
        assert sent[0]["status"] == 413
    assert called == []
