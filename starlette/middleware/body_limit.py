from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(BaseException):
    """
    Raised from `receive()` once the request body exceeds the limit.

    This deliberately derives from `BaseException`, so that it is not swallowed or
    turned into a 500 response by the `Exception` handlers inside the application.
    """


class _BodyLimit:
    """Per-request state, shared through the scope so that inner limits can override outer ones."""

    def __init__(self, max_body_size: int) -> None:
        self.max_body_size = max_body_size
        self.received = 0
        self.exceeded = False
        # Set when the Content-Length check has been deferred until routing is done,
        # so that an inner limit can still override this one.
        self.pending_length: int | None = None

    def check(self) -> None:
        if self.exceeded or self.received > self.max_body_size:
            self.exceeded = True
            raise RequestBodyTooLarge()


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _declared_length_too_large(scope: Scope, max_body_size: int) -> bool:
    length = _declared_length(scope)
    return length is not None and length > max_body_size


def check_pending_content_length(scope: Scope) -> None:
    """Run a deferred `Content-Length` check, once routing has settled on the effective limit."""
    state: _BodyLimit | None = scope.get(_SCOPE_KEY)
    if state is not None and state.pending_length is not None:
        length, state.pending_length = state.pending_length, None
        if length > state.max_body_size:
            state.exceeded = True
            raise RequestBodyTooLarge()


class RequestBodyLimitMiddleware:
    """
    Limit the total size of the request body, responding with a 413 if it is exceeded.

    A limit set on a middleware nested inside another one overrides the outer limit.
    """

    # Used by the routing classes: wait for the endpoint to be resolved before checking
    # `Content-Length`, as an inner route, mount or router may override the limit.
    _defer_content_length_check = False

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: _BodyLimit | None = scope.get(_SCOPE_KEY)
        if state is not None:
            # An outer limit is already counting the body: override its limit, and let it
            # produce the 413 response.
            state.max_body_size = self.max_body_size
            state.pending_length = None
            state.exceeded = False
            state.check()
            if _declared_length_too_large(scope, self.max_body_size):
                state.exceeded = True
                raise RequestBodyTooLarge()
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def respond() -> None:
            if not response_started:
                response = PlainTextResponse("Content Too Large", status_code=413)
                await response(scope, receive, send_wrapper)

        state = _BodyLimit(self.max_body_size)
        if self._defer_content_length_check:
            state.pending_length = _declared_length(scope)
        elif _declared_length_too_large(scope, self.max_body_size):
            await respond()
            return
        scope[_SCOPE_KEY] = state

        async def receive_wrapper() -> Message:
            state.check()
            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                state.check()
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except RequestBodyTooLarge:
            await respond()
        finally:
            scope.pop(_SCOPE_KEY, None)


class RoutingBodyLimitMiddleware(RequestBodyLimitMiddleware):
    """The variant of the middleware used by `Starlette`, `Router`, `Mount` and `Route`."""

    _defer_content_length_check = True
