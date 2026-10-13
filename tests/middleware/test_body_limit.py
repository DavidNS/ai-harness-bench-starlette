from __future__ import annotations

from collections.abc import Iterator

import pytest

from starlette.exceptions import HTTPException
from starlette.middleware.body_limit import MAX_BODY_SIZE_SCOPE_KEY, RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import Message, Receive, Scope, Send
from tests.types import TestClientFactory


async def read_all(scope: Scope, receive: Receive, send: Send) -> None:
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await PlainTextResponse(str(total))(scope, receive, send)


async def never_reads(scope: Scope, receive: Receive, send: Send) -> None:
    never_reads.called = True  # type: ignore[attr-defined]
    await PlainTextResponse("ok")(scope, receive, send)


async def run(
    app: object,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_extra: dict[str, object] | None = None,
) -> tuple[list[Message], Scope]:
    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": headers or []}
    scope.update(scope_extra or {})
    pending: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)  # type: ignore[operator]
    return sent, scope


def status_of(sent: list[Message]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def body_of(sent: list[Message]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_limit_enforced_without_content_length(test_client_factory: TestClientFactory) -> None:
    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))

    def gen(n: int) -> Iterator[bytes]:
        yield b"x" * n

    response = client.post("/", content=gen(11))
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert response.headers["content-type"].startswith("text/plain")

    response = client.post("/", content=gen(5))
    assert response.status_code == 200
    assert response.text == "5"


def test_content_length_failfast(test_client_factory: TestClientFactory) -> None:
    never_reads.called = False  # type: ignore[attr-defined]
    client = test_client_factory(RequestBodyLimitMiddleware(never_reads, max_body_size=10))

    response = client.post("/", content=b"x" * 20)
    assert response.status_code == 413
    assert response.text == "Content Too Large"
    assert never_reads.called is False  # type: ignore[attr-defined]

    response = client.post("/", content=b"x" * 5)
    assert response.status_code == 200
    assert never_reads.called is True  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_understated_content_length_and_exact_limit() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)

    sent, _ = await run(app, [b"x" * 20], headers=[(b"content-length", b"5")])
    assert status_of(sent) == 413
    assert body_of(sent) == b"Content Too Large"

    sent, _ = await run(app, [b"x" * 10], headers=[(b"content-length", b"10")])
    assert status_of(sent) == 200
    assert body_of(sent) == b"10"

    # An unparsable Content-Length is ignored by the fail-fast check, the body is still counted.
    sent, _ = await run(app, [b"x" * 11], headers=[(b"content-length", b"abc")])
    assert status_of(sent) == 413


def test_multipart_and_streaming_counted(test_client_factory: TestClientFactory) -> None:
    async def form_app(scope: Scope, receive: Receive, send: Send) -> None:
        async with Request(scope, receive).form() as form:
            size = len(await form["file"].read())  # type: ignore[union-attr]
        await PlainTextResponse(str(size))(scope, receive, send)

    # The file content alone is exactly at the limit; multipart overhead pushes the request over it.
    client = test_client_factory(RequestBodyLimitMiddleware(form_app, max_body_size=100))
    response = client.post("/", files={"file": ("a.txt", b"x" * 100)})
    assert response.status_code == 413
    assert response.text == "Content Too Large"

    client = test_client_factory(RequestBodyLimitMiddleware(form_app, max_body_size=1000))
    response = client.post("/", files={"file": ("a.txt", b"x" * 100)})
    assert response.status_code == 200
    assert response.text == "100"

    def chunks(n: int) -> Iterator[bytes]:
        for _ in range(n):
            yield b"abcd"

    client = test_client_factory(RequestBodyLimitMiddleware(read_all, max_body_size=10))
    assert client.post("/", content=chunks(2)).status_code == 200
    assert client.post("/", content=chunks(3)).status_code == 413


@pytest.mark.anyio
async def test_no_double_response() -> None:
    async def respond_then_read(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await send({"type": "http.response.body", "body": b"unreachable"})

    app = RequestBodyLimitMiddleware(respond_then_read, max_body_size=3)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"abcd", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    with pytest.raises(HTTPException) as exc_info:
        await app({"type": "http", "headers": []}, receive, send)
    assert exc_info.value.status_code == 413
    assert [m["type"] for m in sent] == ["http.response.start"]

    # Before the response starts, the 413 is produced by the middleware.
    sent, _ = await run(RequestBodyLimitMiddleware(read_all, max_body_size=3), [b"abcd"])
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert status_of(sent) == 413


@pytest.mark.anyio
async def test_other_http_exceptions_and_non_http_scopes_pass_through() -> None:
    async def not_found(scope: Scope, receive: Receive, send: Send) -> None:
        raise HTTPException(status_code=404)

    with pytest.raises(HTTPException) as exc_info:
        await run(RequestBodyLimitMiddleware(not_found, max_body_size=3), [b"a"])
    assert exc_info.value.status_code == 404

    seen: list[Scope] = []

    async def ws_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(dict(scope))

    middleware = RequestBodyLimitMiddleware(ws_app, max_body_size=3)

    async def receive() -> Message:  # pragma: no cover
        raise AssertionError

    async def send(message: Message) -> None:  # pragma: no cover
        raise AssertionError

    await middleware({"type": "websocket"}, receive, send)
    assert seen == [{"type": "websocket"}]


@pytest.mark.anyio
async def test_scope_key() -> None:
    assert MAX_BODY_SIZE_SCOPE_KEY == "starlette.max_body_size"
    observed: list[object] = []

    async def spy(scope: Scope, receive: Receive, send: Send) -> None:
        observed.append(scope.get(MAX_BODY_SIZE_SCOPE_KEY))
        await PlainTextResponse("ok")(scope, receive, send)

    app = RequestBodyLimitMiddleware(spy, max_body_size=7)

    _, scope = await run(app, [b""])
    assert observed == [7]
    assert MAX_BODY_SIZE_SCOPE_KEY not in scope

    _, scope = await run(app, [b""], scope_extra={MAX_BODY_SIZE_SCOPE_KEY: 99})
    assert observed == [7, 7]
    assert scope[MAX_BODY_SIZE_SCOPE_KEY] == 99


@pytest.mark.anyio
async def test_nested_limits_raise_and_lower_the_outer_limit() -> None:
    inner_calls: list[object] = []

    async def spy_read_all(scope: Scope, receive: Receive, send: Send) -> None:
        inner_calls.append(scope[MAX_BODY_SIZE_SCOPE_KEY])
        await read_all(scope, receive, send)

    # Inner limit raises the outer one.
    raised = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(spy_read_all, max_body_size=100), max_body_size=5)
    sent, scope = await run(raised, [b"x" * 50])
    assert status_of(sent) == 200
    assert body_of(sent) == b"50"
    assert MAX_BODY_SIZE_SCOPE_KEY not in scope

    # Inner limit lowers the outer one.
    lowered = RequestBodyLimitMiddleware(RequestBodyLimitMiddleware(spy_read_all, max_body_size=5), max_body_size=100)
    sent, _ = await run(lowered, [b"x" * 50])
    assert status_of(sent) == 413

    # Inner Content-Length fail-fast does not call the app.
    inner_calls.clear()
    sent, _ = await run(lowered, [b"x" * 50], headers=[(b"content-length", b"50")])
    assert status_of(sent) == 413
    assert inner_calls == []
