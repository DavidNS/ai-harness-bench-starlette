from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` once the body received exceeds the limit."""

    def __init__(self) -> None:
        super().__init__("Request body exceeded the maximum size.")


class _State:
    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.response_started = False

    def declared_too_large(self, scope: Scope) -> bool:
        if self.limit is None:
            return False
        value = Headers(scope=scope).get("content-length")
        if value is None:
            return False
        try:
            return int(value) > self.limit
        except ValueError:
            return False


async def _respond_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = PlainTextResponse("Content Too Large", status_code=413)
    await response(scope, receive, send)


async def reject_declared_too_large(scope: Scope, receive: Receive, send: Send) -> bool:
    """
    Fail fast on `Content-Length` against the limit currently in force.

    Returns `True` if a 413 response was sent, in which case the caller must stop.
    """
    state: _State | None = scope.get(_SCOPE_KEY)
    if state is None or not state.declared_too_large(scope):
        return False
    state.exceeded = True
    await _respond_too_large(scope, receive, send)
    return True


class RequestBodyLimitMiddleware:
    """
    Limits the total size of the HTTP request body, responding with a 413 if exceeded.

    The limit counts the body bytes actually received from the server. The `Content-Length`
    header is only used to reject early. If this middleware is nested inside another one,
    the inner limit replaces the outer one.
    """

    # Whether to check `Content-Length` on entry. Routing sets this to `False`, because
    # a route, mount or router further down may still change the limit.
    _check_on_entry = True

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _State | None = scope.get(_SCOPE_KEY)
        if state is not None:
            # Nested: override the limit of the outer one, which keeps counting.
            state.limit = self.max_body_size
            if self._check_on_entry and await reject_declared_too_large(scope, receive, send):
                return
            await self._run(state, scope, receive, send)
            return

        state = scope[_SCOPE_KEY] = _State(self.max_body_size)

        async def wrapped_receive() -> Message:
            if state.exceeded:
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                if state.limit is not None and state.received > state.limit:
                    state.exceeded = True
                    raise RequestBodyTooLarge()
            return message

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state.response_started = True
            await send(message)

        try:
            if self._check_on_entry and await reject_declared_too_large(scope, receive, wrapped_send):
                return
            await self._run(state, scope, wrapped_receive, wrapped_send)
        finally:
            del scope[_SCOPE_KEY]

    async def _run(self, state: _State, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception:
            if not state.exceeded:
                raise
        if state.exceeded and not state.response_started:
            await _respond_too_large(scope, receive, send)


class _RoutingBodyLimitMiddleware(RequestBodyLimitMiddleware):
    _check_on_entry = False


def make_limit_middleware(app: ASGIApp, max_body_size: int | None, *, check_on_entry: bool = False) -> ASGIApp:
    if max_body_size is None:
        return app
    cls: Any = RequestBodyLimitMiddleware if check_on_entry else _RoutingBodyLimitMiddleware
    return cls(app, max_body_size)
