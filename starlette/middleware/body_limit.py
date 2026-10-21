from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.request_body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` once the request body exceeds the configured limit."""


class _BodyLimitState:
    """Per-request state, shared by every body limit middleware the request passes through."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is None:
        return None
    value = value.strip()
    if not value.isascii() or not value.isdigit():
        return None
    return int(value)


class RequestBodyLimitMiddleware:
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    The limit counts the body bytes actually received from the server. `Content-Length` is only
    used to fail fast: a request declaring a larger body never gets to read it. A limit set by
    an inner `RequestBodyLimitMiddleware` replaces the outer one for the rest of the request.
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
            # An outer middleware already counts the body, only the limit is replaced. The
            # outer middleware takes care of the response.
            state.limit = self.max_body_size
            await self.app(scope, receive, send)
            return

        state = _BodyLimitState(self.max_body_size)
        scope[_SCOPE_KEY] = state
        declared = _declared_length(scope)
        response_started = False
        replaced = False

        def too_large() -> bool:
            return state.exceeded or (declared is not None and declared > state.limit)

        async def send_too_large() -> None:
            nonlocal response_started, replaced
            response_started = replaced = True
            response = PlainTextResponse("Content Too Large", status_code=413)
            await response(scope, receive, send)

        async def wrapped_receive() -> Message:
            if too_large():
                state.exceeded = True
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                if state.received > state.limit:
                    state.exceeded = True
                    raise RequestBodyTooLarge()
            return message

        async def wrapped_send(message: Message) -> None:
            nonlocal response_started
            if replaced:
                return
            if message["type"] == "http.response.start" and not response_started:
                if too_large():
                    # The declared length is over the limit even if the application never read.
                    await send_too_large()
                    return
                response_started = True
            await send(message)

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except RequestBodyTooLarge:
            pass
        if not response_started and too_large():
            await send_too_large()
