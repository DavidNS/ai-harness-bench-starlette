from __future__ import annotations

from typing import Any

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.request_body_limit"


class RequestBodyTooLarge(Exception):
    """Raised into the application when the request body exceeds the limit."""


def _is_too_large(exc: BaseException) -> bool:
    inner_exceptions = getattr(exc, "exceptions", None)  # exception groups, also on Python 3.10
    if inner_exceptions:
        return all(_is_too_large(inner) for inner in inner_exceptions)
    return isinstance(exc, RequestBodyTooLarge)


class _BodyLimit:
    """Per-request state, shared through the scope by every layer that sets a limit."""

    def __init__(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        self.scope = scope
        self.limit = limit
        self.received = 0
        self.response_started = False
        self.exceeded = False
        # True once our 413 went out, after which the application's own sends are dropped.
        self.rejected = False
        self._receive = receive
        self._send = send

    def declared_length(self) -> int | None:
        for name, value in self.scope["headers"]:
            if name == b"content-length":
                value = value.strip()
                return int(value) if value.isdigit() else None
        return None

    async def reject(self) -> None:
        self.exceeded = True
        if self.response_started:
            return
        self.rejected = self.response_started = True
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(self.scope, self._receive, self._send)

    async def check(self) -> bool:
        """Fail fast on the declared length. Returns False if the request was rejected."""
        declared = self.declared_length()
        if self.received > self.limit or (declared is not None and declared > self.limit):
            await self.reject()
            return False
        return True

    async def receive(self) -> Message:
        if self.rejected:
            raise RequestBodyTooLarge()
        if self.exceeded:
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.received > self.limit:
                await self.reject()
                if self.rejected:
                    raise RequestBodyTooLarge()
                # The response is already underway: stop the body stream.
                return {"type": "http.disconnect"}
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)


async def call_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int | None,
    *,
    check_declared_length: bool = True,
) -> None:
    """
    Call `app`, enforcing `limit` on the request body.

    `None` means "inherit": an enclosing limit stays in force, and without one the
    request is not limited. A limit set here replaces any enclosing one.
    """
    state: _BodyLimit | None = scope.get(_SCOPE_KEY)
    if state is not None:
        if limit is not None:
            state.limit = limit
        if check_declared_length and not await state.check():
            return
        await app(scope, receive, send)
        return

    if limit is None:
        await app(scope, receive, send)
        return

    state = scope[_SCOPE_KEY] = _BodyLimit(scope, receive, send, limit)
    try:
        if check_declared_length and not await state.check():
            return
        await app(scope, state.receive, state.send)
    except BaseException as exc:
        if state.rejected and _is_too_large(exc):
            return
        raise


async def check_body_limit(scope: Scope) -> bool:
    """Fail-fast check against the limit currently in force. False means a 413 was sent."""
    state: _BodyLimit | None = scope.get(_SCOPE_KEY)
    return state is None or await state.check()


class RequestBodyLimitMiddleware:
    """
    Rejects requests whose body exceeds `max_body_size` bytes with a 413 response.

    The limit counts the body bytes received from the server. `Content-Length` is only
    used to reject early; a missing or understated header does not let a body through.
    """

    # Overridden by the Starlette application, which checks the declared length
    # once routing has settled which limit applies.
    _check_declared_length_on_entry = True

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await call_with_body_limit(
            self.app,
            scope,
            receive,
            send,
            self.max_body_size,
            check_declared_length=self._check_declared_length_on_entry,
        )


class _RoutedRequestBodyLimitMiddleware(RequestBodyLimitMiddleware):
    _check_declared_length_on_entry = False


def validate_max_body_size(max_body_size: Any) -> None:
    if max_body_size is not None and (
        isinstance(max_body_size, bool) or not isinstance(max_body_size, int) or max_body_size < 0
    ):
        raise ValueError("max_body_size must be a non-negative integer or None.")
