from __future__ import annotations

import tempfile
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pytest

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

CALLS: list[str] = []


async def echo_len(request: Request) -> Response:
    CALLS.append(request.url.path)
    body = await request.body()
    return PlainTextResponse(str(len(body)))


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    CALLS.clear()


def chunks(*sizes: int) -> Iterator[bytes]:
    return (b"x" * size for size in sizes)


def test_default_unlimited() -> None:
    client = TestClient(Starlette(routes=[Route("/", echo_len, methods=["POST"])]))
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_app_limit_streaming_no_content_length() -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"])], max_body_size=10)
    client = TestClient(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=chunks(6, 6))
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")


def test_app_limit_content_length_failfast() -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"])], max_body_size=10)
    response = TestClient(app).post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert CALLS == []


def test_understated_content_length() -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"])], max_body_size=10)
    response = TestClient(app).post("/", content=b"x" * 50, headers={"content-length": "3"})
    assert response.status_code == 413


def test_router_limit() -> None:
    router = Router(routes=[Route("/", echo_len, methods=["POST"])], max_body_size=5)
    client = TestClient(router)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert client.post("/", content=chunks(3, 3)).status_code == 413
    assert CALLS == ["/", "/"]  # the 6-byte Content-Length request never ran


def test_route_limit() -> None:
    app = Starlette(routes=[Route("/", echo_len, methods=["POST"], max_body_size=5)])
    client = TestClient(app)
    assert client.post("/", content=b"x" * 5).status_code == 200
    assert client.post("/", content=b"x" * 6).status_code == 413
    assert client.post("/", content=chunks(3, 3)).status_code == 413


def test_mount_limit() -> None:
    app = Starlette(routes=[Mount("/m", routes=[Route("/", echo_len, methods=["POST"])], max_body_size=5)])
    client = TestClient(app)
    assert client.post("/m/", content=b"x" * 5).status_code == 200
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert client.post("/m/", content=chunks(3, 3)).status_code == 413


def test_mount_asgi_leaf_limit() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        CALLS.append("asgi")
        await PlainTextResponse(str(len(await Request(scope, receive).body())))(scope, receive, send)

    client = TestClient(Starlette(routes=[Mount("/m", asgi, max_body_size=5)]))
    assert client.post("/m/", content=b"x" * 6).status_code == 413
    assert CALLS == []
    assert client.post("/m/", content=chunks(3, 3)).status_code == 413


def test_inner_route_raises_outer_limit() -> None:
    app = Starlette(
        routes=[
            Route("/big", echo_len, methods=["POST"], max_body_size=100),
            Route("/small", echo_len, methods=["POST"]),
            Mount("/m", routes=[Route("/big", echo_len, methods=["POST"], max_body_size=100)], max_body_size=5),
        ],
        max_body_size=10,
    )
    client = TestClient(app)
    assert client.post("/big", content=b"x" * 50).text == "50"
    assert client.post("/big", content=chunks(30, 30)).text == "60"
    assert client.post("/big", content=b"x" * 101).status_code == 413
    assert client.post("/small", content=b"x" * 11).status_code == 413
    assert client.post("/m/big", content=b"x" * 50).text == "50"
    assert client.post("/m/big", content=chunks(30, 30)).text == "60"


def test_inner_route_lowers_outer_limit() -> None:
    app = Starlette(
        routes=[
            Route("/low", echo_len, methods=["POST"], max_body_size=5),
            Mount("/m", routes=[Route("/low", echo_len, methods=["POST"], max_body_size=5)], max_body_size=100),
        ],
        max_body_size=100,
    )
    client = TestClient(app)
    assert client.post("/low", content=b"x" * 6).status_code == 413
    assert CALLS == []
    assert client.post("/low", content=chunks(3, 3)).status_code == 413
    assert client.post("/m/low", content=b"x" * 6).status_code == 413
    assert CALLS == ["/low"]  # only the streamed request ran, and was cut off


def test_failfast_no_route_and_wrong_method_use_app_limit() -> None:
    app = Starlette(
        routes=[Route("/r", echo_len, methods=["POST"], max_body_size=1000)],
        max_body_size=10,
    )
    client = TestClient(app)
    assert client.post("/nothing", content=b"x" * 11).status_code == 413
    assert client.post("/nothing", content=b"x" * 5).status_code == 404
    assert client.put("/r", content=b"x" * 11).status_code == 413
    assert client.put("/r", content=b"x" * 5).status_code == 405


def test_failfast_waits_for_routing() -> None:
    app = Starlette(
        routes=[Mount("/m", routes=[Route("/", echo_len, methods=["POST"], max_body_size=100)], max_body_size=5)],
        max_body_size=5,
    )
    assert TestClient(app).post("/m/", content=b"x" * 50).text == "50"


def test_no_body_and_websocket_unaffected() -> None:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket

    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    app = Starlette(routes=[Route("/", echo_len, methods=["GET", "POST"]), WebSocketRoute("/ws", ws)], max_body_size=1)
    with TestClient(app) as client:
        assert client.get("/").text == "0"
        with client.websocket_connect("/ws") as session:
            session.send_text("hello world")
            assert session.receive_text() == "hello world"


def test_standalone_middleware() -> None:
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        CALLS.append("called")
        await PlainTextResponse(str(len(await Request(scope, receive).body())))(scope, receive, send)

    client = TestClient(RequestBodyLimitMiddleware(asgi, max_body_size=10))
    assert client.post("/", content=b"x" * 10).text == "10"
    assert client.post("/", content=chunks(6, 6)).status_code == 413
    assert CALLS == ["called", "called"]  # the streamed request ran, and was cut off
    response = client.post("/", content=b"x" * 11)
    assert (response.status_code, response.text) == (413, "Content Too Large")
    assert CALLS == ["called", "called"]


@pytest.mark.anyio
async def test_response_already_started() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        message = await receive()
        message = await receive()
        seen.append(message["type"])
        await send({"type": "http.response.body", "body": b"ok"})

    seen: list[str] = []
    sent: list[Message] = []
    incoming = [
        {"type": "http.request", "body": b"x" * 6, "more_body": True},
        {"type": "http.request", "body": b"x" * 6, "more_body": True},
    ]

    async def receive() -> Message:
        return incoming.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    await RequestBodyLimitMiddleware(app, max_body_size=10)(scope, receive, send)
    assert seen == ["http.disconnect"]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]


def _multipart(file_size: int) -> tuple[bytes, dict[str, str]]:
    boundary = "bnd"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\n\r\n'.encode()
        + b"x" * file_size
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


def test_multipart_limit_counts_overhead() -> None:
    async def upload(request: Request) -> Response:
        async with request.form() as form:
            upload_file = form["f"]
            assert isinstance(upload_file, UploadFile)
            return PlainTextResponse(str(len(await upload_file.read())))

    body, headers = _multipart(50)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body))
    client = TestClient(app)
    assert client.post("/", content=body, headers=headers).text == "50"
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body) - 1)
    assert TestClient(app).post("/", content=body, headers=headers).status_code == 413
    assert TestClient(app).post("/", content=iter([body[:40], body[40:]]), headers=headers).status_code == 413


@pytest.mark.anyio
async def test_multipart_tempfiles_closed_on_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def factory(*args: Any, **kwargs: Any) -> Any:
        f = original(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", factory)
    body, headers = _multipart(50)

    async def stream() -> AsyncGenerator[bytes, None]:
        yield body[:120]
        raise RuntimeError("interrupted")

    from starlette.datastructures import Headers

    parser = MultiPartParser(Headers(headers), stream())
    with pytest.raises(RuntimeError):
        await parser.parse()
    assert created and all(f.closed for f in created)


def test_unmatched_inside_mount_uses_app_limit() -> None:
    async def default(scope: Scope, receive: Receive, send: Send) -> None:
        CALLS.append("default")
        await PlainTextResponse("default")(scope, receive, send)

    router = Router(routes=[Route("/x", echo_len, methods=["POST"])], default=default, max_body_size=100)
    client = TestClient(Starlette(routes=[Mount("/s", app=router)], max_body_size=5))
    assert client.post("/s/x", content=b"x" * 50).text == "50"
    CALLS.clear()
    assert client.post("/s/nothing", content=b"x" * 50).status_code == 413
    assert client.put("/s/x", content=b"x" * 50).status_code == 413
    assert CALLS == []
