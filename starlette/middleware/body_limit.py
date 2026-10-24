from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised by `receive()` when the request body exceeds the configured limit."""


class _BodyLimitState:
    """Per-request state, shared by every body limit applied to the same request."""

    def __init__(self, limit: int, scope: Scope, receive: Receive, send: Send) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.started = False  # A response has been started.
        self.suppressed = False  # We sent the 413 response, drop everything else.
        self._scope = scope
        self._receive = receive
        self._send = send

    async def receive(self) -> Message:
        if self.exceeded:
            if self.started:
                # Stop the body stream without producing an error nobody can report.
                return {"type": "http.disconnect"}
            raise RequestBodyTooLarge()
        message = await self._receive()
        if message["type"] == "http.request":  # pragma: no branch
            self.received += len(message.get("body", b""))
            if self.received > self.limit:
                self.exceeded = True
                return await self.receive()
        return message

    async def send(self, message: Message) -> None:
        if self.suppressed:
            return
        if message["type"] == "http.response.start":
            if self.exceeded and not self.started:
                await self.respond()
                return
            self.started = True
        await self._send(message)

    async def respond(self) -> None:
        if self.started:
            return
        self.started = True
        self.suppressed = True
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(self._scope, self._receive, self._send)

    async def reject_if_declared_too_large(self) -> bool:
        """Send a 413 and return `True` if `Content-Length` is over the current limit."""
        try:
            declared = int(Headers(scope=self._scope).get("content-length", ""))
        except ValueError:
            return False
        if declared <= self.limit:
            return False
        self.exceeded = True
        await self.respond()
        return True


async def reject_oversized_content_length(scope: Scope, send: Send) -> bool:
    """
    The point where the limit that applies to a request is settled.

    Returns `True` (after sending a 413) if the declared `Content-Length` is over the limit.
    """
    state: _BodyLimitState | None = scope.get(STATE_KEY)
    if scope["type"] != "http" or state is None:
        return False
    return await state.reject_if_declared_too_large()


class RequestBodyLimitMiddleware:
    """
    Responds with a 413 to requests whose body is larger than `max_body_size` bytes.

    The limit counts the bytes received from the server. `Content-Length` is only used to
    reject early, before the application is called.
    """

    # Starlette's own routing settles the limit later, once routing has decided.
    _defer_content_length_check = False

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        assert max_body_size >= 0, "'max_body_size' must not be negative."
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(STATE_KEY)
        if state is not None:
            # Already limited further out: override the limit for this part of the tree.
            state.limit = self.max_body_size
            if not self._defer_content_length_check and await state.reject_if_declared_too_large():
                return
            await self.app(scope, receive, send)
            return

        state = _BodyLimitState(self.max_body_size, scope, receive, send)
        scope[STATE_KEY] = state
        if not self._defer_content_length_check and await state.reject_if_declared_too_large():
            return

        try:
            await self.app(scope, state.receive, state.send)
        except Exception:
            if not state.exceeded:
                raise
        if state.exceeded:
            await state.respond()


class _RoutingBodyLimitMiddleware(RequestBodyLimitMiddleware):
    _defer_content_length_check = True


def body_limited(app: ASGIApp, max_body_size: int | None, *, defer_content_length_check: bool) -> Any:
    """Wrap `app` with a limit, if there is one. Used by the routing classes."""
    if max_body_size is None:
        return None
    cls = _RoutingBodyLimitMiddleware if defer_content_length_check else RequestBodyLimitMiddleware
    return cls(app, max_body_size)
