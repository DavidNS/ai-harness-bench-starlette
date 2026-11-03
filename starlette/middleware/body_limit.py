from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"
# Set by a router without a limit of its own: the layers above it have no limit.
_ROUTED_KEY = "starlette.body_limit_routed"


class RequestBodyTooLarge(Exception):
    """Raised inside the application when the request body exceeds the limit."""


class _BodyLimitState:
    """Per-request state, shared by every layer that applies a limit to the request."""

    def __init__(self, limit: int, content_length: int | None) -> None:
        self.limit = limit
        self.content_length = content_length
        self.received = 0
        self.exceeded = False
        # The limit of the outermost routing layer, which applies to requests matching no route.
        self.top: float | None = None

    def check_received(self) -> None:
        if self.received > self.limit:
            self.exceeded = True
            raise RequestBodyTooLarge()

    def settle(self, limit: int) -> None:
        if self.content_length is not None and self.content_length > limit:
            self.exceeded = True
            raise RequestBodyTooLarge()


def _declared_content_length(scope: Scope) -> int | None:
    for key, value in scope["headers"]:
        if key == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is not None and max_body_size < 0:
        raise ValueError("max_body_size must not be negative")


def mark_routed(scope: Scope) -> None:
    if _SCOPE_KEY not in scope:
        scope[_ROUTED_KEY] = True


def settle_body_limit(scope: Scope, owner: Any, state: _BodyLimitState | None = None) -> None:
    """
    Fail fast on a declared `Content-Length` over the limit that `owner` will apply.

    `owner` resolves that limit by matching the request against its routes, without running
    anything. It is called on entry to every layer that has a limit of its own and to every
    router, before any of their middleware, so a limit set further in is checked as well.
    """
    state = state or scope.get(_SCOPE_KEY)
    if state is None or state.content_length is None:
        return
    inherited = state.limit
    if state.top is None:
        own = owner.max_body_size
        state.top = inherited if own is None else own
    state.settle(owner._resolve_body_limit(scope, inherited, state.top))


def _is_too_large(exc: BaseException) -> bool:
    if isinstance(exc, RequestBodyTooLarge):
        return True
    # Task groups (anyio, BaseHTTPMiddleware) wrap the error in an exception group.
    nested: Sequence[BaseException] = getattr(exc, "exceptions", ())
    return bool(nested) and all(_is_too_large(e) for e in nested)


class RequestBodyLimitMiddleware:
    """
    Responds with `413 Content Too Large` to requests whose body exceeds `max_body_size` bytes.

    The limit counts the body bytes received from the server. `Content-Length` is only used to
    reject early. If the middleware is used below another body limit, it overrides that limit.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    def _check_declared(self, scope: Scope, state: _BodyLimitState) -> None:
        """Reject a declared `Content-Length` over the limit this layer applies, before it runs."""
        assert self.max_body_size is not None
        state.settle(self.max_body_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        outer: _BodyLimitState | None = scope.get(_SCOPE_KEY)
        if outer is not None:
            # An outer layer counts the body and sends the response, we only change the limit.
            self._check_declared(scope, outer)
            outer.limit = self.max_body_size
            await self.app(scope, receive, send)
            return

        state = _BodyLimitState(self.max_body_size, _declared_content_length(scope))
        if scope.get(_ROUTED_KEY):
            state.top = float("inf")  # the application has no limit
        scope[_SCOPE_KEY] = state
        response_started = False

        async def limited_receive() -> Message:
            if state.exceeded:
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                state.check_received()
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if state.exceeded and not response_started:
                return  # The 413 response replaces whatever the application tries to send.
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            self._check_declared(scope, state)
            await self.app(scope, limited_receive, guarded_send)
        except Exception as exc:
            if not _is_too_large(exc) and (response_started or not state.exceeded):
                raise
            state.exceeded = True

        if state.exceeded and not response_started:
            response = PlainTextResponse("Content Too Large", status_code=413)
            await response(scope, receive, send)


class _RoutedBodyLimitMiddleware(RequestBodyLimitMiddleware):
    """
    The limit of a routing layer. Without an owner (the application), the `Content-Length`
    check waits for the outermost router. With one, the owner resolves the limit that its
    routes will apply and the check happens on entry, before the owner's own middleware.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None, owner: Any = None) -> None:
        super().__init__(app, max_body_size)
        self.owner = owner

    def _check_declared(self, scope: Scope, state: _BodyLimitState) -> None:
        if self.owner is not None:
            settle_body_limit(scope, self.owner, state)
