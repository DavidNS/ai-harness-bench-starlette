from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` once a 413 response has been sent for an oversized body."""


class _BodyLimitState:
    """Per-request state shared by every level that sets a `max_body_size`."""

    def __init__(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        self.scope = scope
        self.limit = limit
        self.received = 0
        self.response_started = False
        self.rejected = False
        self.disconnected = False
        self._receive = receive
        self._send = send

    async def receive(self) -> Message:
        if self.disconnected:
            return {"type": "http.disconnect"}
        if self.rejected:
            raise RequestBodyTooLarge()
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.received > self.limit:
                if self.response_started:
                    self.disconnected = True
                    return {"type": "http.disconnect"}
                await self.reject()
                raise RequestBodyTooLarge()
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    async def reject(self) -> None:
        self.rejected = True
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(self.scope, self._receive, self._send)


async def reject_if_declared_too_large(scope: Scope) -> bool:
    """Send a 413 if the declared Content-Length exceeds the current limit. Returns True if it did."""
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is None:
        return False
    try:
        declared = int(Headers(scope=scope).get("content-length", ""))
    except ValueError:
        return False
    if declared > state.limit:
        await state.reject()
        return True
    return False


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    check_content_length: bool = True,
) -> None:
    """
    Call `app` with the body limit applied. A `max_body_size` of `None` keeps the limit
    set by an outer level, if any. The level that starts tracking a request is the one
    that swallows the exception raised once the limit is crossed.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    owner = False
    if max_body_size is not None:
        if state is None:
            state = _BodyLimitState(scope, receive, send, max_body_size)
            scope[_SCOPE_KEY] = state
            receive, send = state.receive, state.send
            owner = True
        else:
            state.limit = max_body_size

    if check_content_length and await reject_if_declared_too_large(scope):
        return

    if not owner:
        await app(scope, receive, send)
        return

    try:
        await app(scope, receive, send)
    except Exception:
        # Concurrent readers (e.g. a streaming response listening for a disconnect) can
        # surface the failure wrapped in an exception group, so any error is dropped once
        # the 413 has been sent.
        if not state.rejected:
            raise


class RequestBodyLimitMiddleware:
    """Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size)
