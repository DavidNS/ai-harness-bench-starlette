from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


async def read_body(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def chunks(*sizes: int) -> Iterator[bytes]:
    for size in sizes:
        yield b"x" * size


def assert_too_large(response: Any) -> None:
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/").text == "0"
    assert_too_large(client.post("/", content=chunks(5, 6)))
    assert client.get("/").status_code == 405


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_understated_content_length_does_not_let_body_through(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    response = client.post("/", content=chunks(6, 6), headers={"content-length": "1"})
    assert_too_large(response)


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/", content=chunks(3, 3)))


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ],
        max_body_size=20,
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 15).text == "15"
    assert client.post("/m/", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/m/", content=chunks(3, 3)))
    assert_too_large(client.post("/", content=chunks(10, 11)))


def test_mount_of_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")  # pragma: no cover

    app = Starlette(routes=[Mount("/m", app=asgi)], max_body_size=5)
    client = test_client_factory(app)
    assert_too_large(client.post("/m/", content=b"x" * 6))
    assert calls == []


def test_route_limit_raises_and_lowers(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read_body, methods=["POST"], max_body_size=100),
            Route("/lower", read_body, methods=["POST"], max_body_size=5),
            Route("/inherit", read_body, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/raise", content=b"x" * 50).text == "50"
    assert_too_large(client.post("/raise", content=b"x" * 101))
    assert_too_large(client.post("/raise", content=chunks(60, 60)))
    assert client.post("/lower", content=b"x" * 5).text == "5"
    assert_too_large(client.post("/lower", content=b"x" * 6))
    assert_too_large(client.post("/lower", content=chunks(3, 3)))
    assert client.post("/inherit", content=b"x" * 10).text == "10"
    assert_too_large(client.post("/inherit", content=chunks(6, 6)))


def test_nested_inner_limits_override(test_client_factory: TestClientFactory) -> None:
    inner = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)
    app = Starlette(routes=[Mount("/m", app=inner, max_body_size=5)], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 40).text == "40"
    assert_too_large(client.post("/m/", content=b"x" * 51))


def test_content_length_fail_fast_in_app(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/", read_body, methods=["POST"]),
            Route("/big", read_body, methods=["POST"], max_body_size=100),
            Route("/small", read_body, methods=["POST"], max_body_size=3),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=100),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert_too_large(client.post("/", content=b"x" * 11))
    assert_too_large(client.post("/small", content=b"x" * 4))
    assert_too_large(client.post("/missing", content=b"x" * 11))  # no route
    assert_too_large(client.put("/", content=b"x" * 11))  # no method
    assert_too_large(client.post("/big", content=b"x" * 101))
    assert_too_large(client.post("/m/", content=b"x" * 101))
    assert calls == []
    # An inner route, mount or router may raise the limit above the declared length.
    assert client.post("/big", content=b"x" * 50).text == "50"
    assert client.post("/m/", content=b"x" * 50).text == "50"
    # No route and no method answer with their usual response when within the limit.
    assert client.post("/missing", content=b"x" * 5).status_code == 404
    assert client.put("/", content=b"x" * 5).status_code == 405


def test_standalone_middleware(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("called")
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        await PlainTextResponse(str(len(body)))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.get("/").text == "0"
    calls.clear()
    # Rejected on entry: the application never runs.
    assert_too_large(client.post("/", content=b"x" * 11))
    assert calls == []
    assert_too_large(client.post("/", content=chunks(6, 6)))


def test_standalone_middleware_without_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read_body, methods=["POST"])])
    client = test_client_factory(RequestBodyLimitMiddleware(router, None))
    assert client.post("/", content=b"x" * 1000).text == "1000"


def test_invalid_limit() -> None:
    with pytest.raises(ValueError):
        RequestBodyLimitMiddleware(Router(), max_body_size=-1)


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while (await receive()).get("more_body", False):
                pass  # pragma: no cover
        finally:
            calls.append("stopped")
        await send({"type": "http.response.body", "body": b"ok"})  # pragma: no cover

    sent: list[Any] = []

    async def run() -> None:
        messages = [
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"", "more_body": False},
        ]

        async def receive() -> Any:
            return messages.pop(0)

        async def send(message: Any) -> None:
            sent.append(message)

        await RequestBodyLimitMiddleware(asgi, max_body_size=10)({"type": "http", "headers": []}, receive, send)

    anyio.run(run)
    assert calls == ["stopped"]
    assert [m["type"] for m in sent] == ["http.response.start"]


def test_app_swallowing_error_still_gets_413(test_client_factory: TestClientFactory) -> None:
    async def swallow(request: Request) -> Response:
        try:
            await request.body()
        except Exception:
            pass
        return PlainTextResponse("late")

    # The response is only started after the endpoint returns, so the 413 is sent
    # by the middleware when the handler's own response is not.
    async def swallow_no_response(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await Request(scope, receive).body()
        except Exception:
            pass

    client = test_client_factory(RequestBodyLimitMiddleware(swallow_no_response, max_body_size=3))
    assert_too_large(client.post("/", content=chunks(2, 2)))


def test_response_started_is_not_replaced(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async def gen() -> Any:
            yield b"a"
            try:
                async for _ in request.stream():
                    pass  # pragma: no cover
            except Exception:
                calls.append("stopped")
                raise
            yield b"b"  # pragma: no cover

        return StreamingResponse(gen())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=3)
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.post("/", content=chunks(2, 2))
    assert response.status_code == 200
    assert calls == ["stopped"]


def test_json_endpoint(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        return JSONResponse(await request.json())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=20)
    client = test_client_factory(app)
    assert client.post("/", json={"a": 1}).json() == {"a": 1}
    assert_too_large(client.post("/", content=chunks(15, 15)))


def test_websocket_and_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app) as client:  # runs lifespan
        with client.websocket_connect("/ws") as session:
            session.send_text("a long message")
            assert session.receive_text() == "a long message"


def test_multipart_counts_whole_body(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["file"].read())))  # type: ignore[union-attr]

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=500)
    client = test_client_factory(app)
    # 300 bytes of file data plus multipart framing is within the limit...
    assert client.post("/", files={"file": ("a.txt", b"x" * 300)}).text == "300"
    # ... but the framing counts too: 480 bytes of file data are over it.
    assert_too_large(client.post("/", files={"file": ("a.txt", b"x" * 480)}))


def test_multipart_temp_files_closed_on_limit(
    test_client_factory: TestClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette import formparsers

    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    async def endpoint(request: Request) -> Response:
        async with request.form() as form:  # pragma: no cover
            return PlainTextResponse(str(len(form)))

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=300)
    body, content_type = _multipart([("a.txt", b"x" * 100), ("b.txt", b"y" * 400)])
    messages = [
        {"type": "http.request", "body": body[:250], "more_body": True},
        {"type": "http.request", "body": body[250:], "more_body": False},
    ]
    sent: list[Any] = []

    async def receive() -> Any:
        return messages.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-type", content_type.encode())],
    }
    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413
    assert created and all(file.closed for file in created)


def _multipart(files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "BOUNDARY"
    body = b""
    for name, data in files:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_multipart_temp_files_closed_on_any_interruption(
    test_client_factory: TestClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    body, content_type = _multipart([("a.txt", b"x" * 10)])
    parser: MultiPartParser

    async def stream() -> Any:
        yield body[:-20]
        raise RuntimeError("interrupted")

    parser = MultiPartParser(Headers({"content-type": content_type}), stream())

    async def run() -> None:
        with pytest.raises(RuntimeError):
            await parser.parse()

    anyio.run(run)
    assert parser._files_to_close_on_error
    assert all(file.closed for file in parser._files_to_close_on_error)
