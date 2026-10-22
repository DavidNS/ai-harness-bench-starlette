from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SCOPE_KEY = "starlette.body_limit"


class RequestBodyTooLarge(Exception):
    """Raised when the request body is larger than the limit that applies to it."""


class _BodyLimitState:
    """Per-request state, shared by the outermost limit and any inner limit overriding it."""

    __slots__ = ("limit", "received", "exceeded", "response_started")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        self.response_started = False


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is not None:
        assert isinstance(max_body_size, int) and not isinstance(max_body_size, bool), (
            "'max_body_size' must be an integer or None"
        )
        assert max_body_size >= 0, "'max_body_size' must not be negative"


def check_body_limit(scope: Scope) -> None:
    """
    Fail fast if the declared `Content-Length` (or what was already received)
    is over the limit that currently applies. A no-op when no limit is active.
    """
    state: _BodyLimitState | None = scope.get(SCOPE_KEY)
    if state is None or scope["type"] != "http":
        return
    declared = 0
    for value in Headers(scope=scope).getlist("content-length"):
        try:
            declared = max(declared, int(value))
        except ValueError:
            pass
    if max(declared, state.received) > state.limit:
        state.exceeded = True
        raise RequestBodyTooLarge()


class RequestBodyLimitMiddleware:
    """
    Rejects HTTP requests whose body is larger than `max_body_size` bytes with a 413.

    When nested inside another limit, it overrides the outer limit for the rest of the request.
    """

    def __init__(self, app: ASGIApp, max_body_size: int, *, _check_on_entry: bool = True) -> None:
        validate_max_body_size(max_body_size)
        assert max_body_size is not None, "'max_body_size' is required"
        self.app = app
        self.max_body_size = max_body_size
        # Inside a Starlette app something below may still change the limit,
        # so the declared length is checked once routing has decided instead.
        self._check_on_entry = _check_on_entry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        outer: _BodyLimitState | None = scope.get(SCOPE_KEY)
        if outer is not None:
            outer.limit = self.max_body_size
            if self._check_on_entry:
                check_body_limit(scope)
            await self.app(scope, receive, send)
            return

        state = scope[SCOPE_KEY] = _BodyLimitState(self.max_body_size)

        async def limited_receive() -> Message:
            if state.exceeded:
                raise RequestBodyTooLarge()
            message = await receive()
            if message["type"] == "http.request":
                state.received += len(message.get("body", b""))
                if state.received > state.limit:
                    state.exceeded = True
                    raise RequestBodyTooLarge()
            return message

        async def guarded_send(message: Message) -> None:
            if state.exceeded and not state.response_started:
                return  # the 413 replaces whatever the application tries to say now
            if message["type"] == "http.response.start":
                state.response_started = True
            await send(message)

        try:
            if self._check_on_entry:
                check_body_limit(scope)
            await self.app(scope, limited_receive, guarded_send)
        except Exception as exc:
            if not state.exceeded or (state.response_started and not isinstance(exc, RequestBodyTooLarge)):
                raise

        if state.exceeded and not state.response_started:
            response = PlainTextResponse("Content Too Large", status_code=413)
            await response(scope, receive, send)
