from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` once the request body exceeds the configured limit."""


class _BodyLimitState:
    """The limit and the byte count shared by every layer that handles one request."""

    __slots__ = ("limit", "received", "exceeded")

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False


def _validate(max_body_size: int | None) -> None:
    if max_body_size is not None and (isinstance(max_body_size, bool) or max_body_size < 0):
        raise ValueError("max_body_size must be a non-negative integer or None.")


def _declared_length_exceeds(scope: Scope, limit: int) -> bool:
    for key, value in scope["headers"]:
        if key == b"content-length":
            try:
                if int(value) > limit:
                    return True
            except ValueError:
                pass  # Not a valid length; the received bytes are still counted.
    return False


async def _send_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = PlainTextResponse("Content Too Large", status_code=413)
    await response(scope, receive, send)


async def check_declared_length(scope: Scope, receive: Receive, send: Send) -> bool:
    """
    Fail fast on a `Content-Length` over the limit that currently applies.

    Returns `True` if the request may proceed. Otherwise a 413 response has been sent
    and the application must not be called.
    """
    if scope["type"] != "http":
        return True
    state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
    if state is None or state.limit is None or not _declared_length_exceeds(scope, state.limit):
        return True
    state.exceeded = True
    await _send_too_large(scope, receive, send)
    return False


class RequestBodyLimitMiddleware:
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413.

    The bytes received from the server are counted, so `Content-Length` is only used to
    fail fast. When used below another body limit (e.g. inside a Starlette application
    with `max_body_size` set) this limit replaces the outer one.
    """

    # Whether a `Content-Length` check is left to the routing layer, because something
    # below this middleware may still change the limit.
    defer_declared_length_check = False

    def __init__(self, app: ASGIApp, max_body_size: int | None) -> None:
        _validate(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _BodyLimitState | None = scope.get(_SCOPE_KEY)
        if state is not None:
            # An outer layer is already counting; only the limit changes.
            if self.max_body_size is not None:
                state.limit = self.max_body_size
                if state.received > state.limit:
                    state.exceeded = True
                    raise RequestBodyTooLarge()
                if not self.defer_declared_length_check and not await check_declared_length(scope, receive, send):
                    return
            await self.app(scope, receive, send)
            return

        if self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        state = scope[_SCOPE_KEY] = _BodyLimitState(self.max_body_size)
        response_started = False

        async def receive_wrapper() -> Message:
            assert state is not None
            if state.exceeded:
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                if state.limit is not None and state.received > state.limit:
                    state.exceeded = True
                    raise RequestBodyTooLarge()
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        if not self.defer_declared_length_check and not await check_declared_length(scope, receive, send_wrapper):
            return

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except RequestBodyTooLarge:
            pass
        if state.exceeded and not response_started:
            await _send_too_large(scope, receive, send_wrapper)


class _DeferredRequestBodyLimitMiddleware(RequestBodyLimitMiddleware):
    """Used by `Starlette`, `Router` and `Mount`: the routing layer checks `Content-Length`."""

    defer_declared_length_check = True
