from __future__ import annotations

import anyio
import pytest

from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send


async def read_all(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    more_body = True
    while more_body:
        message = await receive()
        body += message.get("body", b"")
        more_body = message.get("more_body", False)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(len(body)).encode()})


def drive(
    app: object,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
) -> tuple[list[Message], list[int]]:
    """Run an ASGI app with the body split in `chunks`. Returns sent messages and receive call count."""
    sent: list[Message] = []
    calls: list[int] = []
    pending = list(chunks)

    async def receive() -> Message:
        calls.append(1)
        if not pending:
            return {"type": "http.disconnect"}
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": scope_type, "method": "POST", "path": "/", "headers": headers or []}

    async def main() -> None:
        await app(scope, receive, send)  # type: ignore[operator]

    anyio.run(main)
    return sent, calls


def status_and_body(sent: list[Message]) -> tuple[int, bytes, list[tuple[bytes, bytes]]]:
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:]), sent[0]["headers"]


def test_unlimited_plain_app_is_untouched() -> None:
    sent, _ = drive(read_all, [b"a" * 100, b"b" * 100])
    assert status_and_body(sent)[:2] == (200, b"200")


def test_under_and_at_limit_passes() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    sent, _ = drive(app, [b"12345", b"12345"])
    assert status_and_body(sent)[:2] == (200, b"10")


def test_over_limit_returns_413() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    sent, _ = drive(app, [b"12345", b"123456"])
    status, body, headers = status_and_body(sent)
    assert status == 413
    assert body == b"Content Too Large"
    assert (b"content-type", b"text/plain; charset=utf-8") in headers


def test_understated_content_length_does_not_let_body_through() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    sent, _ = drive(app, [b"x" * 50], headers=[(b"content-length", b"3")])
    assert status_and_body(sent)[0] == 413


def test_invalid_content_length_is_ignored() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    sent, _ = drive(app, [b"x" * 5], headers=[(b"content-length", b"abc")])
    assert status_and_body(sent)[:2] == (200, b"5")


def test_declared_content_length_fails_fast_without_receive() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    sent, calls = drive(app, [b"x" * 5], headers=[(b"content-length", b"11")])
    assert status_and_body(sent)[:2] == (413, b"Content Too Large")
    assert calls == []


def test_declared_content_length_checked_lazily_when_eager_check_disabled() -> None:
    app = RequestBodyLimitMiddleware(read_all, max_body_size=10, check_content_length=False)
    sent, calls = drive(app, [b"x" * 5], headers=[(b"content-length", b"11")])
    assert status_and_body(sent)[0] == 413
    # The underlying receive is never reached.
    assert calls == []


def test_non_http_scope_passes_through() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    drive(RequestBodyLimitMiddleware(app, max_body_size=1), [], scope_type="lifespan")
    assert seen == ["lifespan"]


def test_non_request_messages_are_not_counted() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": message["type"].encode()})

    sent, _ = drive(RequestBodyLimitMiddleware(app, max_body_size=1), [])
    assert status_and_body(sent)[1] == b"http.disconnect"


def test_response_already_started_sends_no_second_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            if not message.get("more_body"):
                break

    sent, _ = drive(RequestBodyLimitMiddleware(app, max_body_size=4), [b"123", b"456", b"789"])
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert sent[0]["status"] == 200


def test_body_stream_stops_after_limit_exceeded() -> None:
    results: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        with pytest.raises(Exception) as first:
            await receive()
            await receive()
        results.append(type(first.value).__name__)
        with pytest.raises(Exception) as second:
            await receive()
        results.append(type(second.value).__name__)
        raise second.value

    sent, calls = drive(RequestBodyLimitMiddleware(app, max_body_size=4), [b"123", b"456", b"789"])
    assert status_and_body(sent)[0] == 413
    assert results == ["_BodyLimitExceeded", "_BodyLimitExceeded"]
    assert len(calls) == 2


def test_inner_limit_raises_outer_limit() -> None:
    inner = RequestBodyLimitMiddleware(read_all, max_body_size=100)
    outer = RequestBodyLimitMiddleware(inner, max_body_size=10)
    sent, _ = drive(outer, [b"x" * 50])
    assert status_and_body(sent)[:2] == (200, b"50")


def test_inner_limit_lowers_outer_limit() -> None:
    inner = RequestBodyLimitMiddleware(read_all, max_body_size=10)
    outer = RequestBodyLimitMiddleware(inner, max_body_size=100)
    sent, _ = drive(outer, [b"x" * 50])
    assert status_and_body(sent)[0] == 413


def test_request_body_via_request_object() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        body = await request.body()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(len(body)).encode()})

    sent, _ = drive(RequestBodyLimitMiddleware(app, max_body_size=5), [b"abc", b"def"])
    assert status_and_body(sent)[0] == 413
