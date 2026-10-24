from __future__ import annotations

import tempfile
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import BaseRoute, Match, Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory

calls: list[str] = []


async def read(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def stream_body(size: int, chunk: int = 10) -> Iterator[bytes]:
    for _ in range(size // chunk):
        yield b"x" * chunk


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=100))
    assert client.post("/", content=b"x" * 100).text == "100"
    response = client.post("/", content=b"x" * 101)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert calls == ["/"]


def test_streamed_body_without_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=100))
    assert client.post("/", content=stream_body(100)).text == "100"
    response = client.post("/", content=stream_body(200))
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_understated_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=100))
    response = client.post("/", content=b"x" * 200, headers={"content-length": "10"})
    assert response.status_code == 413


def test_requests_without_body(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", read)], max_body_size=0))
    assert client.get("/").text == "0"


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    router = Router(routes=[Route("/", read, methods=["POST"])], max_body_size=10)
    client = test_client_factory(router)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=stream_body(20)).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    route = Route("/", read, methods=["POST"], max_body_size=10)
    client = test_client_factory(route)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=stream_body(20)).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/m", routes=[Route("/", read, methods=["POST"])], max_body_size=10),
            Route("/", read, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/m/", content=b"x" * 10).status_code == 200
    assert client.post("/m/", content=b"x" * 11).status_code == 413
    assert client.post("/", content=b"x" * 1000).status_code == 200


def test_mount_limit_plain_asgi_app(test_client_factory: TestClientFactory) -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("asgi")
        await PlainTextResponse("ok")(scope, receive, send)

    client = test_client_factory(Starlette(routes=[Mount("/m", asgi, max_body_size=10)]))
    assert client.post("/m/", content=b"x" * 10).status_code == 200
    calls.clear()
    assert client.post("/m/", content=b"x" * 11).status_code == 413
    assert calls == []


def test_inner_limits_override_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/raise", read, methods=["POST"], max_body_size=1000),
            Route("/lower", read, methods=["POST"], max_body_size=10),
            Route("/inherit", read, methods=["POST"]),
            Mount(
                "/mount",
                routes=[
                    Route("/raise", read, methods=["POST"], max_body_size=1000),
                    Route("/inherit", read, methods=["POST"]),
                ],
                max_body_size=50,
            ),
            Mount("/router", app=Router(routes=[Route("/", read, methods=["POST"])], max_body_size=500)),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)

    assert client.post("/raise", content=b"x" * 500).status_code == 200
    assert client.post("/raise", content=b"x" * 1001).status_code == 413
    assert client.post("/raise", content=stream_body(500)).status_code == 200
    assert client.post("/raise", content=stream_body(1010)).status_code == 413

    assert client.post("/lower", content=b"x" * 10).status_code == 200
    assert client.post("/lower", content=b"x" * 11).status_code == 413
    assert client.post("/lower", content=stream_body(20)).status_code == 413

    assert client.post("/inherit", content=b"x" * 100).status_code == 200
    assert client.post("/inherit", content=b"x" * 101).status_code == 413

    assert client.post("/mount/inherit", content=b"x" * 50).status_code == 200
    assert client.post("/mount/inherit", content=b"x" * 51).status_code == 413
    assert client.post("/mount/raise", content=b"x" * 500).status_code == 200
    assert client.post("/router/", content=b"x" * 500).status_code == 200
    assert client.post("/router/", content=b"x" * 501).status_code == 413


def test_content_length_fails_fast_after_routing(test_client_factory: TestClientFactory) -> None:
    ran: list[str] = []

    async def middleware_app(scope: Scope, receive: Receive, send: Send) -> None:
        ran.append("route-middleware")
        await PlainTextResponse("ok")(scope, receive, send)

    class Recorder:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            ran.append("middleware")
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/raise", read, methods=["POST"], max_body_size=1000),
            Route("/plain", read, methods=["POST"]),
            Route("/guarded", read, methods=["POST"], middleware=[Middleware(Recorder)]),
            Mount("/m", routes=[Route("/r", read, methods=["POST"], max_body_size=1000)], max_body_size=10),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)

    # Declared length over the application limit, but the route raises it.
    assert client.post("/raise", content=b"x" * 500).status_code == 200
    assert client.post("/m/r", content=b"x" * 500).status_code == 200
    assert calls == ["/raise", "/m/r"]
    calls.clear()

    assert client.post("/plain", content=b"x" * 101).status_code == 413
    assert client.post("/guarded", content=b"x" * 101).status_code == 413
    assert client.post("/raise", content=b"x" * 1001).status_code == 413
    assert client.post("/m/r", content=b"x" * 1001).status_code == 413
    assert client.post("/nothing", content=b"x" * 101).status_code == 413
    assert client.post("/m/nothing", content=b"x" * 101).status_code == 413
    assert client.get("/plain", headers={"content-length": "101"}).status_code == 413  # no method match
    assert client.post("/plain/", content=b"x" * 101).status_code == 413  # redirect_slashes
    assert calls == []
    assert ran == []

    # No route, no method: the limit that applies decides.
    assert client.post("/nothing", content=b"x" * 100).status_code == 404
    assert client.get("/plain", headers={"content-length": "100"}).status_code == 405


def test_route_with_own_limit_does_not_apply_to_405(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read, methods=["POST"], max_body_size=1000)], max_body_size=10)
    client = test_client_factory(app)
    assert client.put("/", content=b"x" * 11).status_code == 413
    assert client.put("/", content=b"x" * 10).status_code == 405


def test_content_length_rejection_runs_no_application_code(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert calls == []


def test_non_numeric_content_length_is_ignored_by_fail_fast(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    scope: Scope = {"type": "http", "headers": [(b"content-length", b"abc")]}
    sent: list[Message] = []

    async def receive() -> Message:  # pragma: no cover
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=1), scope, receive, send)
    assert sent[0]["status"] == 200


def test_middleware_standalone(test_client_factory: TestClientFactory) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append("app")
        total = 0
        more = True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await PlainTextResponse(str(total))(scope, receive, send)

    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    calls.clear()
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert calls == []  # fail fast on entry
    response = client.post("/", content=stream_body(20))
    assert (response.status_code, response.text) == (413, "Content Too Large")
    response = client.post("/", content=b"x" * 100, headers={"content-length": "1"})
    assert response.status_code == 413


def test_middleware_limit_is_not_changed_by_inner_routes(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read, methods=["POST"], max_body_size=1000)])
    client = test_client_factory(RequestBodyLimitMiddleware(app, max_body_size=10))
    assert client.post("/", content=b"x" * 11).status_code == 413
    assert calls == []
    assert client.post("/", content=b"x" * 10).status_code == 200


def test_middleware_on_starlette(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[Route("/", read, methods=["POST"])],
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=10)],
    )
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).status_code == 200
    assert client.post("/", content=stream_body(20)).status_code == 413


@pytest.mark.anyio
async def test_middleware_ignores_other_scopes() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestBodyLimitMiddleware(app, max_body_size=1)
    await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    await middleware({"type": "websocket"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan", "websocket"]


def test_websocket_unaffected(test_client_factory: TestClientFactory) -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[WebSocketRoute("/ws", ws)], max_body_size=1)
    with test_client_factory(app).websocket_connect("/ws") as session:
        session.send_text("hello world")
        assert session.receive_text() == "hello world"


def test_lifespan_unaffected(test_client_factory: TestClientFactory) -> None:
    app = Starlette(max_body_size=1)
    with test_client_factory(app):
        pass


def test_response_already_started(test_client_factory: TestClientFactory) -> None:
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        with pytest.raises(Exception):
            while True:
                await receive()

    messages = iter([{"type": "http.request", "body": b"x" * 11, "more_body": True}])

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), {"type": "http", "headers": []}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert sent[0]["status"] == 200


def test_no_second_response_after_413(test_client_factory: TestClientFactory) -> None:
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except Exception:
            await PlainTextResponse("handled", status_code=400)(scope, receive, send)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 11}

    async def send(message: Message) -> None:
        sent.append(message)

    import anyio

    anyio.run(RequestBodyLimitMiddleware(app, max_body_size=10), {"type": "http", "headers": []}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 413


def multipart_body(file_size: int) -> tuple[bytes, dict[str, str]]:
    boundary = "bnd"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + b"y" * file_size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


async def upload(request: Request) -> Response:
    async with request.form() as form:
        upload_file = form["f"]
        return PlainTextResponse(str(len(await upload_file.read())))  # type: ignore[union-attr]


def test_multipart(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300))
    body, headers = multipart_body(100)
    assert client.post("/", content=body, headers=headers).text == "100"

    # Counts the framing as well as the file data.
    body, headers = multipart_body(280)
    assert len(body) > 300
    response = client.post("/", content=body, headers=headers)
    assert response.status_code == 413

    body, headers = multipart_body(280)

    def chunks() -> Iterator[bytes]:
        for i in range(0, len(body), 20):
            yield body[i : i + 20]

    assert client.post("/", content=chunks(), headers=headers).status_code == 413


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_body_limit_crossed(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=300)
    body, headers = multipart_body(280)
    messages: Iterator[Message] = iter(
        {"type": "http.request", "body": body[i : i + 20], "more_body": i + 20 < len(body)}
        for i in range(0, len(body), 20)
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "root_path": "",
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert created[0].closed


@pytest.mark.anyio
async def test_multipart_parser_closes_files_on_any_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        file = original(*args, **kwargs)
        created.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking)
    body, headers = multipart_body(50)

    class Interrupted(Exception):
        pass

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body[:120]
        raise Interrupted

    parser = MultiPartParser(Headers(headers), stream())
    with pytest.raises(Interrupted):
        await parser.parse()
    assert len(created) == 1
    assert created[0].closed


@pytest.mark.anyio
async def test_receive_keeps_failing_and_foreign_errors_propagate() -> None:
    sent: list[Message] = []
    errors: list[BaseException] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        with pytest.raises(Exception) as first:
            await receive()
        errors.append(first.value)
        with pytest.raises(type(first.value)):
            await receive()
        raise RuntimeError("unrelated")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 11}

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = RequestBodyLimitMiddleware(app, max_body_size=10)
    with pytest.raises(RuntimeError, match="unrelated"):
        await middleware({"type": "http", "headers": []}, receive, send)
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_exception_groups_of_body_errors_are_swallowed() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: receive())  # type: ignore[arg-type,return-value]

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 11}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await RequestBodyLimitMiddleware(app, max_body_size=10)({"type": "http", "headers": []}, receive, send)
    assert sent[0]["status"] == 413


def test_custom_route_is_settled_by_router(test_client_factory: TestClientFactory) -> None:
    class CustomRoute(BaseRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            return Match.FULL, {}

        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append("custom")
            await PlainTextResponse("ok")(scope, receive, send)

    class PartialRoute(CustomRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            return Match.PARTIAL, {}

    for route in (CustomRoute(), PartialRoute()):
        client = test_client_factory(Starlette(routes=[route], max_body_size=10))
        assert client.post("/", content=b"x" * 11).status_code == 413
        assert calls == []
        assert client.post("/", content=b"x" * 10).status_code == 200
        calls.clear()


@pytest.mark.anyio
async def test_disconnect_passes_through() -> None:
    seen: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(await receive())

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    await RequestBodyLimitMiddleware(app, max_body_size=0)({"type": "http", "headers": []}, receive, None)  # type: ignore[arg-type]
    assert seen == [{"type": "http.disconnect"}]
