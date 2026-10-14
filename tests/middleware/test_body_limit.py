from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def test_app_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_no_limit_by_default(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", read_body, methods=["POST"])])
    assert test_client_factory(app).post("/", content=b"x" * 100_000).text == "100000"


def test_form_is_limited(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        await request.form()
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=20)
    client = test_client_factory(app)
    assert client.post("/", data={"a": "b"}).text == "ok"
    assert client.post("/", data={"a": "b" * 50}).status_code == 413


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=5)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/free", read_body, methods=["POST"]),
            Mount("/m", routes=[Route("/", read_body, methods=["POST"])], max_body_size=5),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/free", content=b"x" * 100).status_code == 200
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/limited", read_body, methods=["POST"], max_body_size=5),
            Route("/free", read_body, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/limited", content=b"x" * 6).status_code == 413
    assert client.post("/limited", content=b"x" * 5).status_code == 200
    assert client.post("/free", content=b"x" * 600).status_code == 200


def test_inner_limits_raise_and_lower_outer(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/outer", read_body, methods=["POST"]),
            Route("/raised", read_body, methods=["POST"], max_body_size=100),
            Route("/lowered", read_body, methods=["POST"], max_body_size=3),
            Mount("/m", routes=[Route("/raised", read_body, methods=["POST"], max_body_size=100)], max_body_size=20),
            Mount("/r", app=Router(routes=[Route("/", read_body, methods=["POST"])], max_body_size=50)),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    assert client.post("/outer", content=b"x" * 11).status_code == 413
    # The declared length is above the app limit, but the route raises it.
    assert client.post("/raised", content=b"x" * 100).status_code == 200
    assert client.post("/raised", content=b"x" * 101).status_code == 413
    assert client.post("/lowered", content=b"x" * 3).status_code == 200
    assert client.post("/lowered", content=b"x" * 4).status_code == 413
    assert client.post("/m/raised", content=b"x" * 100).status_code == 200
    assert client.post("/m/raised", content=b"x" * 101).status_code == 413
    assert client.post("/r/", content=b"x" * 50).status_code == 200
    assert client.post("/r/", content=b"x" * 51).status_code == 413


def test_standalone_middleware() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        size = 0
        while True:
            message = await receive()
            size += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await PlainTextResponse(str(size))(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(asgi, max_body_size=4))
    assert client.post("/", content=b"1234").text == "4"
    response = client.post("/", content=b"12345")
    assert (response.status_code, response.text) == (413, "Content Too Large")


def test_middleware_passes_through_non_http_and_unlimited() -> None:
    seen: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def main() -> None:
        await RequestBodyLimitMiddleware(asgi, max_body_size=1)({"type": "websocket"}, None, None)  # type: ignore
        await RequestBodyLimitMiddleware(asgi)({"type": "http"}, None, None)  # type: ignore

    anyio.run(main)
    assert seen == ["websocket", "http"]


def test_negative_limit_rejected() -> None:
    with pytest.raises(AssertionError):
        RequestBodyLimitMiddleware(Router(), max_body_size=-1)


async def run_asgi(app: ASGIApp, headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> tuple[list[Message], int]:
    sent: list[Message] = []
    receives = 0
    queue = list(chunks)

    async def receive() -> Message:
        nonlocal receives
        receives += 1
        body = queue.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers, "query_string": b""}
    await app(scope, receive, send)
    return sent, receives


def test_content_length_fail_fast() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent, receives = anyio.run(run_asgi, app, [(b"content-length", b"6")], [b"abc", b"def"])
    assert sent[0]["status"] == 413
    assert receives == 0


def test_understated_and_missing_content_length() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body"):
            pass
        await Response("ok")(scope, receive, send)

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent, _ = anyio.run(run_asgi, app, [(b"content-length", b"1")], [b"abc", b"def"])
    assert sent[0]["status"] == 413
    sent, _ = anyio.run(run_asgi, app, [], [b"abc", b"def"])
    assert sent[0]["status"] == 413
    sent, _ = anyio.run(run_asgi, app, [(b"content-length", b"junk")], [b"abc"])
    assert sent[0]["status"] == 200


def test_response_already_started() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive()).get("more_body"):
            pass  # pragma: no cover
        await send({"type": "http.response.body", "body": b"done"})  # pragma: no cover

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent, receives = anyio.run(run_asgi, app, [], [b"abc", b"def", b"ghi"])
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert receives == 2


def test_stream_stays_stopped() -> None:
    results: list[str] = []

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
            await receive()
        except Exception:
            results.append("first")
        await receive()  # raises again

    app = RequestBodyLimitMiddleware(asgi, max_body_size=5)
    sent, receives = anyio.run(run_asgi, app, [], [b"abc", b"def", b"ghi"])
    assert results == ["first"]
    assert sent[0]["status"] == 413
    assert receives == 2


def test_multipart_counts_overhead(test_client_factory: TestClientFactory) -> None:
    async def endpoint(request: Request) -> Response:
        async with request.form() as form:
            return PlainTextResponse(str(len(await form["f"].read())))  # type: ignore[union-attr]

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=300)
    client = test_client_factory(app)
    assert client.post("/", files={"f": ("a.txt", b"x" * 100)}).text == "100"
    # The file data alone is under the limit, the multipart envelope pushes it over.
    response = client.post("/", files={"f": ("a.txt", b"x" * 290)})
    assert response.status_code == 413


@pytest.mark.anyio
async def test_multipart_temp_files_closed_on_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette import formparsers

    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        f = original(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    body = b'--b\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n' + b"x" * 20

    class Interrupted(BaseException):
        pass

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body
        raise Interrupted()

    headers = Headers({"content-type": "multipart/form-data; boundary=b"})
    with pytest.raises(Interrupted):
        await formparsers.MultiPartParser(headers, stream()).parse()
    assert len(created) == 1
    assert created[0].closed


def test_multipart_temp_files_closed_on_body_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette import formparsers

    created: list[Any] = []
    original = formparsers.SpooledTemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        f = original(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking)

    async def endpoint(request: Request) -> Response:
        await request.form()
        return PlainTextResponse("ok")  # pragma: no cover

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])], max_body_size=200)
    # No Content-Length is sent, so the limit is hit while the body is being parsed.
    body = b'--b
Content-Disposition: form-data; name="f"; filename="a.txt"

' + b"x" * 500
    headers = [(b"content-type", b"multipart/form-data; boundary=b")]
    sent, _ = anyio.run(run_asgi, app, headers, [body[:100], body[100:300], body[300:]])
    assert sent[0]["status"] == 413
    assert len(created) == 1
    assert created[0].closed


def test_limit_applies_to_body_read_by_user_middleware(test_client_factory: TestClientFactory) -> None:
    async def dispatch(request: Request, call_next: RequestResponseEndpoint) -> Response:
        return await call_next(request)

    app = Starlette(
        routes=[Route("/", read_body, methods=["POST"])],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=dispatch)],
        max_body_size=4,
    )
    assert test_client_factory(app).post("/", content=b"12345").status_code == 413
