from __future__ import annotations

from collections.abc import Iterator
from tempfile import SpooledTemporaryFile
from unittest import mock

import pytest

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, Router
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def size(request: Request) -> PlainTextResponse:
    return PlainTextResponse(str(len(await request.body())))


def chunked(total: int) -> Iterator[bytes]:
    yield b"x" * total


def test_default_is_unlimited(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])])
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 100_000).text == "100000"


def test_application_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    response = client.post("/", content=chunked(11))
    assert response.status_code == 413


def test_application_limit_applies_before_routing(test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/missing", content=b"x" * 11).status_code == 413
    assert client.post("/missing", content=b"x").status_code == 404


def test_router_limit(test_client_factory: TestClientFactory) -> None:
    app = Router(routes=[Route("/", size, methods=["POST"])], max_body_size=10)
    client = test_client_factory(app)
    assert client.post("/", content=b"x" * 10).text == "10"
    response = client.post("/", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


def test_mount_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Mount("/limited", routes=[Route("/", size, methods=["POST"])], max_body_size=10),
            Route("/free", size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/limited/", content=b"x" * 10).text == "10"
    assert client.post("/limited/", content=chunked(11)).status_code == 413
    assert client.post("/free", content=b"x" * 100).text == "100"


def test_route_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/limited", size, methods=["POST"], max_body_size=10),
            Route("/free", size, methods=["POST"]),
        ]
    )
    client = test_client_factory(app)
    assert client.post("/limited", content=b"x" * 10).text == "10"
    response = client.post("/limited", content=b"x" * 11)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert client.post("/free", content=b"x" * 100).text == "100"


def test_inner_limits_raise_outer_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/route", size, methods=["POST"], max_body_size=100),
            Mount("/mount", routes=[Route("/", size, methods=["POST"])], max_body_size=100),
            Mount(
                "/router",
                app=Router(routes=[Route("/", size, methods=["POST"])], max_body_size=100),
            ),
            Route("/plain", size, methods=["POST"]),
        ],
        max_body_size=10,
    )
    client = test_client_factory(app)
    for path in ("/route", "/mount/", "/router/"):
        assert client.post(path, content=b"x" * 50).text == "50"
        assert client.post(path, content=chunked(50)).text == "50"
        assert client.post(path, content=b"x" * 101).status_code == 413
    assert client.post("/plain", content=b"x" * 50).status_code == 413


def test_inner_limits_lower_outer_limit(test_client_factory: TestClientFactory) -> None:
    app = Starlette(
        routes=[
            Route("/route", size, methods=["POST"], max_body_size=10),
            Mount("/mount", routes=[Route("/", size, methods=["POST"])], max_body_size=10),
            Mount(
                "/router",
                app=Router(routes=[Route("/", size, methods=["POST"])], max_body_size=10),
            ),
            Route("/plain", size, methods=["POST"]),
        ],
        max_body_size=100,
    )
    client = test_client_factory(app)
    for path in ("/route", "/mount/", "/router/"):
        assert client.post(path, content=b"x" * 10).text == "10"
        assert client.post(path, content=b"x" * 11).status_code == 413
        assert client.post(path, content=chunked(11)).status_code == 413
    assert client.post("/plain", content=b"x" * 50).text == "50"


@pytest.mark.anyio
async def test_response_started_before_limit_is_not_followed_by_second_response() -> None:
    async def streaming(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"started", "more_body": True})
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    app = Starlette(routes=[Mount("/", app=streaming)], max_body_size=5)
    incoming: list[Message] = [{"type": "http.request", "body": b"x" * 10, "more_body": False}]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "http", "method": "POST", "path": "/", "headers": []}, receive, send)
    assert [(m["type"], m.get("status"), m.get("body")) for m in sent] == [
        ("http.response.start", 200, None),
        ("http.response.body", None, b"started"),
    ]


def multipart_body(boundary: str, file_size: int) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\n" + "x" * file_size + f"\r\n--{boundary}--\r\n"
    ).encode()


async def upload(request: Request) -> PlainTextResponse:
    async with request.form() as form:
        return PlainTextResponse(str(len(await form["file"].read())))  # type: ignore[union-attr]


def test_multipart_overhead_counts_toward_limit(test_client_factory: TestClientFactory) -> None:
    boundary = "a7f7ac8d4e2e437c877bb7b8d7cc549c"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    body = multipart_body(boundary, 10)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body))
    client = test_client_factory(app)
    assert client.post("/", content=body, headers=headers).text == "10"

    # The file data alone fits in the limit, the multipart framing does not.
    smaller = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=len(body) - 1)
    client = test_client_factory(smaller)
    response = client.post("/", content=body, headers=headers)
    assert response.status_code == 413
    assert response.text == "Content Too Large"


@pytest.mark.anyio
async def test_multipart_temp_files_closed_when_limit_is_crossed() -> None:
    closed: list[SpooledTemporaryFile[bytes]] = []

    class TrackingSpooledTemporaryFile(SpooledTemporaryFile[bytes]):
        def close(self) -> None:
            closed.append(self)
            super().close()

    boundary = "a7f7ac8d4e2e437c877bb7b8d7cc549c"
    body = multipart_body(boundary, 5000)
    app = Starlette(routes=[Route("/", upload, methods=["POST"])], max_body_size=1000)
    incoming: list[Message] = [
        {"type": "http.request", "body": body[i : i + 500], "more_body": i + 500 < len(body)}
        for i in range(0, len(body), 500)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }
    with mock.patch("starlette.formparsers.SpooledTemporaryFile", TrackingSpooledTemporaryFile):
        await app(scope, receive, send)

    assert sent[0]["status"] == 413
    assert len(closed) == 1
