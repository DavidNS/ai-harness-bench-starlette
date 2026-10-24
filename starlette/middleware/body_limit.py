from __future__ import annotations

from collections.abc import Sequence

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

BODY_LIMIT_SCOPE_KEY = "starlette.body_limit"


class RequestTooLarge(Exception):
    """Raised from `receive()` when the request body crosses the configured limit."""

    def __init__(self, state: _BodyLimitState) -> None:
        super().__init__("Request body exceeded the configured limit.")
        self.state = state


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is not None and (isinstance(max_body_size, bool) or max_body_size < 0):
        raise ValueError("'max_body_size' must be a non-negative integer or None.")


def _declared_length(scope: Scope) -> int | None:
    value = Headers(scope=scope).get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _receive_nothing() -> Message:  # pragma: no cover
    return {"type": "http.disconnect"}


class _BodyLimitState:
    def __init__(self, limit: int, send: Send) -> None:
        self.limit = limit
        self.send = send  # the server's `send`, used to emit our own 413.
        self.received = 0
        self.exceeded = False
        self.started = False  # a response (ours or the app's) has been started.
        self.app_started = False  # the application started its own response.

    def too_large(self, scope: Scope) -> bool:
        if self.received > self.limit:
            return True
        declared = _declared_length(scope)
        return declared is not None and declared > self.limit

    async def respond(self, scope: Scope) -> None:
        self.exceeded = True
        if self.started:
            return
        self.started = True
        response = PlainTextResponse("Content Too Large", status_code=413)
        await response(scope, _receive_nothing, self.send)


def _is_raised_by(exc: BaseException, state: _BodyLimitState) -> bool:
    if isinstance(exc, RequestTooLarge):
        return exc.state is state
    # Task groups (e.g. in `BaseHTTPMiddleware`) wrap exceptions in exception groups.
    leaves: Sequence[BaseException] | None = getattr(exc, "exceptions", None)
    return bool(leaves) and all(_is_raised_by(leaf, state) for leaf in leaves)  # type: ignore[union-attr]


async def _run(app: ASGIApp, scope: Scope, receive: Receive, send: Send, state: _BodyLimitState) -> None:
    async def limited_receive() -> Message:
        if state.exceeded:
            raise RequestTooLarge(state)
        message = await receive()
        if message["type"] == "http.request":
            state.received += len(message.get("body", b""))
            if state.received > state.limit:
                state.exceeded = True
                raise RequestTooLarge(state)
        return message

    async def limited_send(message: Message) -> None:
        if state.exceeded and not state.app_started:
            # The request is rejected: replace whatever the app tries to respond with.
            if message["type"] == "http.response.start":
                await state.respond(scope)
            return
        if message["type"] == "http.response.start":
            state.app_started = True
            state.started = True
        await send(message)

    try:
        await app(scope, limited_receive, limited_send)
    except Exception as exc:
        if not _is_raised_by(exc, state):
            raise
    if state.exceeded:
        await state.respond(scope)


async def call_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int,
    *,
    check: bool,
) -> None:
    """
    Run `app` under the body limit shared by the current request.

    If a limit is already being enforced for this request, `max_body_size` replaces it.
    Otherwise a new one is started.
    With `check=True` a `Content-Length` over the limit is rejected before `app` runs.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimitState | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is None:
        state = scope[BODY_LIMIT_SCOPE_KEY] = _BodyLimitState(max_body_size, send)
        if check and state.too_large(scope):
            await state.respond(scope)
            return
        await _run(app, scope, receive, send, state)
        return

    state.limit = max_body_size
    if check and state.too_large(scope):
        await state.respond(scope)
        return
    await app(scope, receive, send)


async def reject_if_too_large(scope: Scope, send: Send) -> bool:
    """Send a 413 and return True if the request already exceeds the limit in force."""
    state: _BodyLimitState | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is not None and state.too_large(scope):
        await state.respond(scope)
        return True
    return False


class _ScopedBodyLimitMiddleware:
    """Sets the application-wide limit; inner routes, mounts and routers may override it."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await call_with_body_limit(self.app, scope, receive, send, self.max_body_size, check=False)


class RequestBodyLimitMiddleware:
    """
    Rejects requests whose body is larger than `max_body_size` bytes with a 413 response.

    Works with any ASGI application. The limit counts the body bytes actually received,
    `Content-Length` is only used to fail fast.
    """

    def __init__(self, app: ASGIApp, max_body_size: int | None = None) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_body_size is None:
            await self.app(scope, receive, send)
            return

        state = _BodyLimitState(self.max_body_size, send)
        if state.too_large(scope):
            await state.respond(scope)
            return
        await _run(self.app, scope, receive, send, state)
