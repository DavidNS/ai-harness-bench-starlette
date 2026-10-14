from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class _BodyLimitExceeded(Exception):
    pass


class _BodyLimitState:
    """Per-request state shared by nested body limit middleware, so an inner limit overrides the outer one."""

    def __init__(self, limit: int, declared: int | None) -> None:
        self.limit = limit
        self.declared = declared
        self.received = 0
        self.exceeded = False

    def declared_too_large(self) -> bool:
        return self.declared is not None and self.declared > self.limit


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is not None and value.isdigit():
        return int(value)
    return None


def has_body_limit(routes: Iterable[Any]) -> bool:
    """Whether any of the routes (recursively) sets its own `max_body_size`."""
    return any(
        getattr(route, "max_body_size", None) is not None
        or getattr(getattr(route, "_base_app", None), "max_body_size", None) is not None
        or has_body_limit(getattr(route, "routes", ()))
        for route in routes
    )


class RequestBodyLimitMiddleware:
    """Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    The bytes received from the server are counted, `Content-Length` is only used to fail fast.
    When nested, the innermost middleware decides the limit for the part of the app it wraps.
    """

    def __init__(self, app: ASGIApp, max_body_size: int, *, check_content_length: bool = True) -> None:
        self.app = app
        self.max_body_size = max_body_size
        # When False, a declared Content-Length is only checked once the app reads the body.
        # Used by the application when an inner limit may still raise the limit.
        self.check_content_length = check_content_length

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
        if state is None:
            state = scope[_SCOPE_KEY] = _BodyLimitState(self.max_body_size, _declared_length(scope))
            inner_receive = receive

            async def limited_receive() -> Message:
                assert state is not None
                if state.exceeded or state.declared_too_large():
                    state.exceeded = True
                    raise _BodyLimitExceeded()
                message = await inner_receive()
                if message["type"] == "http.request":
                    state.received += len(message.get("body", b""))
                    if state.received > state.limit:
                        state.exceeded = True
                        raise _BodyLimitExceeded()
                return message

            receive = limited_receive
        else:
            state.limit = self.max_body_size

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        if self.check_content_length and state.declared_too_large():
            await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send_wrapper)
        except _BodyLimitExceeded:
            if not response_started:
                await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)
