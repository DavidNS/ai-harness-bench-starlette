from __future__ import annotations

import sys
from typing import Literal

if sys.version_info < (3, 11):  # pragma: no cover
    try:
        from exceptiongroup import BaseExceptionGroup
    except ImportError:

        class BaseExceptionGroup(BaseException):  # type: ignore[no-redef]
            pass


from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

BODY_LIMIT_SCOPE_KEY = "starlette.body_limit"


class _BodyLimitExceeded(BaseException):
    """
    Raised from `receive()` once the body is over the limit.

    It derives from `BaseException` so that application code catching `Exception`
    (error handlers, `except Exception`) does not swallow it or turn it into a 500.
    """


def _is_knock_on(exc: BaseException) -> bool:
    # What `BaseHTTPMiddleware` reports next to the limit error when the app below it stopped early.
    return isinstance(exc, _BodyLimitExceeded) or (
        isinstance(exc, RuntimeError) and str(exc) == "No response returned."
    )


def _declared_length_exceeds(scope: Scope, limit: int) -> bool:
    value = Headers(scope=scope).get("content-length")
    if value is None:
        return False
    value = value.strip()
    if not (value.isascii() and value.isdigit()):
        return False
    # Compare without `int()`: it refuses very long digit strings.
    digits = value.lstrip("0")
    if len(digits) != len(str(limit)):
        return len(digits) > len(str(limit))
    return digits > str(limit)


def validate_max_body_size(max_body_size: int | None) -> None:
    if max_body_size is None:
        return
    if isinstance(max_body_size, bool) or not isinstance(max_body_size, int):
        raise TypeError("max_body_size must be an int or None.")
    if max_body_size < 0:
        raise ValueError("max_body_size must not be negative.")


def may_change_limit(app: ASGIApp) -> bool:
    """Whether `app`, possibly wrapped in middleware, contains a router that can set its own limit."""
    seen = 0
    while app is not None and seen < 100:
        if getattr(app, "_routes_body_limit", False):
            return True
        app = getattr(app, "app", None)  # type: ignore[assignment]
        seen += 1
    return False


class _BodyLimit:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.received = 0
        self.exceeded = False
        # A declared `Content-Length` is still to be checked, once the limit in force is settled.
        self.pending = False

    def set_limit(self, limit: int) -> None:
        self.limit = limit
        if self.received > limit:
            self.exceeded = True
            raise _BodyLimitExceeded()

    def wrap(self, receive: Receive) -> Receive:
        async def limited_receive() -> Message:
            if self.exceeded:
                raise _BodyLimitExceeded()
            message = await receive()
            if message["type"] == "http.request":
                self.received += len(message.get("body", b""))
                if self.received > self.limit:
                    self.exceeded = True
                    raise _BodyLimitExceeded()
            return message

        return limited_receive


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)


async def reject_if_declared_too_large(scope: Scope, receive: Receive, send: Send) -> bool:
    """Send a 413 and return `True` if `Content-Length` is over the limit in force."""
    state: _BodyLimit | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is not None:
        state.pending = False
    if state is not None and scope["type"] == "http" and _declared_length_exceeds(scope, state.limit):
        await _too_large(scope, receive, send)
        return True
    return False


async def run_with_body_limit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    max_body_size: int | None,
    *,
    check_content_length: Literal["now", "lazy", "never"],
) -> None:
    """
    Call `app` with the body limit applied.

    Inside a limited request the limit already in force is replaced, so that inner
    routing can raise or lower it. Otherwise this call owns the limit: it counts the
    body and sends the 413.

    `check_content_length` says when a declared `Content-Length` is checked: "now", because nothing
    below `app` can change the limit any more; "never", because a router below `app` settles it; or
    "lazy", when `app` is opaque and may or may not route: the check is then made when `app` first
    receives or sends, unless a router below has settled the limit and checked by then.
    """
    if scope["type"] != "http":
        await app(scope, receive, send)
        return

    state: _BodyLimit | None = scope.get(BODY_LIMIT_SCOPE_KEY)
    if state is not None:
        # An outer layer owns the counting and the response.
        if max_body_size is not None:
            state.set_limit(max_body_size)
        if check_content_length == "now":
            if await reject_if_declared_too_large(scope, receive, send):
                return
        elif check_content_length == "lazy":
            state.pending = True
            receive, send = _lazy_check(state, scope, receive, send)
        await app(scope, receive, send)
        return

    if max_body_size is None:
        await app(scope, receive, send)
        return

    state = _BodyLimit(max_body_size)
    if check_content_length == "now" and _declared_length_exceeds(scope, max_body_size):
        await _too_large(scope, receive, send)
        return
    scope[BODY_LIMIT_SCOPE_KEY] = state
    if check_content_length == "lazy":
        state.pending = True
        await _guard(state, app, scope, state.wrap(receive), send, lazy=True)
        return
    await _guard(state, app, scope, receive, send)


def _lazy_check(state: _BodyLimit, scope: Scope, receive: Receive, send: Send) -> tuple[Receive, Send]:
    def check() -> None:
        if state.exceeded:
            # Concurrent tasks of the app must stop too once the declared length was rejected.
            raise _BodyLimitExceeded()
        if state.pending:
            state.pending = False
            if _declared_length_exceeds(scope, state.limit):
                state.exceeded = True
                raise _BodyLimitExceeded()

    async def lazy_receive() -> Message:
        check()
        return await receive()

    async def lazy_send(message: Message) -> None:
        check()
        await send(message)

    return lazy_receive, lazy_send


async def _guard(
    state: _BodyLimit, app: ASGIApp, scope: Scope, receive: Receive, send: Send, *, lazy: bool = False
) -> None:
    response_started = False

    async def guarded_send(message: Message) -> None:
        nonlocal response_started
        if message["type"] == "http.response.start":
            response_started = True
        await send(message)

    app_receive: Receive = receive if lazy else state.wrap(receive)
    app_send: Send = guarded_send
    if lazy:
        app_receive, app_send = _lazy_check(state, scope, app_receive, guarded_send)
    try:
        await app(scope, app_receive, app_send)
    except BaseException as exc:
        if not state.exceeded:
            raise
        if isinstance(exc, BaseExceptionGroup):
            _, rest = exc.split(_is_knock_on)
            if rest is not None:
                raise rest from None
        elif not isinstance(exc, _BodyLimitExceeded):
            raise
    if state.exceeded and not response_started:
        await _too_large(scope, receive, guarded_send)


class RequestBodyLimitMiddleware:
    """
    Reject HTTP requests whose body is larger than `max_body_size` bytes with a 413.

    The limit counts the body bytes received from the server. `Content-Length` is only
    used to reject early, before the application runs.
    """

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        validate_max_body_size(max_body_size)
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if _declared_length_exceeds(scope, self.max_body_size):
            await _too_large(scope, receive, send)
            return

        await _guard(_BodyLimit(self.max_body_size), self.app, scope, receive, send)


class _ScopeBodyLimitMiddleware:
    """The application-level limit of `Starlette`: shares its state with the routing layers."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await run_with_body_limit(self.app, scope, receive, send, self.max_body_size, check_content_length="never")
