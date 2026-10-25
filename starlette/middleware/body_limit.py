from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class _BodyTooLarge(Exception):
    """Raised inside the application when the request body crosses the limit."""


class _BodyLimiter:
    """Per-request state shared by the body limit layers of a Starlette application."""

    def __init__(self, max_body_size: int, receive: Receive) -> None:
        self.max_body_size = max_body_size
        self.received = 0
        self.exceeded = False
        self._receive = receive

    async def receive(self) -> Message:
        if self.exceeded:
            raise _BodyTooLarge()
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.received > self.max_body_size:
                self.exceeded = True
                raise _BodyTooLarge()
        return message

    def check_content_length(self, scope: Scope) -> None:
        """Fail fast when a declared `Content-Length` is over the limit that currently applies."""
        for name, raw in scope.get("headers", []):
            if bytes(name).lower() != b"content-length":
                continue
            value = bytes(raw).strip()
            if value.isascii() and value.isdigit() and int(value) > self.max_body_size:
                self.exceeded = True
                raise _BodyTooLarge()


def check_body_limit(scope: Scope) -> None:
    """
    Called by the routing code once it has settled which limit applies to the request.
    Raises if the declared `Content-Length` is over it. Does nothing when no limit is in play.
    """
    limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY)
    if limiter is not None:
        limiter.check_content_length(scope)


class RequestBodyLimitMiddleware:
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a
    `413 Content Too Large` response.

    The limit counts the body bytes actually received from the server.
    `Content-Length` is only used to reject early.
    """

    # Whether limiters are shared with the routing code, so that inner limits override outer ones.
    _scoped = False
    # Whether the `Content-Length` check is left to the routing code.
    _deferred = False

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        assert max_body_size >= 0, "'max_body_size' must not be negative"
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY) if self._scoped else None
        if limiter is not None:
            # Nested inside another limit layer: this one overrides it for this part of the tree.
            limiter.max_body_size = self.max_body_size
            if not self._deferred:
                limiter.check_content_length(scope)
            await self.app(scope, receive, send)
            return

        limiter = _BodyLimiter(self.max_body_size, receive)
        if self._scoped:
            scope[_SCOPE_KEY] = limiter
        response_started = False
        replaced = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started, replaced
            if replaced:
                return
            if message["type"] == "http.response.start":
                if limiter.exceeded and not response_started:
                    # Whatever the application makes of the failure (for instance a 500 from
                    # error handling in between), the answer to an oversized request is a 413.
                    replaced = response_started = True
                    await self._too_large(scope, receive, send)
                    return
                response_started = True
            await send(message)

        try:
            if not self._deferred:
                limiter.check_content_length(scope)
            await self.app(scope, limiter.receive, send_wrapper)
        except Exception:
            if not limiter.exceeded:
                raise
        finally:
            if self._scoped:
                scope.pop(_SCOPE_KEY, None)
        if limiter.exceeded and not response_started:
            await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


class _ScopedBodyLimit(RequestBodyLimitMiddleware):
    """The body limit layer used by `Starlette`, `Router`, `Mount` and `Route`."""

    _scoped = True

    def __init__(self, app: ASGIApp, max_body_size: int, *, deferred: bool = False) -> None:
        super().__init__(app, max_body_size)
        self._deferred = deferred
