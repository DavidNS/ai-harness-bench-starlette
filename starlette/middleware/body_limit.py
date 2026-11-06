from __future__ import annotations

import sys

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if sys.version_info < (3, 11):  # pragma: no cover
    from exceptiongroup import BaseExceptionGroup

SCOPE_KEY = "starlette.request_body_limit"


class RequestBodyTooLarge(Exception):
    """Raised by `receive()` when the request body is larger than the limit that applies."""


def _is_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return all(_is_limit_error(inner) for inner in exc.exceptions)
    return isinstance(exc, RequestBodyTooLarge)


def _declared_length(scope: Scope) -> int | None:
    values = Headers(scope=scope).getlist("content-length")
    if len(values) != 1:
        return None
    try:
        length = int(values[0])
    except ValueError:
        return None
    return length if length >= 0 else None


class _BodyLimit:
    """The state of the body limit of a single request, shared through the ASGI scope."""

    def __init__(self, scope: Scope, receive: Receive, send: Send, max_body_size: int | None) -> None:
        self._scope = scope
        self.max_body_size = max_body_size
        self.declared_length = _declared_length(scope)
        self.received = 0
        self.exceeded = False
        self.length_checked = False
        self.response_started = False
        self.response_complete = False
        self.rejected = False
        self._receive = receive
        self._send = send

    def set_limit(self, max_body_size: int | None) -> None:
        """Replace the limit, for the part of the application that is being entered."""
        self.max_body_size = max_body_size
        self.length_checked = False
        self._check_received()

    def _check_received(self) -> None:
        if self.exceeded or (self.max_body_size is not None and self.received > self.max_body_size):
            self.exceeded = True
            raise RequestBodyTooLarge()

    def check_declared_length(self) -> None:
        """Reject a `Content-Length` over the limit that applies, before the body is read."""
        self.length_checked = True
        if self.max_body_size is not None and self.declared_length is not None:
            if self.declared_length > self.max_body_size:
                self.exceeded = True
                raise RequestBodyTooLarge()

    async def receive(self) -> Message:
        self._check_received()
        if not self.length_checked and not self.response_started:
            self.check_declared_length()
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            self._check_received()
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if self.exceeded and not self.response_complete:
            if self.response_started:
                # Too late for a 413: cut the response off.
                self.rejected = True
                return
            await self.reject()
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        elif message["type"] == "http.response.body" and not message.get("more_body", False):
            self.response_complete = True
        await self._send(message)

    async def reject(self) -> None:
        """Send the 413 response, unless a response has already been started."""
        if self.rejected:
            return
        self.rejected = True
        if self.response_started:
            return
        self.response_started = True
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(self._scope, self._receive, self._send)


def check_content_length(scope: Scope) -> None:
    """
    Called once routing has decided which handler gets the request, so that the limit
    that applies is final: reject a `Content-Length` over it without running the handler.
    """
    state: _BodyLimit | None = scope.get(SCOPE_KEY)
    if state is not None and not state.response_started:
        state.check_declared_length()


async def limit_body(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
) -> None:
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimit | None = scope.get(SCOPE_KEY)
    if state is not None:
        # Nested inside another limit: the inner limit overrides the outer one.
        if max_body_size is not None:
            state.set_limit(max_body_size)
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    state = _BodyLimit(scope, receive, send, max_body_size)
    scope[SCOPE_KEY] = state
    try:
        await app(scope, state.receive, state.send)
    except BaseException as exc:
        if not (state.exceeded and _is_limit_error(exc)):
            raise
        await state.reject()
    else:
        if state.exceeded:
            await state.reject()


class RequestBodyLimitMiddleware:
    """
    Limit the size of the request body to `max_body_size` bytes.

    A request that goes over the limit gets a "413 Content Too Large" response.
    If the response has already started, it is cut off instead.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        assert max_body_size is None or max_body_size >= 0, "'max_body_size' must not be negative"
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await limit_body(self.app, scope, receive, send, self.max_body_size)
