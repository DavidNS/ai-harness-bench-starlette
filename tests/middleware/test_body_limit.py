from __future__ import annotations

from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.formparsers import MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

calls: list[str] = []


async def echo(request: Request) -> Response:
    calls.append(request.url.path)
    body = await request.body()
    return JSONResponse({"size": len(body)})


async def form(request: Request) -> Response:
    async with request.form() as data:
        return JSONResponse({"keys": list(data.keys())})


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    calls.clear()


def post(app: Any, path: str = "/", data: bytes = b"x" * 10, **kwargs: Any) -> Any:
    return TestClient(app).post(path, content=data, **kwargs)


def test_app_limit() -> None:
    app = Starlette(routes=[Route("/", echo, methods=["POST"])], max_body_size=10)
    assert post(app).json() == {"size": 10}
    response = post(app, data=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_default_unlimited() -> None:
    app = Starlette(routes=[Route("/", echo, methods=["POST"])])
    assert post(app, data=b"x" * 100000).status_code == 200


def test_router_and_mount_and_route_limits() -> None:
    app = Starlette(
        routes=[
            Mount("/router", app=Router([Route("/", echo, methods=["POST"])], max_body_size=5)),
            Mount("/mount", routes=[Route("/", echo, methods=["POST"])], max_body_size=5),
            Route("/route", echo, methods=["POST"], max_body_size=5),
        ]
    )
    for path in ("/router/", "/mount/", "/route"):
        assert post(app, path, b"x" * 5).status_code == 200
        assert post(app, path, b"x" * 6).status_code == 413


def test_inner_raises_and_lowers() -> None:
    app = Starlette(
        routes=[
            Route("/raise", echo, methods=["POST"], max_body_size=100),
            Route("/lower", echo, methods=["POST"], max_body_size=5),
            Route("/inherit", echo, methods=["POST"]),
            Mount("/m", routes=[Route("/", echo, methods=["POST"])], max_body_size=100),
            Mount(
                "/n",
                routes=[Route("/", echo, methods=["POST"], max_body_size=100)],
                max_body_size=3,
            ),
        ],
        max_body_size=10,
    )
    assert post(app, "/raise", b"x" * 50).status_code == 200
    assert post(app, "/raise", b"x" * 101).status_code == 413
    assert post(app, "/lower", b"x" * 6).status_code == 413
    assert post(app, "/inherit", b"x" * 11).status_code == 413
    assert post(app, "/m/", b"x" * 50).status_code == 200
    assert post(app, "/n/", b"x" * 50).status_code == 200


def test_content_length_fail_fast_waits_for_routing() -> None:
    app = Starlette(
        routes=[Route("/raise", echo, methods=["POST"], max_body_size=100), Route("/x", echo, methods=["POST"])],
        max_body_size=10,
    )
    assert post(app, "/x", b"x" * 11).status_code == 413
    assert calls == []
    assert post(app, "/raise", b"x" * 50).status_code == 200
    assert post(app, "/missing", b"x" * 11).status_code == 413  # no route: app limit
    assert post(app, "/x", b"x" * 11).status_code == 413
    assert post(app.router, "/x", b"x" * 11).status_code == 413
    # 405: matched route without its own limit
    assert TestClient(app).put("/x", content=b"x" * 11).status_code == 413


async def run_asgi(app: Any, headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> list[Message]:
    sent: list[Message] = []
    queue = list(chunks)

    async def receive() -> Message:
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers, "query_string": b""}
    await app(scope, receive, send)
    return sent


def test_middleware_rejects_on_entry() -> None:
    ran = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal ran
        ran = True

    mw = RequestBodyLimitMiddleware(app, max_body_size=5)
    sent = anyio.run(run_asgi, mw, [(b"content-length", b"6")], [b"x"])
    assert not ran
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Content Too Large"


def test_middleware_understated_and_absent_header() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await Request(scope, receive).body()
        await Response("ok")(scope, receive, send)

    mw = RequestBodyLimitMiddleware(app, max_body_size=5)
    for headers in ([], [(b"content-length", b"1")]):
        sent = anyio.run(run_asgi, mw, headers, [b"abc", b"def"])
        assert sent[0]["status"] == 413
    sent = anyio.run(run_asgi, mw, [], [b"abc", b"de"])
    assert sent[0]["status"] == 200


def test_middleware_response_already_started() -> None:
    errors: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await receive()
        except Exception as exc:
            errors.append(type(exc).__name__)
            raise

    mw = RequestBodyLimitMiddleware(app, max_body_size=5)
    sent = anyio.run(run_asgi, mw, [], [b"abc", b"def"])
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert errors == ["BodyTooLarge"]


def test_middleware_ignores_websocket_and_lifespan() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    mw = RequestBodyLimitMiddleware(app, max_body_size=0)

    async def main() -> None:
        for kind in ("websocket", "lifespan"):
            await mw({"type": kind}, None, None)  # type: ignore[arg-type]

    anyio.run(main)
    assert seen == ["websocket", "lifespan"]


def test_requests_without_body() -> None:
    app = Starlette(routes=[Route("/", echo)], max_body_size=0)
    assert TestClient(app).get("/").status_code == 200


def multipart_files(size: int) -> dict[str, Any]:
    return {"file": ("f.txt", b"a" * size, "text/plain")}


def test_multipart_limit_counts_overhead() -> None:
    app = Starlette(routes=[Route("/", form, methods=["POST"])], max_body_size=500)
    assert TestClient(app).post("/", files=multipart_files(10)).status_code == 200
    assert TestClient(app).post("/", files=multipart_files(500)).status_code == 413


def test_multipart_tempfiles_closed_when_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    original = MultiPartParser.on_headers_finished

    def spy(self: MultiPartParser) -> None:
        original(self)
        created.extend(self._files_to_close_on_error)

    monkeypatch.setattr(MultiPartParser, "on_headers_finished", spy)
    app = Starlette(routes=[Route("/", form, methods=["POST"])], max_body_size=1000)
    body = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="f.txt"\r\n\r\n' + b"a" * 1000 + b"\r\n--b--\r\n"
    )
    chunks = [body[i : i + 100] for i in range(0, len(body), 100)]
    headers = [(b"content-type", b"multipart/form-data; boundary=b")]
    sent = anyio.run(run_asgi, app, headers, chunks)
    assert sent[0]["status"] == 413
    assert created and all(f.closed for f in created)


def test_multipart_tempfiles_closed_when_body_is_truncated_or_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from tempfile import SpooledTemporaryFile

    import starlette.formparsers as formparsers

    created: list[Any] = []

    class Tracking(SpooledTemporaryFile):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Tracking)
    app = Starlette(routes=[Route("/", form, methods=["POST"])], max_body_size=200)
    headers = {"content-type": "multipart/form-data; boundary=b"}
    part = b'--b\r\nContent-Disposition: form-data; name="f"; filename="a"\r\n\r\n'

    # one message crossing the limit: the parser sees the allowed part only
    response = TestClient(app).post("/", content=iter([part + b"x" * 1000]), headers=headers)
    assert response.status_code == 413
    assert created and all(f.closed for f in created)

    created.clear()
    response = TestClient(app).post("/", content=part + b"x", headers=headers)  # truncated, no closing boundary
    assert response.status_code == 200
    assert created and all(f.closed for f in created)
