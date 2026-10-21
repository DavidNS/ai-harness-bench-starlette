from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class _RequestBodyTooLarge(Exception):
    """Raised inside the application to stop it once the body limit is crossed."""


def _declared_length(headers: Headers) -> int | None:
    value = headers.get("content-length")
    # `str.isdigit()` alone also accepts non-ASCII digits, and `int()` accepts
    # underscores and whitespace, so be strict about what counts as a length.
    if value is None or not (value.isascii() and value.isdigit()):
        return None
    return int(value)


def _contains(exc: BaseException) -> bool:
    if isinstance(exc, _RequestBodyTooLarge):
        return True
    children = getattr(exc, "exceptions", None)
    return bool(children) and all(_contains(child) for child in children)


class _BodyLimitState:
    """Per-request state, shared with inner limits through the ASGI scope."""

    def __init__(self, scope: Scope, receive: Receive, send: Send, max_body_size: int) -> None:
        self.scope = scope
        self.receive = receive
        self.send = send
        self.max_body_size = max_body_size
        self.declared = _declared_length(Headers(scope=scope))
        self.received = 0
        self.response_started = False
        self.exceeded = False
        self.rejected = False  # Whether the 413 response was sent by us.

    def _is_exceeded(self) -> bool:
        if self.exceeded:
            return True
        if self.declared is not None and self.declared > self.max_body_size:
            return True
        return self.received > self.max_body_size

    async def _reject(self) -> None:
        if not self.exceeded:
            self.exceeded = True
            if not self.response_started:
                self.rejected = True
                self.response_started = True
                await PlainTextResponse("Content Too Large", status_code=413)(self.scope, self.receive, self.send)
        raise _RequestBodyTooLarge()

    async def wrapped_receive(self) -> Message:
        # `Content-Length` is only a fail-fast check, evaluated against the limit
        # in effect now, which an inner route or mount may have changed.
        if self._is_exceeded():
            await self._reject()
        message = await self.receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self._is_exceeded():
                await self._reject()
        return message

    async def wrapped_send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start":
            # An application that never reads the body is still rejected.
            if self._is_exceeded():
                await self._reject()
            self.response_started = True
        await self.send(message)


class RequestBodyLimitMiddleware:
    """
    Limits the total size of the request body, responding with `413 Content Too Large`
    when it is exceeded.

    The size is the number of body bytes received from the ASGI server.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
        if state is not None:
            # An outer limit is already enforcing: this one overrides it.
            state.max_body_size = self.max_body_size
            await self.app(scope, receive, send)
            return

        state = _BodyLimitState(scope, receive, send, self.max_body_size)
        scope[_SCOPE_KEY] = state
        try:
            await self.app(scope, state.wrapped_receive, state.wrapped_send)
        except Exception as exc:
            if state.exceeded and _contains(exc):
                return
            raise


def with_body_limit(app: ASGIApp, max_body_size: int | None) -> ASGIApp:
    if max_body_size is None:
        return app
    return RequestBodyLimitMiddleware(app, max_body_size=max_body_size)


__all__ = ["RequestBodyLimitMiddleware"]
