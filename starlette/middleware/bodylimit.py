from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.request_body_limit"


class _BodyTooLarge(Exception):
    """Raised from `receive()` once the request body crosses the configured limit."""


class _BodyLimitState:
    """Per-request state, shared by every limit layer the request passes through."""

    def __init__(self, limit: int, scope: Scope) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.declared: int | None = None
        value = Headers(scope=scope).get("content-length")
        if value is not None:
            try:
                declared = int(value)
            except ValueError:
                pass
            else:
                if declared >= 0:
                    self.declared = declared

    def is_over(self) -> bool:
        return self.received > self.limit or (self.declared is not None and self.declared > self.limit)


class RequestBodyLimitMiddleware:
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a
    `413 Content Too Large` response.

    The bytes counted are those received from the ASGI server. `Content-Length` is only
    used to fail fast; a missing or understated header does not let a larger body through.

    A limit set further down the stack (for example on a `Mount` or a `Route`) replaces
    the limit of the outer one for that part of the application.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None, *, _defer_declared_length: bool = False) -> None:
        assert max_body_size is None or max_body_size >= 0, "'max_body_size' must not be negative."
        self.app = app
        self.max_body_size = max_body_size
        # When an inner layer may still change the limit, the declared length is only
        # checked once the application starts reading the body.
        self._defer_declared_length = _defer_declared_length

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(SCOPE_KEY)
        if state is not None:
            # An outer layer already counts the body: only replace its limit.
            state.limit = self.max_body_size
            if state.is_over():
                await self._reject(scope, receive, send)
            else:
                await self.app(scope, receive, send)
            return

        state = scope[SCOPE_KEY] = _BodyLimitState(self.max_body_size, scope)
        if not self._defer_declared_length and state.is_over():
            await self._reject(scope, receive, send)
            return

        response_started = False

        async def limited_receive() -> Message:
            if state.exceeded or state.is_over():
                state.exceeded = True
                raise _BodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                if state.is_over():
                    state.exceeded = True
                    raise _BodyTooLarge()
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if state.exceeded and not response_started:
                # The 413 below replaces whatever the application tries to answer.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _BodyTooLarge:
            pass

        if state.exceeded and not response_started:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(scope, receive, send)
