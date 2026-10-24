from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SCOPE_KEY = "starlette.body_limit"


class _BodyTooLarge(Exception):
    def __init__(self, limiter: _BodyLimiter) -> None:
        super().__init__("Request body exceeded the maximum size.")
        self.limiter = limiter


def _is_own_error(exc: BaseException, limiter: _BodyLimiter) -> bool:
    if isinstance(exc, _BodyTooLarge):
        return exc.limiter is limiter
    # Exception groups, e.g. raised out of a task group that was reading the body.
    nested: Sequence[BaseException] = getattr(exc, "exceptions", ())
    return bool(nested) and all(_is_own_error(e, limiter) for e in nested)


class _BodyLimiter:
    """Counts the body bytes received for a single request, and enforces a limit on them."""

    def __init__(self, limit: int, scope: Scope, receive: Receive, send: Send) -> None:
        self.limit = limit
        self.scope = scope
        self.received = 0
        self.response_started = False
        self.rejected = False
        self.exceeded = False
        self._receive = receive
        self._send = send

    async def receive(self) -> Message:
        if self.exceeded:
            raise _BodyTooLarge(self)
        message = await self._receive()
        if message["type"] == "http.request":
            self.received += len(message.get("body", b""))
            if self.received > self.limit:
                await self.reject()
                raise _BodyTooLarge(self)
        return message

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start":
            self.response_started = True
        await self._send(message)

    async def reject(self) -> None:
        self.exceeded = True
        if self.response_started or self.rejected:
            return
        self.rejected = True
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(self.scope, self._receive, self._send)

    async def settle(self) -> bool:
        """Fail fast on a declared `Content-Length`. Returns `True` if the request was rejected."""
        declared = Headers(scope=self.scope).get("content-length", "")
        too_large = self.received > self.limit or (
            declared.isascii() and declared.isdigit() and int(declared) > self.limit
        )
        if too_large:
            await self.reject()
        return too_large

    async def run(self, app: ASGIApp) -> None:
        try:
            await app(self.scope, self.receive, self.send)
        except BaseException as exc:
            if not _is_own_error(exc, self):
                raise


def _settles(app: Any) -> bool:
    return bool(getattr(app, "_settles_body_limit", False))


async def settle_body_limit(scope: Scope) -> bool:
    """
    Called by the routing once it knows which limit applies to the request.
    Returns `True` if the request was rejected, and must not be handled.
    """
    limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY)
    return limiter is not None and await limiter.settle()


async def apply_body_limit(
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    app: ASGIApp,
    *,
    settle: bool,
) -> None:
    """
    Run `app` with `max_body_size` applied. A limit set by an outer layer of the same
    application is overridden, and `None` leaves the outer limit (if any) in place.

    If `settle` is set the limit is final here, and a too large `Content-Length` is rejected.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    limiter: _BodyLimiter | None = scope.get(_SCOPE_KEY)
    if limiter is not None:
        if max_body_size is not None:
            limiter.limit = max_body_size
        if settle and await limiter.settle():
            return
        await app(scope, receive, send)
    elif max_body_size is None:
        await app(scope, receive, send)
    else:
        limiter = _BodyLimiter(max_body_size, scope, receive, send)
        scope[_SCOPE_KEY] = limiter
        try:
            if not (settle and await limiter.settle()):
                await limiter.run(app)
        finally:
            del scope[_SCOPE_KEY]


class _ApplicationBodyLimitMiddleware:
    """The `Starlette(max_body_size=...)` limit. Inner routes, mounts and routers may override it."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await apply_body_limit(scope, receive, send, self.max_body_size, self.app, settle=False)


class RequestBodyLimitMiddleware:
    """
    Rejects requests whose body exceeds `max_body_size` bytes with a 413 response.

    Works with any ASGI application. The limit is fixed: nothing below the middleware can change it.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limiter = _BodyLimiter(self.max_body_size, scope, receive, send)
        if not await limiter.settle():
            await limiter.run(self.app)
