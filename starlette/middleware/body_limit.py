from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

BODY_LIMIT_SCOPE_KEY = "starlette.body_limit"
APP_LIMIT_SCOPE_KEY = "starlette.app_body_limit"

# Anything with more digits than this is larger than any limit worth configuring.
_MAX_DECLARED_DIGITS = 30


class RequestBodyTooLarge(Exception):
    """Raised from `receive()` once the request body crossed the limit."""


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is not None and (isinstance(max_body_size, bool) or max_body_size < 0):
        raise ValueError("max_body_size must be a non-negative integer or None.")


def _caused_by_limit(exc: BaseException) -> bool:
    # The exception may have been wrapped (chained, or grouped by a task group).
    seen: set[int] = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, RequestBodyTooLarge):
            return True
        related = (current.__cause__, current.__context__, *getattr(current, "exceptions", ()))
        pending.extend(other for other in related if other is not None)
    return False


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is None or not value.isascii() or not value.isdigit():
        return None
    value = value.lstrip("0") or "0"
    return int(value) if len(value) <= _MAX_DECLARED_DIGITS else 10**_MAX_DECLARED_DIGITS


class BodyLimit:
    """Per-request state: counts the body bytes received and holds the limit that currently applies."""

    def __init__(self, limit: int | None, base: int | None) -> None:
        self.limit = limit
        self.base = base  # the application's limit, when the scope doesn't say otherwise
        self.received = 0
        self.exceeded = False
        self.response_started = False
        self.held: list[Message] = []
        self.rejecting = False

    def _check(self) -> None:
        if self.limit is not None and self.received > self.limit:
            self.exceeded = True
        if self.exceeded:
            raise RequestBodyTooLarge()

    def wrap_receive(self, receive: Receive) -> Receive:
        async def limited_receive() -> Message:
            self._check()
            message = await receive()
            if message["type"] == "http.request":
                self.received += len(message.get("body", b""))
                self._check()
            return message

        return limited_receive

    def wrap_send(self, send: Send) -> Send:
        async def tracking_send(message: Message) -> None:
            if self.rejecting or not self.exceeded:
                if message["type"] == "http.response.start":
                    self.response_started = True
                await send(message)
            elif not self.response_started:
                # Too large, and no response yet: hold what the app sends until we know
                # whether it let the error propagate or swallowed it and answered anyway.
                self.held.append(message)
            # else: the response had started when the limit was crossed, stop the stream.

        return tracking_send

    def application_limit(self, scope: Scope) -> int | None:
        # Set by the outermost Starlette application, so a mounted sub-application can't replace it.
        limit: int | None = scope.get(APP_LIMIT_SCOPE_KEY, self.base)
        return limit

    def declared_too_large(self, scope: Scope) -> bool:
        if self.limit is None:
            return False
        declared = _declared_length(scope)
        return self.received > self.limit or (declared is not None and declared > self.limit)

    async def reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.exceeded = True
        if not self.response_started:
            self.response_started = True
            self.rejecting = True  # let the 413 through `tracking_send` instead of holding it
            await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)

    async def run(self, app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
        propagated = False
        try:
            await app(scope, self.wrap_receive(receive), self.wrap_send(send))
        except Exception as exc:
            if not (self.exceeded and _caused_by_limit(exc)):
                raise
            propagated = True
        if not self.exceeded:
            return
        if propagated and any(m["type"] == "http.response.start" for m in self.held):
            # The app started a response of its own: no second one.
            self.response_started = True
            for message in self.held:
                await send(message)
        else:
            await self.reject(scope, receive, send)


async def apply_body_limit(app: ASGIApp, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
    """
    Run `app` with `limit` as the limit that applies from here on.

    Inside a request that is already being counted the limit is replaced; otherwise
    counting (and the 413 handling) starts here.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return
    state: BodyLimit | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is not None:
        state.limit = limit
        await app(scope, receive, send)
        return
    state = BodyLimit(limit, limit)
    scope[BODY_LIMIT_SCOPE_KEY] = state
    try:
        await state.run(app, scope, receive, send)
    finally:
        scope.pop(BODY_LIMIT_SCOPE_KEY, None)


async def reject_if_too_large(scope: Scope, receive: Receive, send: Send, *, application_level: bool = False) -> bool:
    """
    Fail fast, for the limit that is now settled: send a 413 and return True when the
    declared `Content-Length` (or what was already received) exceeds it.

    With `application_level` the limit that applies is the application's, for requests
    that matched no route or no method.
    """
    if scope["type"] != "http":
        return False
    state: BodyLimit | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is None:
        return False
    if application_level:
        state.limit = state.application_limit(scope)
    if not state.declared_too_large(scope):
        return False
    await state.reject(scope, receive, send)
    return True


class _ApplicationBodyLimit:
    """Application level limit of `Starlette`, installed inside `ServerErrorMiddleware`."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await apply_body_limit(self.app, scope, receive, send, self.max_body_size)


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413 response.

    Works with any ASGI application. Websockets and lifespan are passed through.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Private state: nothing below this middleware can change its limit.
        state = BodyLimit(self.max_body_size, self.max_body_size)
        if state.declared_too_large(scope):
            await state.reject(scope, receive, send)
            return
        await state.run(self.app, scope, receive, send)


def _defers_check(app: object) -> bool:
    """Whether `app` is, or wraps, a router: it will do the check once its own limit is settled."""
    from starlette.routing import Router

    for _ in range(32):  # follow `.router` / `.app` through wrapping middleware
        if isinstance(app, Router):
            return True
        app = getattr(app, "router", None) or getattr(app, "app", None)
        if app is None:
            return False
    return False  # pragma: no cover
